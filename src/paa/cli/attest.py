"""``paa attest`` and ``paa verify`` — build the artifact, then prove it (C7.2).

The H3.1 front doors. ``paa attest build`` is the producer side: it
gathers the mandate, the workspace's diff, the observed (or honestly
asserted) commands and tests, the approvals, writes every payload as a
content-addressed blob, chains the sections, signs the chain root with
Ed25519, and leaves a self-contained directory under
``$PAA_HOME/attest/<id>/``.

``paa verify`` is the consumer side and the spec's acceptance line made
a command: a reviewer carries the bundle to a clean checkout, runs
``paa verify <dir>``, and the verdict comes from the bundle's own bytes
— no database, no producer, no network. Exit ``0`` only when every door
held. ``--trust`` pins the signing key (origin); without it the verdict
says ``self-asserted key`` and never ``trusted``.

``paa attest pr`` verifies the bundle first, then emits a PR comment
body — a markdown summary with the goal, chain root, key id, test split
and the exact verification command — to the console or a file. Every
untrusted field is an inert code span. It posts nothing: this runtime
has no network surface for posting, and a command that pretended to
publish would be the quietly-false artifact the attestation exists to
catch; the interface doc carries the Action that verifies for real.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console

# APPROVAL_DECISIONS is imported in _parse_approval; the package __init__ is eager.

if TYPE_CHECKING:
    # Annotation-only: the producer module is imported inside the command
    # bodies so importing this CLI never executes paa.attest.__init__.
    from paa.attest.bundle import Approval

console = Console(emoji=False)  # no :shortcode: substitution in hostile details

# No paa.attest module is imported at module scope: ``paa.attest.__init__``
# eagerly re-exports bundle and verify, so importing it here would make
# every command pay for the whole attestation machinery. Imports stay in
# the command bodies (the ``paa observe`` CLI's own pattern).

attest_app = typer.Typer(
    help="Build attested change bundles (H3.1): the artifact a reviewer "
    "verifies offline with `paa verify`."
)


def _parse_approval(raw: str) -> Approval:
    """``kind:id:decision[:note]`` — colons separate, note may be empty."""
    from paa.attest.bundle import Approval
    from paa.attest.format import APPROVAL_DECISIONS

    parts = raw.split(":", 3)
    if len(parts) < 3:
        raise typer.BadParameter(
            f"{raw!r} is not kind:id:decision[:note] (decision is one of "
            f"{sorted(APPROVAL_DECISIONS)})"
        )
    kind, approver, decision = parts[0].strip(), parts[1].strip(), parts[2].strip()
    note = parts[3] if len(parts) == 4 else ""
    try:
        return Approval(
            approver_kind=kind, approver_id=approver, decision=decision, note=note
        )
    except Exception as exc:  # the closed vocabulary, restated as CLI help
        raise typer.BadParameter(str(exc)) from exc


def _shlex_command(raw: str) -> list[str]:
    """One command line → argv, refusing what shlex cannot split honestly."""
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise typer.BadParameter(f"{raw!r}: {exc}") from exc
    if not argv:
        raise typer.BadParameter(f"{raw!r} splits to an empty command")
    return argv


@attest_app.command("build")
def attest_build(
    goal: str = typer.Option(..., "--goal", help="What this work was for."),
    workspace: Path = typer.Option(
        Path("."), "--workspace", "-w", help="The workspace being attested."
    ),
    requester: str = typer.Option(
        "operator", "--requester", help="Who asked for the work (asserted)."
    ),
    diff_since: str | None = typer.Option(
        None, "--diff-since", help="A rev; commits from it to HEAD ride the diff."
    ),
    command: list[str] = typer.Option(
        None,
        "--command",
        help="A command that ran, as a shell line (asserted; repeatable).",
    ),
    test_bundle: list[Path] = typer.Option(
        None,
        "--test-bundle",
        help="A paa observe bundle.json that witnessed a test run (repeatable).",
    ),
    test_command: list[str] = typer.Option(
        None,
        "--test-command",
        help="A test command that ran, as a shell line (asserted; repeatable).",
    ),
    evidence_bundle: list[Path] = typer.Option(
        None,
        "--evidence-bundle",
        help="A paa observe bundle.json that witnessed any other command "
        "(repeatable).",
    ),
    approval: list[str] = typer.Option(
        None,
        "--approval",
        help="A sign-off, kind:id:decision[:note] (repeatable; decision is "
        "approved, rejected or changes_requested).",
    ),
    key_path: Path | None = typer.Option(
        None, "--key", help="The signing key file (default: $PAA_HOME/attest/attest-key.json)."
    ),
    out: Path | None = typer.Option(
        None, "--out", help="Where to write the bundle (default: $PAA_HOME/attest/<id>)."
    ),
    insecure_fallback_signing: bool = typer.Option(
        False,
        "--insecure-fallback-signing",
        help="Allow the symmetric dev fallback when real signing is "
        "unavailable — honestly labelled in the manifest.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Build one attested change bundle: mandate, diff, commands, tests,
    approvals, hash chain, signature."""
    from paa.attest.bundle import build_bundle, load_or_generate_key
    from paa.config import get_settings
    from paa.core.errors import PaaError

    home = get_settings().home
    key = load_or_generate_key(
        key_path or (home / "attest" / "attest-key.json"),
        allow_insecure=insecure_fallback_signing,
    )

    from datetime import UTC, datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    bundle_dir = out or (home / "attest" / stamp)

    try:
        result = build_bundle(
            out_dir=bundle_dir,
            workspace=workspace,
            goal=goal,
            requester=requester,
            diff_since=diff_since,
            asserted_commands=(_shlex_command(c) for c in command or []),
            test_bundles=test_bundle or [],
            asserted_tests=(_shlex_command(c) for c in test_command or []),
            evidence_bundles=evidence_bundle or [],
            approvals=[_parse_approval(a) for a in approval or []],
            key=key,
        )
    except PaaError as exc:
        # The refusal can quote an operator token or a path from a hostile
        # workspace; print it literally, never as Rich markup.
        console.print(
            f"attest refused: {exc.message}",
            style="red",
            markup=False,
            highlight=False,
        )
        raise typer.Exit(2) from exc

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "bundle_dir": str(result.bundle_dir),
                    "bundle_id": result.bundle_id,
                    "chain_root": result.chain_root,
                    "key_id": result.key_id,
                    "algorithm": result.algorithm,
                    "sections": result.section_sizes,
                    "evidence_count": result.evidence_count,
                    "observed_tests": result.observed_tests,
                    "asserted_tests": result.asserted_tests,
                    "warnings": list(result.warnings),
                }
            )
        )
        return

    console.print(
        f"[green]attested[/green] {result.bundle_id} "
        f"({result.algorithm}, key {result.key_id[:12]}…)"
    )
    console.print(f"[dim]bundle: {result.bundle_dir}[/dim]")
    console.print(f"[dim]chain root: {result.chain_root[:12]}…[/dim]")
    console.print(
        f"sections: {', '.join(result.section_sizes)}; "
        f"evidence bundles: {result.evidence_count}; "
        f"tests observed/asserted: {result.observed_tests}/{result.asserted_tests}"
    )
    console.print(f"verify with: paa verify {result.bundle_dir}")
    for warning in result.warnings:
        console.print(
            f"warning: {_console_text(warning)}",
            style="yellow",
            markup=False,
            highlight=False,
        )


