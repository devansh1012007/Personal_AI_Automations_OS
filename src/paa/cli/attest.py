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

``paa attest pr`` emits the PR comment body — a markdown summary of the
bundle with the exact verification command — to the console or a file.
It posts nothing: this runtime has no network surface for posting, and
a command that pretended to publish would be exactly the kind of
quietly-false artifact the attestation exists to catch. The body is
paste-ready, and ``docs/interfaces/attested-bundle-spec.md`` carries a
GitHub Action template that runs the verification for real.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

from paa.attest.format import APPROVAL_DECISIONS

if TYPE_CHECKING:
    # Annotation-only: the producer module is imported inside the command
    # bodies so the CLI's Typer surface stays cheap (see the comment below).
    from paa.attest.bundle import Approval

console = Console()

# The producer and verifier modules are imported inside the command bodies
# (the ``paa observe`` CLI's own pattern): ``paa --version`` and every other
# command pay only for this module's Typer surface, and the attest machinery
# arrives when an attest or verify command actually runs.

attest_app = typer.Typer(
    help="Build attested change bundles (H3.1): the artifact a reviewer "
    "verifies offline with `paa verify`."
)


def _parse_approval(raw: str) -> Approval:
    """``kind:id:decision[:note]`` — colons separate, note may be empty."""
    from paa.attest.bundle import Approval

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
        f"one of {sorted(APPROVAL_DECISIONS)}).",
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
        console.print(f"[red]attest refused:[/red] {exc.message}")
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
        console.print(f"[yellow]warning:[/yellow] {warning}")


@attest_app.command("pr")
def attest_pr(
    bundle_dir: Path = typer.Argument(..., help="The bundle directory."),
    out_file: Path | None = typer.Option(
        None, "--out", help="Write the body here instead of printing it."
    ),
) -> None:
    """Emit the PR comment body for a bundle. Posts nothing — the body
    is paste-ready, and the Action template in the interface doc runs
    the verification for real."""
    from paa.core.errors import PaaError

    try:
        body = _pr_body(bundle_dir)
    except (PaaError, ValueError, OSError) as exc:
        console.print(f"[red]pr refused:[/red] {exc}")
        raise typer.Exit(2) from exc
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

    result = verify_bundle(bundle_dir, trust_key=trust, workspace=workspace)

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
            mark = "[green]✓[/green]" if check.ok else "[red]✗[/red]"
            console.print(f"{mark} {check.name}: {check.detail}")
        verdict = "verified" if result.ok else "FAILED"
        colour = "green" if result.ok else "red"
        console.print(
            f"[{colour}]{verdict}[/{colour}] "
            f"bundle {result.bundle_id} "
            f"chain {result.chain_root[:12]}… "
            f"key {result.key_id[:12]}… ({result.algorithm})"
        )
        if result.observed_tests or result.asserted_tests:
            console.print(
                f"tests observed/asserted: {result.observed_tests}/"
                f"{result.asserted_tests}; approvals: {result.approvals}"
            )
        for warning in result.warnings:
            console.print(f"[yellow]warning:[/yellow] {warning}")

    if not result.ok:
        raise typer.Exit(1)


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
    test_entries = tests.get("entries") or []
    observed = sum(1 for e in test_entries if e.get("source") == "observed")
    asserted = len(test_entries) - observed
    green = sum(
        1 for e in test_entries if e.get("source") == "observed" and e.get("command_green")
    )
    requester = mandate.get("requester_asserted") or {}

    lines = [
        "## Attested change bundle",
        "",
        f"- **Bundle**: `{manifest.get('bundle_id')}` "
        f"(tool {manifest.get('tool_version')}, {manifest.get('created_at')})",
        f"- **Goal**: {mandate.get('goal', '-')}",
        f"- **Requested by** (asserted): "
        f"`{requester.get('kind', '?')}:{requester.get('id', '?')}` under policy "
        f"`{mandate.get('policy_label', '?')}`",
        f"- **Chain root**: `{manifest.get('chain_root')}`",
        f"- **Signed**: {signature.get('algorithm')} "
        f"(key `{str(signature.get('key_id'))[:16]}…`)",
        f"- **Tests**: {observed} observed ({green} green) / {asserted} asserted",
        f"- **Approvals**: {len(approvals.get('entries') or [])} recorded",
        f"- **Evidence bundles**: {len(evidence.get('bundles') or [])} "
        "(T1 observe bundles, content-addressed)",
        "",
        "### How to verify",
        "",
        "```bash",
        f"paa verify {bundle_dir} --trust <signer-public-key>",
        "```",
        "",
        "The command runs offline: it recomputes every blob's hash, re-walks",
        "the chain and checks the signature against the embedded public key.",
        "Without `--trust` it reports the key as *self-asserted* — integrity",
        "checked, origin not claimed.",
    ]
    return "\n".join(lines)
