"""Static rejection of dangerous Python before it ever executes. RFC §13.

An LLM critic is not a security control. It can be argued with, it is
non-deterministic across runs, and — decisively — the thing it is reviewing may
be adversarial text written specifically to persuade it. RFC §13 therefore puts
the security gate on the *host*, in deterministic code, and this module is that
gate for Python source.

Design commitments
------------------
**Deny by default.** Rules describe what is forbidden and everything matching
is rejected. A construct nobody anticipated is not automatically allowed,
because the interesting attacks are always the unanticipated ones.

**Unparseable source is REJECTED, never skipped.** This is the single most
important line in the module. A scanner that skips what it cannot read gives an
attacker a trivially reusable bypass: emit source the scanner's parser chokes
on but the *executing* interpreter accepts — a version-specific syntax, an
encoding declaration the tokenizer disagrees about, a null byte — and the file
sails through unscanned. "I could not analyse this" must mean "this does not
run", not "this is fine".

**Findings, not exceptions, from :meth:`scan`.** The engine wants to collect
everything wrong with an artifact in one pass for the ledger.
:meth:`scan_or_raise` is the enforcement wrapper.

Honest scope: this analyses *statically inspectable Python*. It cannot see
inside a compiled extension, a pickle payload, or a string that is assembled at
runtime from data the source never mentions — which is precisely why the
obfuscation rules below exist, and why the sandbox layer is a separate control
rather than an alternative to this one.
"""

from __future__ import annotations

import ast
import enum
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, Field

from paa.core.errors import SecurityScanError

__all__ = [
    "AstSecurityScanner",
    "Finding",
    "ScannerPolicy",
    "Severity",
]

log = structlog.get_logger(__name__)


class Severity(str, enum.Enum):
    """How bad a finding is.

    Only ``HIGH`` and ``CRITICAL`` block by default
    (:attr:`ScannerPolicy.blocking_severity`). ``LOW``/``MEDIUM`` exist so the
    scanner can *report* a smell without making the runtime unusable — a gate
    that fires on everything gets switched off, and a gate that is off protects
    nothing.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One rule violation, located precisely enough to fix."""

    rule: str
    severity: Severity
    lineno: int
    col: int
    snippet: str
    message: str

    def to_payload(self) -> dict[str, Any]:
        """Ledger form. Matches ``SecurityScanError.findings``."""
        return {
            "rule": self.rule,
            "severity": self.severity.value,
            "lineno": self.lineno,
            "col": self.col,
            "snippet": self.snippet,
            "message": self.message,
        }

    def __str__(self) -> str:
        return f"{self.severity.value} {self.rule} at {self.lineno}:{self.col} — {self.message}"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

#: Modules that are never importable in a sandboxed artifact.
#:
#: ``ctypes`` earns its place for a reason worth spelling out: it can call
#: arbitrary native code, which means every other rule here — and the AST
#: analysis itself — becomes decorative. Same for ``pickle``/``marshal``, whose
#: *load* path is arbitrary code execution by design, not by bug.
_DEFAULT_DENIED_MODULES: frozenset[str] = frozenset(
    {
        # process execution
        "subprocess",
        "pty",
        "commands",
        # native code / memory
        "ctypes",
        "cffi",
        "mmap",
        # deserialisation-as-code-execution
        "pickle",
        "cPickle",
        "dill",
        "marshal",
        "shelve",
        # network
        "socket",
        "socketserver",
        "ssl",
        "asyncio.subprocess",
        "urllib",
        "urllib2",
        "urllib3",
        "http",
        "httplib",
        "http.client",
        "ftplib",
        "telnetlib",
        "smtplib",
        "poplib",
        "imaplib",
        "xmlrpc",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "paramiko",
        "boto3",
        # import machinery — re-entry point for everything above
        "importlib",
        "imp",
        "runpy",
        "code",
        "codeop",
        # misc escape hatches
        "multiprocessing",
        "webbrowser",
        "distutils",
        "setuptools",
    }
)

#: Dotted attribute calls that are forbidden even when the module is allowed.
#: ``os`` is too useful to ban wholesale (``os.path``, ``os.environ.get``), so
#: the dangerous members are named individually.
_DEFAULT_DENIED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "os.system",
        "os.popen",
        "os.popen2",
        "os.popen3",
        "os.popen4",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.fork",
        "os.forkpty",
        "os.kill",
        "os.killpg",
        "os.setuid",
        "os.setgid",
        "os.chown",
        "os.chmod",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.removedirs",
        "os.truncate",
        "shutil.rmtree",
        "shutil.move",
        "shutil.chown",
        "sys.setrecursionlimit",
        "pathlib.Path.unlink",
        "importlib.import_module",
        "builtins.eval",
        "builtins.exec",
    }
)