@attest_app.command("pr")
def attest_pr(
    bundle_dir: Path = typer.Argument(..., help="The bundle directory."),
    out_file: Path | None = typer.Option(
        None, "--out", help="Write the body here instead of printing it."
    ),
) -> None:
    """Verify a bundle, then emit its PR comment body. Posts nothing —
    the body is paste-ready, and the Action template in the interface
    doc runs the verification for real."""
    from paa.attest.verify import verify_bundle
    from paa.core.errors import PaaError

    def _refuse(message: str) -> NoReturn:
        console.print(
            _console_text(message),
            style="red",
            markup=False,
            highlight=False,
        )
        raise typer.Exit(2)

    # A body that describes an artifact as attested must not be produced
    # for a started, tampered or fallback-signed bundle. Integrity is
    # checked here with no --trust pin; origin stays self-asserted, as
    # the body says. Verify first, then render.
    try:
        result = verify_bundle(bundle_dir)
    except (
        PaaError, OSError, ValueError, UnicodeError, TypeError, RecursionError
    ) as exc:
        _refuse(f"pr refused: hostile bundle input ({type(exc).__name__})")
    if not result.ok:
        failed = ", ".join(
            f"{check.name}: {check.detail}" for check in result.checks if not check.ok
        )
        _refuse(f"pr refused: bundle failed verification ({failed or 'no check passed'})")

    try:
        body = _pr_body(bundle_dir)
        # The renderer reads the manifest a second time; check the bytes
        # again so a local writer cannot swap them between the two reads.
        after = verify_bundle(bundle_dir)
    except (
        PaaError, OSError, ValueError, UnicodeError, TypeError, RecursionError
    ) as exc:
        _refuse(f"pr refused: {exc}")
    if not after.ok:
        failed = ", ".join(
            f"{check.name}: {check.detail}" for check in after.checks if not check.ok
        )
        _refuse(f"pr refused: bundle changed while rendering ({failed})")
    if out_file is not None:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(body, encoding="utf-8")
        console.print(f"[green]wrote[/green] {out_file}")
    else:
        console.print(body, markup=False, highlight=False)


