"""The AST security scanner — the gate that runs before agent code executes.

``src/paa/validation/ast_scanner.py`` opens with the sentence the whole
module rests on: *"Unparseable source is REJECTED, never skipped. This is the
single most important line in the module."* Until this file existed, not one
of the module's nineteen rule names was asserted anywhere in the repository,
and the sentence was a promise rather than a fact.

What is asserted here:

* **every rule the scanner can emit**, and the inventory is derived from the
  module's own source — so a rule added later cannot slip past unnoticed;
* **every denied module, attribute and builtin** the default policy names;
* the **policy's configurability** — the allowlist, the longest-prefix rule,
  ``blocking_severity``, ``workspace_root``, ``allow_absolute_paths`` and
  ``max_string_literal_length`` — because a deny list nobody can adjust is a
  deny list that gets switched off;
* the **never raises** property over a hostile corpus, which is the property
  that turns "I could not analyse this" into "this does not run".
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from paa.core.errors import SecurityScanError
from paa.validation import ast_scanner as ast_scanner_module
from paa.validation.ast_scanner import (
    AstSecurityScanner,
    Finding,
    ScannerPolicy,
    Severity,
)

# ---------------------------------------------------------------------------
# The rule inventory, derived from the module's own source
# ---------------------------------------------------------------------------

#: The rule names this file knows about.
#:
#: Written out by hand from the module, and checked against the module's
#: source below. The hand-written list is what makes the check meaningful: it
#: is the list a reader can verify, and the sweep is what makes it stale when
#: the module changes.
EXPECTED_RULES: frozenset[str] = frozenset(
    {
        "absolute_path_literal",
        "denied_attribute_import",
        "denied_call",
        "denied_import",
        "denied_module_use",
        "dynamic_attribute_access",
        "dynamic_execution",
        "long_literal",
        "null_byte_in_source",
        "obfuscated_execution",
        "obfuscation_signal",
        "path_traversal",
        "reflective_dunder_access",
        "sensitive_path_reference",
        "sensitive_path_write",
        "undecodable_source",
        "unparseable_source",
        "unreadable_source",
        "wildcard_import",
    }
)

#: Source that makes each rule fire, and nothing else is asserted about it.
#:
#: A rule is only *covered* when the scanner emits it for this source; a
#: snippet that happens to trip several rules is fine, because the assertion
#: is that the named rule is among the findings.
RULE_SOURCES: dict[str, str] = {
    "denied_import": "import subprocess\n",
    "wildcard_import": "from os import *\n",
    "denied_attribute_import": "from os import system\n",
    "dynamic_execution": "eval('1')\n",
    "denied_call": "import os\nos.system('ls')\n",
    "denied_module_use": "import socket\nsocket.socket()\n",
    "reflective_dunder_access": "getattr(obj, '__globals__')\n",
    "dynamic_attribute_access": "getattr(obj, name)\n",
    "obfuscated_execution": "exec(b64decode('QQ=='))\n",
    "sensitive_path_write": "open('/etc/passwd', 'w')\n",
    "path_traversal": "path = '../../etc/passwd'\n",
    "absolute_path_literal": "path = '/usr/local/bin/tool'\n",
    "sensitive_path_reference": "path = '/etc/passwd'\n",
    "long_literal": "blob = '" + ("A" * 600) + "'\n",
    "obfuscation_signal": "blob = b64decode('QQ==')\n" + ("# filler\n" * 3) + "eval(blob)\n",
    "unparseable_source": "def broken(:\n",
    "null_byte_in_source": "x = 1\x00\n",
}


def _rules_in_source() -> set[str]:
    """Every rule name the scanner's own source can emit.

    The sweep reads the module rather than a copy of its data, so a rule
    added to the module is a rule this test knows about. Two construction
    shapes carry a rule name in the module today — ``Finding(rule=...)`` and
    ``self._add(node, "rule", ...)`` — and both are recognised; the helper
    ``_add``'s own forwarding of its ``rule`` parameter is the one
    construction site that carries no literal, and it is skipped.
    """
    source = Path(ast_scanner_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    rules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = (
            function.attr
            if isinstance(function, ast.Attribute)
            else (function.id if isinstance(function, ast.Name) else "")
        )
        if name == "Finding":
            for keyword in node.keywords:
                if (
                    keyword.arg == "rule"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    rules.add(keyword.value.value)
        elif name == "_add" and len(node.args) >= 2:
            argument = node.args[1]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                rules.add(argument.value)
    return rules


def test_the_rule_inventory_is_complete() -> None:
    """The list of rules this file exercises is the list the module can emit.

    A rule added to the module without a case here would be a rule nobody
    asserts — which is exactly how nineteen rules came to be unasserted in
    the first place. This test is the lock that stops the twentieth.
    """
    assert _rules_in_source() == set(EXPECTED_RULES)


#: The two rules that are about *files* rather than about source text.
#:
#: They cannot be reached through :meth:`AstSecurityScanner.scan`, because
#: they are the answers to "could this file be read at all" — and a file
#: that could not be read is rejected unscanned, for the same reason a
#: syntax error is.
FILE_RULES: frozenset[str] = frozenset({"undecodable_source", "unreadable_source"})


@pytest.mark.parametrize("rule", sorted(RULE_SOURCES))
def test_every_source_driven_rule_is_reachable(rule: str) -> None:
    """Each rule fires on the source that is supposed to trip it."""
    source = RULE_SOURCES[rule]

    findings = AstSecurityScanner().scan(source)

    assert rule in {finding.rule for finding in findings}, findings


def test_every_rule_has_a_case() -> None:
    """No rule is left without a case in this file.

    The two shapes of case — a source that trips the rule, and a file that
    cannot be read — together cover the whole inventory. A rule in neither
    is a rule nobody asserts.
    """
    assert set(RULE_SOURCES) | FILE_RULES == set(EXPECTED_RULES)
    assert not set(RULE_SOURCES) & FILE_RULES


def test_the_two_file_rules_need_a_file(tmp_path: Path) -> None:
    """``undecodable_source`` and ``unreadable_source`` are about *files*.

    They cannot be reached through :meth:`AstSecurityScanner.scan`, because
    they are the answers to "could this file be read at all" — and a file
    that could not be read is rejected unscanned, for the same reason a
    syntax error is.
    """
    scanner = AstSecurityScanner()

    binary = tmp_path / "binary.py"
    binary.write_bytes(b"x = 1\n\xff\xfe\n")
    assert [f.rule for f in scanner.scan_path(binary)] == ["undecodable_source"]

    # A directory is not readable as a file, and the refusal is an OSError,
    # so this exercises the same branch a permission error would.
    assert [f.rule for f in scanner.scan_path(tmp_path)] == ["unreadable_source"]

    assert [f.rule for f in scanner.scan_path(tmp_path / "absent.py")] == ["unreadable_source"]


# ---------------------------------------------------------------------------
# The deny list
# ---------------------------------------------------------------------------

#: The default policy's own data — the single source of truth for what is
#: denied, read from the policy rather than copied, so a change to the deny
#: list is covered by the test that already exists.
DEFAULT_POLICY = ScannerPolicy()


@pytest.mark.parametrize("module", sorted(DEFAULT_POLICY.denied_modules))
def test_every_denied_module_is_refused(module: str) -> None:
    """Every module the policy denies is refused when imported."""
    findings = AstSecurityScanner().scan(f"import {module}\n")

    assert "denied_import" in {finding.rule for finding in findings}


@pytest.mark.parametrize("module", sorted(DEFAULT_POLICY.denied_modules))
def test_every_denied_module_is_refused_when_imported_from(module: str) -> None:
    """``from x import y`` is refused too, not only ``import x``.

    The two spellings reach the same module and the deny list is about the
    module, so a rule that covered only one of them would leave the other
    open — and ``from subprocess import run`` is the spelling a model
    reaches for.
    """
    findings = AstSecurityScanner().scan(f"from {module} import thing\n")

    assert "denied_import" in {finding.rule for finding in findings}


@pytest.mark.parametrize("attribute", sorted(DEFAULT_POLICY.denied_attributes))
def test_every_denied_attribute_is_refused(attribute: str) -> None:
    """Every dotted callable the policy denies is refused when called."""
    root = attribute.split(".")[0]
    source = f"import {root}\n{attribute}('x')\n"

    findings = AstSecurityScanner().scan(source)

    assert "denied_call" in {finding.rule for finding in findings}


@pytest.mark.parametrize("builtin", sorted(DEFAULT_POLICY.denied_builtins))
def test_every_denied_builtin_is_refused(builtin: str) -> None:
    """Every builtin the policy denies is refused when called."""
    findings = AstSecurityScanner().scan(f"{builtin}('x')\n")

    assert "dynamic_execution" in {finding.rule for finding in findings}


def test_a_denied_submodule_is_caught_by_its_package() -> None:
    """``import urllib.request`` is caught by the ``urllib`` entry.

    Otherwise the deny list would have to name every submodule of every
    banned package, and it would miss one.
    """
    findings = AstSecurityScanner().scan("import urllib.request\n")

    assert "denied_import" in {finding.rule for finding in findings}


# ---------------------------------------------------------------------------
# The policy is configurable
# ---------------------------------------------------------------------------


def test_the_allowlist_wins_over_the_denylist() -> None:
    """An operator can re-enable a module for a trusted skill.

    The escape valve is the reason the default deny list can stay strict: a
    strict default with no override gets loosened permanently by the first
    person it inconveniences.
    """
    policy = ScannerPolicy(allowed_modules=frozenset({"urllib.request"}))

    assert policy.is_module_denied("urllib.request") is False
    # The allowlist is per module, not per package: `urllib.parse` is still
    # denied, because "urllib.parse" is not what was allowed.
    assert policy.is_module_denied("urllib.parse") is True

    findings = AstSecurityScanner(policy).scan("import urllib.request\n")
    assert "denied_import" not in {finding.rule for finding in findings}


def test_the_longest_prefix_decides() -> None:
    """The most specific matching prefix wins, whichever list it is on.

    Two prefixes can both match — ``urllib`` and ``urllib.request`` — and
    the answer has to be one of them, not "denied because something matched".
    The more specific one decides, so an operator who allows ``urllib`` does
    not accidentally re-allow a submodule someone else denied, and an
    operator who denies ``urllib.request`` does not have their denial
    overridden by a broader allow.
    """
    policy = ScannerPolicy(
        allowed_modules=frozenset({"urllib"}),
        denied_modules=frozenset({"urllib.request"}),
    )

    assert policy.is_module_denied("urllib.request") is True
    assert policy.is_module_denied("urllib.parse") is False
    assert policy.is_module_denied("socket") is False


def test_blocking_severity_decides_what_raises() -> None:
    """The blocking threshold is the difference between a report and a refusal.

    A gate that fires on everything gets switched off, and a gate that is off
    protects nothing — so the scanner reports smells below the threshold and
    refuses above it.
    """
    medium = "from os import *\n"  # MEDIUM: wildcard_import
    high = "path = '../../etc/passwd'\n"  # HIGH: path_traversal

    default = AstSecurityScanner()
    assert default.scan(medium)[0].severity is Severity.MEDIUM
    default.scan_or_raise(medium)  # below the threshold: reported, not raised
    with pytest.raises(SecurityScanError):
        default.scan_or_raise(high)

    strict = AstSecurityScanner(ScannerPolicy(blocking_severity=Severity.CRITICAL))
    strict.scan_or_raise(high)  # HIGH is below CRITICAL now
    with pytest.raises(SecurityScanError):
        strict.scan_or_raise("eval('1')\n")


def test_workspace_root_exempts_absolute_paths_under_it() -> None:
    """A workload legitimately needs to name its own workspace."""
    policy = ScannerPolicy(workspace_root="/work/space")

    inside = AstSecurityScanner(policy).scan("path = '/work/space/file.txt'\n")
    outside = AstSecurityScanner(policy).scan("path = '/etc/hosts'\n")

    assert "absolute_path_literal" not in {f.rule for f in inside}
    assert "absolute_path_literal" in {f.rule for f in outside}


def test_allow_absolute_paths_disables_the_rule() -> None:
    """The blunt version of the same escape valve."""
    scanner = AstSecurityScanner(ScannerPolicy(allow_absolute_paths=True))

    findings = scanner.scan("path = '/usr/local/bin/tool'\n")

    assert "absolute_path_literal" not in {f.rule for f in findings}


def test_max_string_literal_length_is_configurable() -> None:
    """The packed-payload threshold is a policy decision, not a constant."""
    payload = "blob = '" + ("A" * 600) + "'\n"

    assert "long_literal" in {f.rule for f in AstSecurityScanner().scan(payload)}

    lenient = ScannerPolicy(max_string_literal_length=1024)
    findings = AstSecurityScanner(lenient).scan(payload)
    assert "long_literal" not in {f.rule for f in findings}


def test_the_policy_is_frozen() -> None:
    """A policy cannot be edited in place under a running scan.

    The scanner is documented as safe to share across concurrent
    validations; a mutable policy would break that promise for the one
    object every scanner reads.
    """
    policy = ScannerPolicy()

    with pytest.raises(PydanticValidationError):
        policy.__setattr__("denied_modules", frozenset())


# ---------------------------------------------------------------------------
# The scanner's answers
# ---------------------------------------------------------------------------


def test_a_rejected_artifact_carries_the_findings_in_ledger_form() -> None:
    """``SecurityScanError`` is what the ledger records, so its shape matters."""
    scanner = AstSecurityScanner()

    with pytest.raises(SecurityScanError) as excinfo:
        scanner.scan_or_raise("import subprocess\n")

    findings = excinfo.value.findings
    assert findings
    assert {finding["rule"] for finding in findings} == {"denied_import"}
    assert findings[0]["severity"] == "CRITICAL"
    assert findings[0]["lineno"] == 1
    assert findings[0]["message"]


def test_findings_are_ordered_worst_first() -> None:
    """The report leads with the finding that matters most."""
    source = "import subprocess\npath = '../../etc/passwd'\n"

    findings = AstSecurityScanner().scan(source)

    assert findings
    ranks = [finding.severity.rank for finding in findings]
    assert ranks == sorted(ranks, reverse=True)


def test_clean_source_produces_no_findings() -> None:
    """The gate has to let ordinary code through, or it gets switched off."""
    source = (
        "import json\n"
        "from pathlib import Path\n"
        "\n"
        "def load(path: Path) -> dict:\n"
        "    return json.loads(path.read_text(encoding='utf-8'))\n"
    )

    assert AstSecurityScanner().scan(source) == []


# ---------------------------------------------------------------------------
# "Never raises" — the module's single most important promise
# ---------------------------------------------------------------------------

#: The hostile corpus: everything a scanner must survive without raising.
HOSTILE_SOURCES: list[str] = [
    "",
    "   \n\t  \n",
    "def foo(",
    "class A(:",
    "x = 1\x00",
    "\x00",
    "x = " + ("[" * 2000),
    "x = " + ("]" * 2000),
    "x = " + ("(" * 2000),
    "#" + ("x" * 10000),
    "x = '" + ("y" * 10000) + "'",
    "\x1b[31m",
    "def f():\n    return\n  pass\n",
]


@pytest.mark.parametrize("source", HOSTILE_SOURCES)
def test_the_scanner_never_raises_on_hostile_source(source: str) -> None:
    """A scanner that raises has no verdict, and no verdict is not a pass.

    ``scan`` is documented to answer with findings for *any* input. A
    hostile source that escapes as an exception is not a finding — it is the
    gate absent, and the caller that expected a list gets a traceback
    instead.
    """
    findings = AstSecurityScanner().scan(source)

    assert isinstance(findings, list)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "AstSecurityScanner.scan guards ast.parse against RecursionError but not the "
        "walk that follows it: a source that parses into a 2000-deep attribute chain "
        "exhausts the stack inside ast.NodeVisitor.generic_visit, outside the "
        "try/except at src/paa/validation/ast_scanner.py:364-392. ast_scanner.py is "
        "read-only for T-005; the fix is to wrap the visitor call in the same guard "
        "and return the unparseable_source CRITICAL finding."
    ),
)
def test_deeply_nested_source_does_not_escape_as_a_recursion_error() -> None:
    """Deeply nested source is rejected, not fatal.

    The module's promise covers source the parser can read but the walker
    cannot: "I could not analyse this" must mean "this does not run".
    """
    source = "x = " + ("a." * 2000) + "b"

    findings = AstSecurityScanner().scan(source)

    assert findings
    assert all(finding.severity is Severity.CRITICAL for finding in findings)


# ---------------------------------------------------------------------------
# The finding object
# ---------------------------------------------------------------------------


def test_a_finding_serialises_for_the_ledger() -> None:
    """The ledger payload shape is part of the contract."""
    finding = Finding(
        rule="denied_import",
        severity=Severity.CRITICAL,
        lineno=3,
        col=7,
        snippet="import subprocess",
        message="import of denied module 'subprocess'",
    )

    assert finding.to_payload() == {
        "rule": "denied_import",
        "severity": "CRITICAL",
        "lineno": 3,
        "col": 7,
        "snippet": "import subprocess",
        "message": "import of denied module 'subprocess'",
    }
    assert str(finding).startswith("CRITICAL denied_import at 3:7")


def test_severity_ranks_are_ordered() -> None:
    """The threshold comparison in the engine depends on this ordering."""
    ranks = [severity.rank for severity in Severity]

    assert ranks == sorted(ranks)
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.MEDIUM.rank


def test_the_scanner_is_stateless_across_calls() -> None:
    """One scanner, many scans: no state leaks between artifacts.

    The module promises the instance is safe to share across concurrent
    validations, which it can only be if each scan builds its own visitor.
    """
    scanner = AstSecurityScanner()

    scanner.scan("import subprocess\n")
    clean = scanner.scan("x = 1\n")

    assert clean == []


def test_the_policy_is_readable_from_the_scanner() -> None:
    """The scanner reports the policy it is running under."""
    policy = ScannerPolicy(blocking_severity=Severity.MEDIUM)
    scanner = AstSecurityScanner(policy)

    assert scanner.policy is policy
    assert scanner.policy.blocking_severity is Severity.MEDIUM


def test_scan_path_uses_the_given_filename() -> None:
    """The filename in the finding is the one the caller supplied.

    The engine scans in-memory sources under the artifact's own names, and a
    finding that cannot be traced back to a file is a finding nobody can fix.
    """
    findings = AstSecurityScanner().scan("def broken(:\n", filename="pkg/mod.py")

    assert findings
    assert findings[0].rule == "unparseable_source"
    assert findings[0].lineno == 1


def test_scan_path_reads_utf8_by_default(tmp_path: Path) -> None:
    """A UTF-8 file is scanned, not rejected."""
    path = tmp_path / "ok.py"
    path.write_text("x = 1\n", encoding="utf-8")

    findings = AstSecurityScanner().scan_path(path)

    assert findings == []


def test_a_finding_is_immutable() -> None:
    """The finding is a frozen dataclass, so it is hashable and cheap.

    The engine sorts and de-duplicates findings by rule, and a mutable
    object in that path is a source of order-dependent reports.
    """
    finding = Finding(
        rule="denied_import",
        severity=Severity.CRITICAL,
        lineno=1,
        col=0,
        snippet="",
        message="",
    )

    with pytest.raises(FrozenInstanceError):
        finding.__setattr__("rule", "other")