#: Builtins whose entire purpose is to turn data into running code.
_DEFAULT_DENIED_BUILTINS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "execfile", "input", "breakpoint"}
)

#: Names that indicate an encode/decode round trip, which in combination with a
#: dynamic-execution call is the classic packer signature.
_OBFUSCATION_DECODERS: frozenset[str] = frozenset(
    {
        "b64decode",
        "b64encode",
        "b32decode",
        "b16decode",
        "a85decode",
        "b85decode",
        "decodebytes",
        "unhexlify",
        "decompress",
        "fromhex",
    }
)

#: Path fragments that are sensitive regardless of what is done with them.
_DEFAULT_SENSITIVE_PATTERNS: tuple[str, ...] = (
    r"/etc/passwd",
    r"/etc/shadow",
    r"/etc/sudoers",
    r"\.ssh[/\\]",
    r"id_rsa",
    r"id_ed25519",
    r"\.aws[/\\]credentials",
    r"\.kube[/\\]config",
    r"\.netrc",
    r"\.git[/\\]config",
    r"\.env$",
    r"[/\\]\.env$",
    r"C:\\Windows",
    r"[/\\]System32",
    r"SAM$",
    r"HKEY_LOCAL_MACHINE",
    r"HKEY_CURRENT_USER",
    r"[/\\]\.paa[/\\]",
    r"authorized_keys",
    r"shadow$",
)

_ABSOLUTE_POSIX = re.compile(r"^/[A-Za-z0-9_.\-]")
_ABSOLUTE_WINDOWS = re.compile(r"^[A-Za-z]:[\\/]")
_UNC = re.compile(r"^\\\\[A-Za-z0-9]")
_TRAVERSAL = re.compile(r"\.\.[/\\]")