def verify(
    bundle_dir: Path = typer.Argument(..., help="The bundle directory."),
    trust: str | None = typer.Option(
        None,
        "--trust",
        help="Pin the signer: a base64 public key, or a path to a key file.",
    ),
    workspace: Path | None = typer.Option(
        None, "--workspace", "-w", help="Also check this checkout against the "
        "attested manifest."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the verdict as JSON."),
) -> None:
    """Verify an attested change bundle offline: hashes, chain, signature,
    key. Exit 0 only when every check held."""
    from paa.attest.verify import verify_bundle

    try:
        result = verify_bundle(bundle_dir, trust_key=trust, workspace=workspace)
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as exc:
        # The verifier is written to be total on hostile input; this is a
        # last line of defence so a malformed artifact still gets a verdict
        # line instead of a traceback on a reviewer's console.
        console.print(
            f"✗ verification: refused hostile input ({type(exc).__name__})",
            style="red",
            markup=False,
            highlight=False,
        )
        raise typer.Exit(1) from exc

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "ok": result.ok,
                    "bundle_id": result.bundle_id,
                    "chain_root": result.chain_root,
                    "key_id": result.key_id,
                    "algorithm": result.algorithm,
                    "created_at": result.created_at,
                    "evidence_count": result.evidence_count,
                    "observed_tests": result.observed_tests,
                    "asserted_tests": result.asserted_tests,
                    "approvals": result.approvals,
                    "checks": [
                        {"name": c.name, "ok": c.ok, "detail": c.detail}
                        for c in result.checks
                    ],
                    "warnings": list(result.warnings),
                }
            )
        )
    else:
        for check in result.checks:
            mark = "✓" if check.ok else "✗"
            console.print(
                f"{mark} {check.name}: {_console_text(check.detail)}",
                style="green" if check.ok else "red",
                markup=False,
                highlight=False,
            )
        verdict = "verified" if result.ok else "FAILED"
        console.print(
            _console_text(
                f"{verdict} bundle {result.bundle_id} "
                f"chain {result.chain_root[:12]}… "
                f"key {result.key_id[:12]}… ({result.algorithm})"
            ),
            style="green" if result.ok else "red",
            markup=False,
            highlight=False,
        )
        if result.observed_tests or result.asserted_tests:
            console.print(
                f"tests observed/asserted: {result.observed_tests}/"
                f"{result.asserted_tests}; approvals: {result.approvals}",
                markup=False,
                highlight=False,
            )
        for warning in result.warnings:
            console.print(
                f"warning: {_console_text(warning)}",
                style="yellow",
                markup=False,
                highlight=False,
            )

    if not result.ok:
        raise typer.Exit(1)


def _console_text(value: object) -> str:
    """Flatten terminal and format controls in untrusted CLI text."""
    import unicodedata

    text = "" if value is None else str(value)
    # ANSI escapes, bidi overrides and Unicode line/paragraph separators
    # are all format characters with no place in a refusal line: a hostile
    # blob role must not clear the terminal or spoof a second verdict.
    return "".join(
        " " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} else char
        for char in text
    )


