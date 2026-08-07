"""Claw-Hub / OpenClaw skill directories — ``provider='claw_hub'``.

An OpenClaw skill is a *directory*, not a package: a ``SKILL.md`` describing the
capability plus one or more Python scripts that implement it. The runtime meets
these skills where they live — downloaded from Claw Hub or dropped in by a user —
so this adapter's job is to turn that on-disk convention into the runtime's
in-memory contract, and to run the scripts under containment when asked.

``SKILL.md`` structure
----------------------
The file is (optionally) YAML frontmatter fenced by ``---`` lines, followed by a
markdown body::

    ---
    name: summarise-inbox
    description: Summarise the last N emails into a short digest.
    version: 1.2.0
    permissions: [PERM_SANDBOX_RUN, PERM_NET_EGRESS]
    input_schema: {"type": "object", "properties": {"n": {"type": "integer"}}}
    output_schema: {"type": "object"}
    ---
    You are an inbox summariser. ...

The **frontmatter** builds the :class:`~paa.skills.contracts.SkillContract`; the
**body** becomes the system-prompt wrapper carried on
:attr:`~paa.skills.contracts.SkillInvocation.system_prompt`.

No YAML dependency
------------------
``PyYAML`` is not a declared dependency (see ``pyproject.toml`` — heavyweight
deps are opt-in), and a security-relevant parse must not depend on whether an
optional package happens to be installed. So the frontmatter is parsed by a
small indentation reader here that covers the subset skills actually use —
scalars, block lists, block maps, and inline JSON flow collections — and refuses
loudly on anything it does not understand rather than guessing.

Containment
-----------
:meth:`ClawHubAdapter.invoke` runs the entrypoint under the supplied sandbox with
the skill directory declared as a **read-only mount** (RFC §8.2: a skill's own
scripts are code, and code that can rewrite itself between the security scan and
execution defeats the scan). Arguments are delivered through a JSON file in the
writable workspace whose path is passed in ``argv`` — **never** through the child
environment, which would leak them into ``/proc/<pid>/environ`` and every
grandchild process.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.core.errors import SkillContractError
from paa.core.types import Permission
from paa.skills.adapters.base import SkillAdapter, SkillResult
from paa.skills.contracts import SkillContract, SkillInvocation

if TYPE_CHECKING:
    from paa.sandbox.base import Sandbox
    from paa.skills.adapters.base import SecretProvider

__all__ = ["ClawHubAdapter", "parse_skill_md"]

log = structlog.get_logger(__name__)

_SKILL_FILE = "SKILL.md"
_FENCE = "---"
_DEFAULT_OBJECT_SCHEMA: dict[str, Any] = {"type": "object"}
#: Candidate entrypoint names, in preference order, when frontmatter is silent.
_ENTRYPOINT_CANDIDATES = ("main.py", "skill.py", "run.py", "__main__.py")
#: argv flag the entrypoint reads its arguments-file path from.
_ARGS_FLAG = "--paa-args"


# ---------------------------------------------------------------------------
# SKILL.md parsing
# ---------------------------------------------------------------------------


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Split ``SKILL.md`` into ``(frontmatter, body)``.

    A file with no leading ``---`` fence is all body and empty frontmatter — a
    perfectly valid skill that simply declares nothing structured. A file that
    *opens* a fence but never closes it is malformed and raises, because silently
    treating an unterminated fence as body would hide an authoring error.

    :raises SkillContractError: on an unterminated frontmatter fence or
        unparseable frontmatter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text.strip()

    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            closing = index
            break
    if closing is None:
        raise SkillContractError(
            "SKILL.md frontmatter opens with '---' but is never closed",
        )

    frontmatter_lines = lines[1:closing]
    body = "\n".join(lines[closing + 1 :]).strip()
    try:
        frontmatter = _parse_yaml_block(frontmatter_lines, 0)[0]
    except (ValueError, json.JSONDecodeError) as exc:
        raise SkillContractError(
            "SKILL.md frontmatter is not parseable", detail=str(exc)
        ) from exc
    if not isinstance(frontmatter, dict):
        raise SkillContractError("SKILL.md frontmatter must be a mapping")
    return frontmatter, body


def _significant(lines: list[str]) -> list[tuple[int, str]]:
    """Drop blanks and comments; return ``(indent, stripped_content)`` per line."""
    out: list[tuple[int, str]] = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        out.append((indent, stripped))
    return out


def _parse_yaml_block(lines: list[str], _base: int) -> tuple[Any, int]:
    """Parse an indentation block into a Python value.

    Handles the subset OpenClaw skills use: block maps (``key: value``), block
    sequences (``- item``), scalars, and inline JSON flow collections for the
    nested schemas. Anything else raises ``ValueError`` — an unparseable
    frontmatter must fail, not be half-read.
    """
    return _parse_lines(_significant(lines), 0)


def _parse_lines(items: list[tuple[int, str]], start: int) -> tuple[Any, int]:
    if start >= len(items):
        return {}, start
    indent = items[start][0]
    if items[start][1].startswith("- "):
        return _parse_sequence(items, start, indent)
    return _parse_mapping(items, start, indent)


def _parse_mapping(items: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
    result: dict[str, Any] = {}
    index = start
    while index < len(items):
        cur_indent, content = items[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise ValueError(f"unexpected indentation in frontmatter: {content!r}")
        if content.startswith("- "):
            raise ValueError(f"sequence item where a mapping key was expected: {content!r}")
        if ":" not in content:
            raise ValueError(f"frontmatter line is not 'key: value': {content!r}")

        key, _, rest = content.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            result[key] = _parse_scalar(rest)
            index += 1
        else:
            # Nested block belongs to this key: everything more-indented below.
            child, index = _parse_lines(items, index + 1)
            result[key] = child
    return result, index


def _parse_sequence(items: list[tuple[int, str]], start: int, indent: int) -> tuple[Any, int]:
    result: list[Any] = []
    index = start
    while index < len(items):
        cur_indent, content = items[index]
        if cur_indent < indent or not content.startswith("- "):
            break
        if cur_indent > indent:
            raise ValueError(f"unexpected indentation in frontmatter list: {content!r}")
        result.append(_parse_scalar(content[2:].strip()))
        index += 1
    return result, index


def _parse_scalar(token: str) -> Any:
    """Coerce a scalar token.

    Inline flow collections (``[...]`` / ``{...}``) are tried as JSON first —
    ``{"type": "object"}`` written verbatim in frontmatter should round-trip
    exactly — and fall back to a tolerant YAML flow reader for the unquoted form
    (``[PERM_SANDBOX_RUN, PERM_NET_EGRESS]``) that JSON rejects.
    """
    if not token:
        return None
    if token[0] in "[{":
        try:
            return json.loads(token)
        except json.JSONDecodeError:
            return _parse_flow(token)
    if (token[0] == token[-1]) and token[0] in "\"'" and len(token) >= 2:
        return token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "~"):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def _parse_flow(token: str) -> Any:
    """Parse an unquoted YAML flow list ``[a, b]`` or map ``{k: v}``.

    Deliberately shallow: it splits on top-level commas and recurses into each
    element via :func:`_parse_scalar`, which covers the flat lists (permissions,
    env allowlists) skills actually put on one line. A genuinely nested flow
    collection should be written as JSON, which the caller tries first.
    """
    inner = token[1:-1].strip()
    if not inner:
        return {} if token[0] == "{" else []
    parts = _split_top_level(inner)
    if token[0] == "{":
        result: dict[str, Any] = {}
        for part in parts:
            key, _, value = part.partition(":")
            result[_parse_scalar(key.strip())] = _parse_scalar(value.strip())
        return result
    return [_parse_scalar(part.strip()) for part in parts]


def _split_top_level(text: str) -> list[str]:
    """Split ``text`` on commas that are not inside a nested ``[]``/``{}``."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [p for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class ClawHubAdapter(SkillAdapter):
    """Discovers and runs OpenClaw skill directories rooted at ``root``.

    ``root`` may be a single skill directory (contains ``SKILL.md``) or a hub
    holding several skill subdirectories. Discovery finds ``SKILL.md`` at the
    root and one level below, which is the layout ``claw pull`` produces.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def provider(self) -> str:
        return "claw_hub"

    async def discover(self) -> list[SkillContract]:
        """Build a contract for every skill directory under ``root``.

        A directory whose ``SKILL.md`` is malformed is skipped with a warning
        rather than aborting discovery of its siblings — one bad download must
        not hide every other skill in the hub.
        """
        contracts: list[SkillContract] = []
        for skill_dir in self._skill_dirs():
            try:
                contracts.append(self._build_contract(skill_dir))
            except SkillContractError as exc:
                log.warning(
                    "skills.claw_hub.skipped", directory=str(skill_dir), detail=str(exc)
                )
        return contracts

    def _skill_dirs(self) -> list[Path]:
        found: list[Path] = []
        if (self._root / _SKILL_FILE).is_file():
            found.append(self._root)
        if self._root.is_dir():
            for child in sorted(self._root.iterdir()):
                if child.is_dir() and (child / _SKILL_FILE).is_file():
                    found.append(child)
        return found

    def build_contract(self, skill_dir: Path | str) -> SkillContract:
        """Public single-directory contract builder (used by tests and tooling)."""
        return self._build_contract(Path(skill_dir))

    def _build_contract(self, skill_dir: Path) -> SkillContract:
        skill_file = skill_dir / _SKILL_FILE
        if not skill_file.is_file():
            raise SkillContractError(
                "skill directory has no SKILL.md", detail=str(skill_dir)
            )
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillContractError("SKILL.md is unreadable", detail=str(exc)) from exc

        frontmatter, body = parse_skill_md(text)
        entrypoint = self._find_entrypoint(skill_dir, frontmatter)

        name = _normalise_name(str(frontmatter.get("name") or skill_dir.name))
        description = self._resolve_description(frontmatter, body)
        permissions = self._resolve_permissions(frontmatter)

        invocation = SkillInvocation(
            kind="python_entrypoint",
            target=entrypoint.name,
            working_dir=str(skill_dir.resolve()),
            entrypoint_file=entrypoint.name,
            system_prompt=body or None,
            timeout_seconds=_opt_float(frontmatter.get("timeout_seconds")),
            env_allowlist=_str_tuple(frontmatter.get("env_allowlist")),
        )

        return SkillContract.parse(
            {
                "skill_name": name,
                "provider": "claw_hub",
                "version": str(frontmatter.get("version") or "0.1.0"),
                "description": description,
                "input_schema": _schema(frontmatter.get("input_schema")),
                "output_schema": _schema(frontmatter.get("output_schema")),
                "risk_profile": _opt_float(frontmatter.get("risk_profile"), default=0.5),
                "required_permissions": permissions,
                "invocation": invocation.model_dump(mode="json"),
                "source_uri": frontmatter.get("source_uri") or skill_dir.as_uri(),
            }
        )

    def _resolve_description(self, frontmatter: dict[str, Any], body: str) -> str:
        candidate = frontmatter.get("description")
        if isinstance(candidate, str) and len(candidate.strip()) >= 20:
            return candidate.strip()
        # Fall back to the first substantial line of the body — a skill with a
        # real prompt but no explicit description is still describable.
        for line in body.splitlines():
            if len(line.strip()) >= 20:
                return line.strip()
        raise SkillContractError(
            "SKILL.md has no usable description (need >= 20 characters in "
            "frontmatter 'description' or the body)"
        )

    def _resolve_permissions(self, frontmatter: dict[str, Any]) -> list[str]:
        declared = _str_tuple(frontmatter.get("permissions"))
        # A sandboxed skill always needs PERM_SANDBOX_RUN; add it so an author
        # who forgets does not produce a contract that can never be dispatched.
        perms = list(declared)
        if Permission.SANDBOX_RUN.value not in perms:
            perms.insert(0, Permission.SANDBOX_RUN.value)
        return perms

    def _find_entrypoint(self, skill_dir: Path, frontmatter: dict[str, Any]) -> Path:
        declared = frontmatter.get("entrypoint")
        if isinstance(declared, str) and declared:
            candidate = skill_dir / declared
            if not candidate.is_file():
                raise SkillContractError(
                    "declared entrypoint does not exist", detail=declared
                )
            return candidate
        for name in _ENTRYPOINT_CANDIDATES:
            if (skill_dir / name).is_file():
                return skill_dir / name
        py_files = sorted(p for p in skill_dir.glob("*.py") if p.name != "__init__.py")
        if len(py_files) == 1:
            return py_files[0]
        raise SkillContractError(
            "could not identify a Python entrypoint; declare 'entrypoint' in "
            "SKILL.md frontmatter",
            detail=f"{len(py_files)} candidate .py files",
        )

    async def invoke(
        self,
        contract: SkillContract,
        arguments: dict[str, Any],
        *,
        sandbox: Sandbox | None,
        timeout: float | None,  # noqa: ASYNC109 - deliberate per-call budget in the adapter API
        secret_broker: SecretProvider | None = None,
    ) -> SkillResult:
        """Run the entrypoint under ``sandbox`` and capture its stdout as JSON.

        The skill directory is declared read-only; arguments are written to a
        JSON file in the writable workspace and the path is passed in ``argv``.
        The entrypoint is expected to print a single JSON object to stdout — that
        object is the skill's output. Non-JSON stdout is a skill-level failure
        (``ok=False``), not an adapter crash.
        """
        if sandbox is None:
            raise SkillContractError(
                "claw_hub skills require a sandbox; none was provided",
                skill_name=contract.skill_name,
            )
        inv = contract.invocation
        if inv.working_dir is None or inv.entrypoint_file is None:
            raise SkillContractError(
                "claw_hub contract is missing working_dir/entrypoint_file",
                skill_name=contract.skill_name,
            )

        from paa.sandbox.base import SandboxSpec

        skill_dir = Path(inv.working_dir)
        entrypoint = skill_dir / inv.entrypoint_file

        # A dedicated writable scratch dir next to the skill; the skill dir
        # itself is mounted read-only.
        workspace = skill_dir.parent / f".paa_run_{uuid.uuid4().hex}"
        workspace.mkdir(parents=True, exist_ok=True)
        args_file = workspace / "arguments.json"
        args_file.write_text(json.dumps(arguments), encoding="utf-8")

        effective_timeout = timeout if timeout is not None else inv.timeout_seconds
        spec = SandboxSpec(
            command=(sys.executable, str(entrypoint), _ARGS_FLAG, str(args_file)),
            workspace_path=workspace,
            read_only_mounts=(skill_dir.resolve(),),
            timeout_seconds=effective_timeout,
            allow_network=Permission.NET_EGRESS in contract.required_permissions,
        )

        started = time.perf_counter()
        try:
            result = await sandbox.run(spec)
        finally:
            _cleanup(workspace)
        latency_ms = (time.perf_counter() - started) * 1000.0

        if not result.ok:
            reason = result.killed_reason or ("timeout" if result.timed_out else "nonzero exit")
            return SkillResult(
                ok=False,
                stdout=result.stdout,
                stderr=result.stderr,
                error=f"claw_hub skill failed ({reason})",
                latency_ms=latency_ms,
                exit_code=result.exit_code,
            )

        output = _parse_output(result.stdout)
        if output is None:
            return SkillResult(
                ok=False,
                stdout=result.stdout,
                stderr=result.stderr,
                error="claw_hub skill did not emit a single JSON object on stdout",
                latency_ms=latency_ms,
                exit_code=result.exit_code,
            )
        return SkillResult(
            ok=True,
            output=output,
            stdout=result.stdout,
            stderr=result.stderr,
            latency_ms=latency_ms,
            exit_code=result.exit_code,
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_output(stdout: str) -> dict[str, Any] | None:
    """Extract the skill's JSON object from stdout.

    Tolerates leading/trailing log noise by trying the whole payload first, then
    the last non-empty line — skills commonly print diagnostics before the final
    result line. Returns ``None`` if no JSON *object* can be recovered.
    """
    text = stdout.strip()
    if not text:
        return None
    for candidate in (text, text.splitlines()[-1].strip()):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _cleanup(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _normalise_name(raw: str) -> str:
    """Coerce a directory/frontmatter name toward ``SKILL_NAME_PATTERN``.

    Lower-cases and turns whitespace into hyphens, because the contract pattern
    forbids uppercase and spaces (see :data:`paa.skills.contracts.SKILL_NAME_PATTERN`).
    A name that still fails the pattern is left as-is so ``SkillContract.parse``
    raises a precise error rather than this silently mangling it further.
    """
    return "-".join(raw.strip().lower().split())


def _schema(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return dict(_DEFAULT_OBJECT_SCHEMA)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _opt_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