class ScannerPolicy(BaseModel):
    """Configurable rule set.

    The allowlist wins over the denylist so an operator can re-enable a module
    for a specific trusted skill without editing the module-level constants —
    an escape valve that keeps the default deny list strict, because a strict
    default with no override gets loosened permanently by the first person it
    inconveniences.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    denied_modules: frozenset[str] = Field(default=_DEFAULT_DENIED_MODULES)
    allowed_modules: frozenset[str] = Field(default=frozenset())
    denied_attributes: frozenset[str] = Field(default=_DEFAULT_DENIED_ATTRIBUTES)
    denied_builtins: frozenset[str] = Field(default=_DEFAULT_DENIED_BUILTINS)
    sensitive_path_patterns: tuple[str, ...] = Field(default=_DEFAULT_SENSITIVE_PATTERNS)

    workspace_root: str | None = None
    """When set, an absolute path literal under this root is *not* flagged —
    the workload legitimately needs to name its own workspace."""

    max_string_literal_length: int = Field(default=512, ge=32)
    """Single-line string literals longer than this are an obfuscation signal.
    512 is comfortably above a long SQL statement or docstring line and well
    below a base64-packed payload."""

    blocking_severity: Severity = Severity.HIGH
    allow_absolute_paths: bool = False

    def is_module_denied(self, module: str) -> bool:
        """Whether ``module`` (or a package it belongs to) is denied.

        Checks every dotted prefix so ``import urllib.request`` is caught by
        the ``urllib`` entry — otherwise the deny list would need every
        submodule of every banned package enumerated, and would miss one.
        """
        parts = module.split(".")
        for index in range(len(parts), 0, -1):
            prefix = ".".join(parts[:index])
            if prefix in self.allowed_modules:
                return False
            if prefix in self.denied_modules:
                return True
        return False


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class AstSecurityScanner:
    """Deterministic AST-based rejection of dangerous Python.

    Stateless and reusable; :meth:`scan` builds a fresh visitor per call so the
    same instance is safe to share across concurrent validations.
    """

    def __init__(self, policy: ScannerPolicy | None = None) -> None:
        self._policy = policy or ScannerPolicy()
        self._sensitive = [
            re.compile(pattern, re.IGNORECASE) for pattern in self._policy.sensitive_path_patterns
        ]

    @property
    def policy(self) -> ScannerPolicy:
        return self._policy

    def scan(self, source: str, *, filename: str = "<artifact>") -> list[Finding]:
        """Analyse ``source`` and return every finding.

        Never raises for bad input — a syntax error becomes a ``CRITICAL``
        finding. See the module docstring for why that must not be a skip.
        """
        findings: list[Finding] = []

        # A NUL byte makes `compile()` refuse the source outright, and is a
        # known trick for desynchronising a scanner from an interpreter.
        if "\x00" in source:
            return [
                Finding(
                    rule="null_byte_in_source",
                    severity=Severity.CRITICAL,
                    lineno=1,
                    col=0,
                    snippet="<null byte>",
                    message="source contains a NUL byte and cannot be safely analysed",
                )
            ]

        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            # THE CRITICAL PATH. Unreadable source is rejected, never skipped.
            return [
                Finding(
                    rule="unparseable_source",
                    severity=Severity.CRITICAL,
                    lineno=exc.lineno or 1,
                    col=exc.offset or 0,
                    snippet=(exc.text or "").strip()[:200],
                    message=(
                        f"source could not be parsed ({exc.msg}); rejected because code "
                        "the scanner cannot analyse must never reach execution"
                    ),
                )
            ]
        except (ValueError, RecursionError, MemoryError) as exc:
            # Deeply nested literals can exhaust the parser's stack. Same rule.
            return [
                Finding(
                    rule="unparseable_source",
                    severity=Severity.CRITICAL,
                    lineno=1,
                    col=0,
                    snippet=type(exc).__name__,
                    message=f"source could not be parsed ({exc}); rejected rather than skipped",
                )
            ]

        lines = source.splitlines()
        visitor = _SecurityVisitor(self._policy, self._sensitive, lines)
        visitor.visit(tree)
        findings.extend(visitor.findings)
        findings.extend(visitor.finalize())

        findings.sort(key=lambda f: (-f.severity.rank, f.lineno, f.col))
        return findings

    def scan_or_raise(self, source: str, *, filename: str = "<artifact>") -> list[Finding]:
        """Scan and raise :class:`SecurityScanError` on any blocking finding.

        Returns the non-blocking findings so a caller that wants to log the
        smells it tolerated still can.
        """
        findings = self.scan(source, filename=filename)
        blocking = [
            f for f in findings if f.severity.rank >= self._policy.blocking_severity.rank
        ]
        if blocking:
            log.warning(
                "validation.ast_scan.rejected",
                filename=filename,
                blocking=len(blocking),
                rules=sorted({f.rule for f in blocking}),
            )
            raise SecurityScanError([f.to_payload() for f in blocking])
        return findings

    def scan_path(self, path: Path | str, **kwargs: Any) -> list[Finding]:
        """Scan a file on disk.

        An undecodable file is a ``CRITICAL`` finding for the same reason a
        syntax error is: we could not read it, so it does not run.
        """
        path = Path(path)
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            return [
                Finding(
                    rule="undecodable_source",
                    severity=Severity.CRITICAL,
                    lineno=1,
                    col=0,
                    snippet=path.name,
                    message=f"file is not valid UTF-8 ({exc.reason}); rejected unscanned",
                )
            ]
        except OSError as exc:
            return [
                Finding(
                    rule="unreadable_source",
                    severity=Severity.CRITICAL,
                    lineno=1,
                    col=0,
                    snippet=path.name,
                    message=f"file could not be read ({exc}); rejected unscanned",
                )
            ]
        return self.scan(source, filename=kwargs.pop("filename", str(path)), **kwargs)


class _SecurityVisitor(ast.NodeVisitor):
    """Walks the tree collecting findings. One instance per scan."""

    def __init__(
        self,
        policy: ScannerPolicy,
        sensitive: list[re.Pattern[str]],
        lines: list[str],
    ) -> None:
        self.policy = policy
        self.sensitive = sensitive
        self.lines = lines
        self.findings: list[Finding] = []

        #: alias -> real module, so ``import os as o; o.system(...)`` resolves.
        self.aliases: dict[str, str] = {}
        #: Names bound by ``from x import y``, mapped to their dotted origin.
        self.imported_names: dict[str, str] = {}

        self.saw_decoder = False
        self.saw_dynamic_exec = False
        self._decoder_node: ast.AST | None = None

    # -- helpers -----------------------------------------------------------

    def _snippet(self, node: ast.AST) -> str:
        lineno = getattr(node, "lineno", 0)
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1].strip()[:200]
        return ""

    def _add(self, node: ast.AST, rule: str, severity: Severity, message: str) -> None:
        self.findings.append(
            Finding(
                rule=rule,
                severity=severity,
                lineno=getattr(node, "lineno", 0),
                col=getattr(node, "col_offset", 0),
                snippet=self._snippet(node),
                message=message,
            )
        )

    def _dotted_name(self, node: ast.AST) -> str | None:
        """Resolve an attribute chain to a dotted string, following aliases."""
        parts: list[str] = []
        current: ast.AST = node
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            base = self.aliases.get(current.id, current.id)
            parts.append(base)
            return ".".join(reversed(parts))
        return None

    # -- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            if self.policy.is_module_denied(alias.name):
                self._add(
                    node,
                    "denied_import",
                    Severity.CRITICAL,
                    f"import of denied module {alias.name!r}",
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level > 0:
            # Relative imports stay inside the artifact and cannot reach a
            # denied stdlib module by name.
            self.generic_visit(node)
            return

        if self.policy.is_module_denied(module):
            self._add(
                node,
                "denied_import",
                Severity.CRITICAL,
                f"import from denied module {module!r}",
            )
        for alias in node.names:
            if alias.name == "*":
                self._add(
                    node,
                    "wildcard_import",
                    Severity.MEDIUM,
                    f"wildcard import from {module!r} hides what is actually bound",
                )
                continue
            dotted = f"{module}.{alias.name}" if module else alias.name
            bound = alias.asname or alias.name
            self.imported_names[bound] = dotted
            if dotted in self.policy.denied_attributes:
                self._add(
                    node,
                    "denied_attribute_import",
                    Severity.CRITICAL,
                    f"import of denied callable {dotted!r}",
                )
            if alias.name in _OBFUSCATION_DECODERS:
                self.saw_decoder = True
                self._decoder_node = node
        self.generic_visit(node)

    # -- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Name):
            self._check_name_call(node, func)
        elif isinstance(func, ast.Attribute):
            self._check_attribute_call(node, func)

        self._check_sensitive_open(node)
        self.generic_visit(node)

    def _check_name_call(self, node: ast.Call, func: ast.Name) -> None:
        name = func.id
        resolved = self.imported_names.get(name, name)

        if name in self.policy.denied_builtins:
            self.saw_dynamic_exec = True
            self._add(
                node,
                "dynamic_execution",
                Severity.CRITICAL,
                f"{name}() turns data into executing code",
            )
            self._check_decoder_argument(node)
            return

        if resolved in self.policy.denied_attributes:
            self._add(
                node,
                "denied_call",
                Severity.CRITICAL,
                f"call to denied callable {resolved!r}",
            )

        if name in {"getattr", "setattr", "delattr"}:
            self._check_reflection(node, name)

        if name in _OBFUSCATION_DECODERS:
            self.saw_decoder = True
            self._decoder_node = node

    def _check_attribute_call(self, node: ast.Call, func: ast.Attribute) -> None:
        dotted = self._dotted_name(func)
        if dotted is None:
            # e.g. ``obj.method()`` where obj is a local. Still catch the
            # decoder-name signal, which is what packers use.
            if func.attr in _OBFUSCATION_DECODERS:
                self.saw_decoder = True
                self._decoder_node = node
            return

        if dotted in self.policy.denied_attributes:
            self._add(
                node,
                "denied_call",
                Severity.CRITICAL,
                f"call to denied callable {dotted!r}",
            )
        root = dotted.split(".")[0]
        if self.policy.is_module_denied(root) or self.policy.is_module_denied(dotted):
            self._add(
                node,
                "denied_module_use",
                Severity.CRITICAL,
                f"use of denied module via {dotted!r}",
            )
        if func.attr in _OBFUSCATION_DECODERS:
            self.saw_decoder = True
            self._decoder_node = node

    def _check_reflection(self, node: ast.Call, name: str) -> None:
        """Flag reflective attribute access used to dodge static analysis.

        A literal ``getattr(x, "width")`` is ordinary code and is left alone.
        Two things are not:

        * a **non-literal** name — ``getattr(os, some_var)`` — which is
          precisely how a denied call is reconstructed at runtime out of
          fragments the scanner never sees as one string;
        * a **dunder** name — ``getattr(x, "__globals__")`` — the standard
          route from any function object to ``__builtins__`` and from there to
          ``eval``, no denied import required.
        """
        if len(node.args) < 2:
            return
        target = node.args[1]
        if isinstance(target, ast.Constant) and isinstance(target.value, str):
            if target.value.startswith("__"):
                self._add(
                    node,
                    "reflective_dunder_access",
                    Severity.HIGH,
                    f"{name}() reaching a dunder attribute {target.value!r} "
                    "is a standard sandbox-escape primitive",
                )
            return
        self._add(
            node,
            "dynamic_attribute_access",
            Severity.HIGH,
            f"{name}() with a computed attribute name defeats static analysis",
        )

    def _check_decoder_argument(self, node: ast.Call) -> None:
        """``exec(b64decode(...))`` — the packed-payload signature, directly."""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fname = (
                    sub.func.attr
                    if isinstance(sub.func, ast.Attribute)
                    else (sub.func.id if isinstance(sub.func, ast.Name) else "")
                )
                if fname in _OBFUSCATION_DECODERS:
                    self._add(
                        node,
                        "obfuscated_execution",
                        Severity.CRITICAL,
                        f"decoded payload ({fname}) passed directly to a dynamic "
                        "execution call — the canonical packer pattern",
                    )
                    return

    def _check_sensitive_open(self, node: ast.Call) -> None:
        """``open(<sensitive>, "w")`` and friends."""
        func = node.func
        is_open = (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr in {"open", "write_text", "write_bytes"}
        )
        if not is_open or not node.args:
            return
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            return

        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value)

        writing = any(ch in mode for ch in "wax+") or (
            isinstance(func, ast.Attribute) and func.attr.startswith("write_")
        )
        if writing and self._is_sensitive(first.value):
            self._add(
                node,
                "sensitive_path_write",
                Severity.CRITICAL,
                f"write to sensitive path {first.value!r}",
            )

    # -- literals ----------------------------------------------------------

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._check_string_literal(node, node.value)
        self.generic_visit(node)

    def _check_string_literal(self, node: ast.Constant, value: str) -> None:
        if _TRAVERSAL.search(value):
            self._add(
                node,
                "path_traversal",
                Severity.HIGH,
                f"path traversal segment in literal {value[:80]!r}",
            )
        elif self._is_absolute(value) and not self.policy.allow_absolute_paths:
            if not self._under_workspace(value):
                self._add(
                    node,
                    "absolute_path_literal",
                    Severity.HIGH,
                    f"absolute path {value[:80]!r} points outside the workspace",
                )

        if self._is_sensitive(value):
            self._add(
                node,
                "sensitive_path_reference",
                Severity.HIGH,
                f"reference to a sensitive path {value[:80]!r}",
            )

        # Long single-line literals: a packed payload has no newlines because
        # it is one base64/hex blob. Multi-line strings of the same length are
        # ordinary embedded templates, so newline-free is the discriminator
        # that keeps the false-positive rate survivable.
        if len(value) > self.policy.max_string_literal_length and "\n" not in value:
            self._add(
                node,
                "long_literal",
                Severity.MEDIUM,
                f"single-line string literal of {len(value)} chars "
                f"(limit {self.policy.max_string_literal_length}) suggests a packed payload",
            )

    @staticmethod
    def _is_absolute(value: str) -> bool:
        return bool(
            _ABSOLUTE_POSIX.match(value) or _ABSOLUTE_WINDOWS.match(value) or _UNC.match(value)
        )

    def _under_workspace(self, value: str) -> bool:
        root = self.policy.workspace_root
        if not root:
            return False
        normalised = value.replace("\\", "/").lower()
        return normalised.startswith(root.replace("\\", "/").lower())

    def _is_sensitive(self, value: str) -> bool:
        return any(pattern.search(value) for pattern in self.sensitive)

    # -- whole-file signals ------------------------------------------------

    def finalize(self) -> list[Finding]:
        """Findings that need the whole file to be visible.

        The decode-then-execute pattern is frequently split across statements —
        ``blob = b64decode(...)`` on one line, ``exec(blob)` fifty lines later —
        so a per-node check cannot see it. Co-occurrence at file scope can.
        """
        extra: list[Finding] = []
        if self.saw_decoder and self.saw_dynamic_exec:
            node = self._decoder_node
            extra.append(
                Finding(
                    rule="obfuscation_signal",
                    severity=Severity.CRITICAL,
                    lineno=getattr(node, "lineno", 1),
                    col=getattr(node, "col_offset", 0),
                    snippet=self._snippet(node) if node else "",
                    message=(
                        "file contains both a decode primitive and a dynamic-execution "
                        "call; decode-then-execute is the standard payload packer"
                    ),
                )
            )
        return extra