def _plain_text(value: object) -> str:
    """One untrusted scalar as one line of text, with no controls."""
    text = "" if value is None else str(value)
    # One scalar must never become a new markdown line, a reference
    # definition or an HTML block: flatten every C0 control (including
    # NUL and newlines) and DEL before it reaches a template.
    text = "".join(
        " " if ord(char) < 0x20 or ord(char) == 0x7F else char for char in text
    )
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        # A lone surrogate can come out of a JSON escape; writing it to
        # --out would fail with a traceback, so refuse it here instead.
        raise ValueError("a bundle field is not UTF-8 encodable") from exc
    return text


def _longest_backtick_run(text: str) -> int:
    longest = 0
    run = 0
    for char in text:
        if char == "`":
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return longest


def _md_scalar(value: object) -> str:
    """Render one untrusted value as an inert, single-line code span."""
    text = _plain_text(value)
    # A delimiter longer than any backtick run in the value cannot be
    # closed by the value. CommonMark strips one space at each end, so
    # padding keeps leading/trailing backticks inert without changing how
    # ordinary text renders.
    fence = "`" * (_longest_backtick_run(text) + 1)
    return f"{fence} {text} {fence}"


def _md_command(bundle_dir: Path) -> str:
    """The verification command as a fenced block the path cannot escape."""
    text = _plain_text(str(bundle_dir))
    # The path is operator input, not bundle data, but a path containing a
    # backtick fence would still close the block when the body is pasted.
    fence = "`" * max(3, _longest_backtick_run(text) + 1)
    return "\n".join(
        (f"{fence}bash", f"paa verify {text} --trust <signer-public-key>", fence)
    )


def _pr_body(bundle_dir: Path) -> str:
    """The PR comment body: the bundle's summary, as markdown."""
    from paa.attest.bundle import read_manifest
    from paa.attest.verify import read_section

    manifest = read_manifest(bundle_dir / "manifest.json")
    sections = {
        str(s.get("name")): str(s.get("sha256"))
        for s in manifest.get("sections") or []
    }
    signature = manifest.get("signature") or {}

    def _section(name: str) -> dict[str, Any]:
        sha = sections.get(name, "")
        return read_section(bundle_dir, sha) or {} if sha else {}

    mandate = _section("mandate")
    tests = _section("tests")
    approvals = _section("approvals")
    evidence = _section("evidence")
    test_entries = [e for e in tests.get("entries") or [] if isinstance(e, dict)]
    observed = sum(1 for e in test_entries if e.get("source") == "observed")
    asserted = len(test_entries) - observed
    green = sum(
        1 for e in test_entries if e.get("source") == "observed" and e.get("command_green")
    )
    requester = mandate.get("requester_asserted") or {}
    if not isinstance(requester, dict):
        requester = {}
    identity = f"{requester.get('kind', '?')}:{requester.get('id', '?')}"
    key_id = str(signature.get("key_id") or "")[:16] + "…"
    approval_rows = [e for e in approvals.get("entries") or [] if isinstance(e, dict)]
    evidence_rows = [e for e in evidence.get("bundles") or [] if isinstance(e, dict)]

    lines = [
        "## Attested change bundle",
        "",
        f"- **Bundle**: {_md_scalar(manifest.get('bundle_id'))} "
        f"(tool {_md_scalar(manifest.get('tool_version'))}, "
        f"{_md_scalar(manifest.get('created_at'))})",
        f"- **Goal**: {_md_scalar(mandate.get('goal', '-'))}",
        f"- **Requested by** (asserted): {_md_scalar(identity)} under policy "
        f"{_md_scalar(mandate.get('policy_label', '?'))}",
        f"- **Chain root**: {_md_scalar(manifest.get('chain_root'))}",
        f"- **Signed**: {_md_scalar(signature.get('algorithm'))} "
        f"(key {_md_scalar(key_id)})",
        f"- **Tests**: {observed} observed ({green} green) / {asserted} asserted",
        f"- **Approvals**: {len(approval_rows)} recorded",
        f"- **Evidence bundles**: {len(evidence_rows)} "
        "(T1 observe bundles, content-addressed)",
        "",
        "### How to verify",
        "",
        _md_command(bundle_dir),
        "",
        "The command runs offline: it recomputes every blob's hash, re-walks",
        "the chain and checks the signature against the embedded public key.",
        "Without `--trust` it reports the key as *self-asserted* — integrity",
        "checked, origin not claimed.",
    ]
    return "\n".join(lines)
