"""The pipeline-package flow — source → gates → conformance → review → stage.

W8 C8.6 (the pipeline spec's P5-lite), proven the way the chat extension
surface's flow was proven (C8.4): a real sandbox, a real ledger, a real
database, the production installer — no double for the gates, the
review seam or the staging. What these tests prove is the *flow* the row
names: a local directory, a PINNED git URL, a signed ``.paapkg`` and
(since W9 C9.1) a **hub** listing all pass through the same five gates,
the six-check conformance standard and a human capability review before
one byte is registered; every refusal path leaves zero trace.

The hosted boundary this file used to pin — ``registry_client.py`` is
never imported here — was the C8.6 park marker, and C9.1 retired it by
delivering the row it parked. The boundary that replaces it is the one
the registry client itself states: **a hub is untrusted, and hub
installs go through the registry client, but nothing it returns becomes
runnable until the six stages pass unmodified** — pinned below by the
hub tests (a hub-served package rides the same stages, and a hostile one
refuses at the same gates with the same zero trace).

The git sources are local repositories created in ``tmp_path`` — no
network, deterministic, the brief's own rule. The hub sources are local
directories in the ``LocalDirectoryRegistry`` format written by the real
publish flow, plus one HTTP-served hub driven through a mocked transport
(the ``tests/models`` pattern) — still no network.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from paa.core.types import EventType, PermissionMode
from paa.improve.gate import DECISION_APPROVED, DECISION_REJECTED, HumanDecision
from paa.ledger.store import LedgerStore
from paa.marketplace.installer import InstallGate, PackageInstaller
from paa.marketplace.package import PIPELINE_KIND, PackageManifest, SkillPackage
from paa.marketplace.registry_client import HttpRegistry
from paa.marketplace.signing import generate_keypair, sign, signing_is_secure
from paa.pipelines.packages import (
    REVIEW_ARTIFACT_NAME,
    SOURCE_GIT,
    SOURCE_HUB,
    SOURCE_LOCAL_DIR,
    SOURCE_PAAPKG,
    CapabilityReview,
    PipelinePackageInstaller,
    install_pipeline_package,
    render_review,
)
from paa.pipelines.publish import PUBLISHER_NAME, publish_pipeline_package
from paa.pipelines.registry import PipelineRegistry
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.storage.relational.database import Database
from paa.validation.ast_scanner import AstSecurityScanner

# The fixture package: the declaration the closed contract accepts (an
# adapter-shaped ask — think, within the sandbox's own budget), plus the
# one entrypoint the loading contract pins. The entrypoint survives both
# arg shapes the contract names: the smoke gate's ``{}`` probe and a
# run's ``{"goal": ..., "params": {...}}``.
_DECLARATION = """\
[pipeline]
name = "demo.echo"
version = "1.2.3"
protocol = "paa-pipeline/1"
description = "A fixture pipeline that echoes its goal."
requires_runtime = ">=4.1,<5"

[run]
resume = "journal"

[evidence]
declares_tier = "unrated"

[capabilities]
think = { roles = ["worker"] }

[limits]
max_tokens = 100000
max_wall_s = 600
max_calls = 100
max_sub_runs = 1
"""

_ENTRYPOINT = """\
import json, sys

args_path = sys.argv[sys.argv.index("--paa-args") + 1]
with open(args_path, encoding="utf-8") as fh:
    args = json.load(fh)
goal = args.get("goal") or ""
print(json.dumps({
    "status": "completed",
    "summary": "echo: " + goal,
    "metrics": {},
}))
"""


def _package_dir(
    root: Path,
    *,
    declaration: str = _DECLARATION,
    entrypoint: str = _ENTRYPOINT,
) -> Path:
    """A pipeline-package source directory, off the tree's own scratch."""
    source = root / f"src-{uuid.uuid4().hex[:6]}"
    source.mkdir(parents=True)
    (source / "pipeline.toml").write_text(declaration, encoding="utf-8")
    (source / "pipeline.py").write_text(entrypoint, encoding="utf-8")
    return source


async def _approve(review: CapabilityReview) -> HumanDecision:
    return HumanDecision(
        decision=DECISION_APPROVED,
        approver_kind="human",
        approver_id="test-approver",
        note="the fixture ask",
    )


async def _refuse(review: CapabilityReview) -> HumanDecision:
    return HumanDecision(
        decision=DECISION_REJECTED,
        approver_kind="human",
        approver_id="test-approver",
        note="no",
    )


def _installer(
    ledger: LedgerStore,
    db: Database,
    home: Path,
    *,
    approver: Any = _approve,
    allow_unverified: bool = True,
    mode: PermissionMode = PermissionMode.ASK,
) -> PipelinePackageInstaller:
    return PipelinePackageInstaller(
        ledger=ledger,
        db=db,
        home=home,
        sandbox=SubprocessSandbox(),
        scanner=AstSecurityScanner(),
        permission_mode=mode,
        allow_unverified=allow_unverified,
        approver=approver,
    )


async def _marketplace_rows(db: Database) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in await db.fetch_all(
            "SELECT package_name, version, kind, publisher, signature_state "
            "FROM marketplace_packages"
        )
    ]


def _git(repo: Path, *args: str) -> str:
    import subprocess

    out = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def _git_repo(root: Path, *, declaration: str = _DECLARATION) -> tuple[Path, str]:
    """A local git repository whose HEAD is the fixture package."""
    repo = root / f"repo-{uuid.uuid4().hex[:6]}"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "fixture@example.test")
    _git(repo, "config", "user.name", "Fixture")
    (repo / "pipeline.toml").write_text(declaration, encoding="utf-8")
    (repo / "pipeline.py").write_text(_ENTRYPOINT, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "the fixture package")
    return repo, _git(repo, "rev-parse", "HEAD")


async def _publish_fixture(
    tmp_path: Path,
    *,
    hub: Path | None = None,
    version: str = "1.2.3",
    declaration: str = _DECLARATION,
    entrypoint: str = _ENTRYPOINT,
    unsigned: bool = True,
) -> tuple[Path, str]:
    """Publish the fixture package into a hub directory (a fresh one by
    default, or the one named — the shape the multi-version tests need).

    The hub is written by the real publish flow, so the index is exactly
    the format an install reads back. The default publish is unsigned —
    the everywhere-green shape, the same opt-in the local-dir tests use —
    and the signature-bearing paths are separate tests.
    """
    decl = declaration
    if version != "1.2.3":
        decl = decl.replace('version = "1.2.3"', f'version = "{version}"')
    source = _package_dir(tmp_path, declaration=decl, entrypoint=entrypoint)
    if hub is None:
        hub = tmp_path / f"hub-{uuid.uuid4().hex[:6]}"
    report = await publish_pipeline_package(
        str(source),
        hub=hub,
        home=tmp_path / "home",
        allow_unverified=unsigned,
    )
    assert report.published
    return hub, report.publisher_key


def _edit_index(hub: Path, mutate: Any) -> None:
    """Rewrite a hub's index through ``mutate`` (a hostile hub's edit)."""
    index = json.loads((hub / "index.json").read_text(encoding="utf-8"))
    mutate(index)
    (hub / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")


def _http_hub(hub: Path, base_url: str) -> HttpRegistry:
    """An ``HttpRegistry`` whose every request is served out of a local
    hub directory, through a mocked transport — the ``tests/models``
    pattern: the seam is the registry's transport, never a socket."""
    import httpx

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search":
            results = json.loads((hub / "index.json").read_text(encoding="utf-8"))
            return httpx.Response(200, json={"results": results})
        _, _, rest = request.url.path.partition("/packages/")
        name, _, version = rest.strip("/").partition("/")
        data = (hub / f"{name}-{version}.paapkg").read_bytes()
        return httpx.Response(200, content=data)

    return HttpRegistry(base_url, transport=httpx.MockTransport(responder))


class TestLocalDirectorySource:
    """The cheapest source, through every stage."""

    async def test_the_whole_flow(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install(str(source))

        assert report.installed
        assert report.source == SOURCE_LOCAL_DIR
        assert report.package_name == "demo.echo"
        assert report.version == "1.2.3"
        assert report.gates == tuple(gate.value for gate in InstallGate)
        assert report.signature_state == "unverified"  # unsigned, opted in
        assert report.decision is not None and report.decision.approved

        # The gated bytes (and the review that cleared them) are staged,
        # which IS registration: the registry scans that layout.
        staged = installer.staged_dir("demo.echo", "1.2.3")
        assert (staged / "pipeline.toml").is_file()
        assert (staged / "pipeline.py").is_file()
        artifact = json.loads(
            (staged / REVIEW_ARTIFACT_NAME).read_text(encoding="utf-8")
        )
        assert artifact["package_name"] == "demo.echo"
        assert artifact["capabilities"] == {"think": {"roles": ["worker"]}}
        assert artifact["decision"]["decision"] == DECISION_APPROVED
        assert artifact["decision"]["approver_kind"] == "human"

        registry = PipelineRegistry(local_dir=home / "pipelines")
        row = registry.get("demo.echo")
        assert row is not None
        assert row.trust == "installed"
        assert row.runnable
        assert row.version == "1.2.3"
        assert registry.local_errors == {}

        # The marketplace row the five gates wrote, in the pipeline kind.
        assert await _marketplace_rows(db) == [
            {
                "package_name": "demo.echo",
                "version": "1.2.3",
                "kind": PIPELINE_KIND,
                "publisher": SOURCE_LOCAL_DIR,
                "signature_state": "unverified",
            }
        ]

        # One install action, one fresh lineage, one event — and the
        # conformance runs it drove are NOT on it (they run on their own
        # lineages, traceable with `paa pipeline trace`).
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [EventType.PIPELINE_PACKAGE_INSTALLED]
        payload = events[0].payload
        assert payload["v"] == 1
        assert payload["lineage"] == str(report.lineage)
        assert payload["source"] == SOURCE_LOCAL_DIR
        assert payload["gates"] == [gate.value for gate in InstallGate]
        assert payload["review"]["approver_id"] == "test-approver"
        assert set(payload["conformance"]) == {
            "double-run-sequence-equality",
            "divergence-halt",
            "unknown-outcome-never-auto-repeats",
            "cancel-within-grace",
            "budget-exhaustion",
            "no-secrets-in-journal",
        }
        assert payload["conformance"]["divergence-halt"] == "pass"

    async def test_the_conformance_runs_land_on_their_own_lineages(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")
        report = await installer.install(str(source))
        assert report.installed

        # Every conformance drive wrote RUN_STARTED rows somewhere; none
        # of them is the install's own lineage.
        rows = await db.fetch_all(
            "SELECT DISTINCT correlation_id FROM system_state_ledger "
            "WHERE event_type = 'PIPELINE_RUN_STARTED'"
        )
        assert rows, "the conformance drives journalled their runs"
        run_lineages = {row["correlation_id"] for row in rows}
        assert report.lineage is not None
        assert str(report.lineage) not in run_lineages

    async def test_a_refused_review_leaves_zero_trace(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        home = tmp_path / "home"
        installer = _installer(ledger, db, home, approver=_refuse)

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "capability_review"
        assert "refused by human:test-approver" in (report.reason or "")
        # Zero trace: no staged bytes, no registry row, and the gates'
        # own marketplace row is unregistered again — the review's "no"
        # leaves as little as a scanner's.
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None
        assert await _marketplace_rows(db) == []
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [EventType.PIPELINE_PACKAGE_REFUSED]
        assert events[0].payload["stage"] == "capability_review"
        assert events[0].payload["v"] == 1

    async def test_no_approver_wired_is_a_refusal_not_an_install(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home", approver=None)

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "capability_review"
        # The refusal-by-default decision is recorded, and it names the
        # absence rather than inventing an approver.
        assert report.decision is not None
        assert report.decision.approver_kind == "installer"
        assert report.decision.approver_id == "unwired-capability-review"

    async def test_the_rendered_review_states_the_ask_and_its_limits(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        reviews: list[CapabilityReview] = []

        async def capture(review: CapabilityReview) -> HumanDecision:
            reviews.append(review)
            return await _approve(review)

        installer = _installer(ledger, db, tmp_path / "home", approver=capture)
        report = await installer.install(str(source))
        assert report.installed and len(reviews) == 1

        text = render_review(reviews[0])
        assert "demo.echo v1.2.3" in text
        assert "local_dir" in text
        assert "think" in text
        # The honest v1 limit, on the surface a human decides on: the
        # ask is recorded, not minted — nobody approves a silent no-op.
        assert "NOT minted in v1" in text
        assert "conformance: GREEN (6 checks)" in text


class TestRefusalPaths:
    """Every stage's refusal, and the zero-trace rule each one keeps."""

    async def test_an_ast_blocked_package_refuses_at_the_gate(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(
            tmp_path, entrypoint="import os\nos.system('rm -rf /')\n"
        )
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "ast_scan"
        assert "AST scan" in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert await _marketplace_rows(db) == []
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None

    async def test_lockdown_refuses_an_ask_beyond_sandboxed_execution(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # LOCKDOWN grants exactly PERM_SANDBOX_RUN — the loading
        # contract's own shape — so the ask that refuses is a
        # publisher's manifest demanding more. Only a ``.paapkg`` can
        # carry one: this flow pins a local-dir/git manifest to the
        # sandbox-run permission the adapter itself needs.
        files = {
            "pipeline.toml": _DECLARATION.encode("utf-8"),
            "pipeline.py": _ENTRYPOINT.encode("utf-8"),
        }
        manifest = PackageManifest(
            package_name="demo.echo",
            version="1.2.3",
            kind=PIPELINE_KIND,
            publisher="fixture",
            required_permissions=["PERM_SANDBOX_RUN", "PERM_NET_EGRESS"],
            entrypoints=["pipeline.py"],
        )
        manifest.content_hash = SkillPackage.hash_files(files)
        path = tmp_path / "egress.paapkg"
        SkillPackage(manifest, files).write(path)
        home = tmp_path / "home"
        installer = _installer(ledger, db, home, mode=PermissionMode.LOCKDOWN)

        report = await installer.install(str(path))

        assert not report.installed
        assert report.stage == "permissions"
        assert "PERM_NET_EGRESS" in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None

    async def test_lockdown_installs_the_sandbox_only_ask(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # The pinned ask — sandboxed execution only — installs under
        # LOCKDOWN, because a sandboxed entrypoint IS the lockdown
        # posture's own execution shape (core/types.py: LOCKDOWN grants
        # exactly PERM_SANDBOX_RUN).
        source = _package_dir(tmp_path)
        installer = _installer(
            ledger, db, tmp_path / "home", mode=PermissionMode.LOCKDOWN
        )

        report = await installer.install(str(source))

        assert report.installed
        assert report.gates == tuple(gate.value for gate in InstallGate)

    async def test_an_off_contract_declaration_refuses_before_the_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # An unknown capability name: the closed vocabulary is the whole
        # point of declaring, and an ask outside it is refused at load.
        source = _package_dir(
            tmp_path,
            declaration=_DECLARATION.replace(
                "think = { roles = [\"worker\"] }", "rm_rf_everything = true"
            ),
        )
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "declaration"
        assert "off contract" in (report.reason or "")
        assert "rm_rf_everything" in (report.reason or "")

    async def test_a_missing_declaration_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        (source / "pipeline.toml").unlink()
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "declaration"
        assert "no pipeline.toml" in (report.reason or "")

    async def test_a_missing_entrypoint_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        (source / "pipeline.py").unlink()
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "declaration"
        assert "entrypoint" in (report.reason or "")

    async def test_a_non_directory_source_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        installer = _installer(ledger, db, tmp_path / "home")
        report = await installer.install(str(tmp_path / "nope"))

        assert not report.installed
        assert report.stage == "source"
        assert "not a directory" in (report.reason or "")

    async def test_a_red_conformance_report_refuses_after_the_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # An entrypoint that cannot complete a run: the smoke gate's
        # probe is `{}` (which this child answers), but a real goal
        # fails it — the standard's double-run check sees a probe that
        # did not complete, and a RED report refuses the install.
        source = _package_dir(
            tmp_path,
            entrypoint=(
                "import json, sys\n"
                "args_path = sys.argv[sys.argv.index('--paa-args') + 1]\n"
                "with open(args_path, encoding='utf-8') as fh:\n"
                "    args = json.load(fh)\n"
                "if args.get('goal'):\n"
                "    sys.exit(3)\n"
                "print(json.dumps({'status': 'completed'}))\n"
            ),
        )
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "conformance"
        assert "RED" in (report.reason or "")
        assert "double-run-sequence-equality" in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert await _marketplace_rows(db) == []
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None

    async def test_a_range_that_excludes_the_running_version_refuses_pre_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # The compatibility floor, checked before any gate runs: the
        # declaration's own requires_runtime excludes this runtime, and
        # the manifest built from it carries the same range.
        from paa import __version__

        source = _package_dir(
            tmp_path,
            declaration=_DECLARATION.replace(
                'requires_runtime = ">=4.1,<5"', 'requires_runtime = ">=99,<100"'
            ),
        )
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install(str(source))

        assert not report.installed
        assert report.stage == "compatibility"
        assert "'>=99,<100'" in (report.reason or "")
        assert "excludes the running version" in (report.reason or "")
        assert __version__ in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert await _marketplace_rows(db) == []
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None


class TestHubSource:
    """The hosted half (C9.1): an untrusted registry read through the
    same six stages, with the hub's own vouch checked pre-gates."""

    async def test_a_hub_package_installs_through_every_stage(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        hub, _ = await _publish_fixture(tmp_path)
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install("hub:demo.echo@1.2.3", hub=str(hub))

        assert report.installed
        assert report.source == SOURCE_HUB
        assert report.source_ref == "demo.echo@1.2.3"
        assert report.gates == tuple(gate.value for gate in InstallGate)
        assert (installer.staged_dir("demo.echo", "1.2.3") / "pipeline.toml").is_file()
        row = PipelineRegistry(local_dir=home / "pipelines").get("demo.echo")
        assert row is not None and row.trust == "installed"
        # The marketplace row the five gates wrote: the publisher is the
        # publish flow's own role name, not a path — and the fixture's
        # unsigned publish rides the same opt-in a local dir does.
        assert await _marketplace_rows(db) == [
            {
                "package_name": "demo.echo",
                "version": "1.2.3",
                "kind": PIPELINE_KIND,
                "publisher": PUBLISHER_NAME,
                "signature_state": "unverified",
            }
        ]
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [EventType.PIPELINE_PACKAGE_INSTALLED]
        assert events[0].payload["source"] == SOURCE_HUB

    async def test_a_signed_hub_package_installs_with_a_trusted_key(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        hub, public_key = await _publish_fixture(tmp_path, unsigned=False)
        await PackageInstaller(db).trust_key("fixture-publisher", public_key, "operator")
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert report.installed
        assert report.signature_state == "valid"

    async def test_a_hostile_hub_package_refuses_at_the_same_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # The pin the replaced park marker promised: hub bytes ride the
        # six stages UNMODIFIED — an AST-blocked package the hub serves
        # refuses at the ast_scan gate exactly as a local one does, and
        # the refusal leaves the same zero trace.
        hub, _ = await _publish_fixture(
            tmp_path, entrypoint="import os\nos.system('rm -rf /')\n"
        )
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "ast_scan"
        assert "AST scan" in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert await _marketplace_rows(db) == []
        assert PipelineRegistry(local_dir=home / "pipelines").get("demo.echo") is None
        assert report.lineage is not None
        events = await ledger.read_correlation(report.lineage)
        assert [e.event_type for e in events] == [EventType.PIPELINE_PACKAGE_REFUSED]
        assert events[0].payload["source"] == SOURCE_HUB

    async def test_a_signature_under_the_wrong_key_refuses_pre_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        hub, _ = await _publish_fixture(tmp_path, unsigned=False)
        other = generate_keypair()

        def swap_key(index: list[dict[str, Any]]) -> None:
            index[0]["publisher_key"] = other.public_b64

        _edit_index(hub, swap_key)
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "source"
        assert "does not verify under the hub index's publisher key" in (
            report.reason or ""
        )
        assert not installer.installed_root.exists()

        # The escape hatch is the operator's explicit word, the same flag
        # the signature gate reads.
        escaped = _installer(ledger, db, tmp_path / "home2", allow_unverified=True)
        again = await escaped.install("hub:demo.echo", hub=str(hub))
        assert again.installed

    async def test_a_served_package_that_differs_from_its_entry_refuses_pre_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        # A publisher that signs with one key while the manifest restates
        # another: the entry vouches with the SIGNING key, so the vouch
        # itself verifies — but the archive's own identity claim disagrees
        # with the listing, and the install refuses rather than pick which
        # of the two to believe (the hub serves one thing and lists
        # another, in the one shape where both are internally consistent).
        real = generate_keypair()
        claimed = generate_keypair()
        files = {
            "pipeline.toml": _DECLARATION.encode("utf-8"),
            "pipeline.py": _ENTRYPOINT.encode("utf-8"),
        }
        manifest = PackageManifest(
            package_name="demo.echo",
            version="1.2.3",
            kind=PIPELINE_KIND,
            publisher="fixture",
            required_permissions=["PERM_SANDBOX_RUN"],
            entrypoints=["pipeline.py"],
        )
        manifest.content_hash = SkillPackage.hash_files(files)
        manifest.publisher_key = claimed.public_b64  # the restated lie
        manifest.signature = sign(manifest.signing_bytes(), real.private_b64)
        hub = tmp_path / "lying-publisher-hub"
        hub.mkdir()
        SkillPackage(manifest, files).write(hub / "demo.echo-1.2.3.paapkg")
        (hub / "index.json").write_text(
            json.dumps(
                [
                    {
                        "package_name": "demo.echo",
                        "version": "1.2.3",
                        "kind": PIPELINE_KIND,
                        "publisher": "fixture",
                        "publisher_key": real.public_b64,  # the honest vouch
                        "signature": manifest.signature,
                        "requires_paa": ">=4.1,<5",
                        "protocol": "paa-pipeline/1",
                    }
                ]
            ),
            encoding="utf-8",
        )
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "source"
        assert "does not match the hub's index entry" in (report.reason or "")
        assert not installer.installed_root.exists()

    async def test_an_index_entry_without_a_vouch_needs_the_opt_in(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        hub, _ = await _publish_fixture(tmp_path, unsigned=False)

        def strip_vouch(index: list[dict[str, Any]]) -> None:
            index[0]["publisher_key"] = ""
            index[0]["signature"] = ""

        _edit_index(hub, strip_vouch)
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "source"
        assert "carries no publisher signature" in (report.reason or "")
        assert not installer.installed_root.exists()

        # The opt-in is the operator's explicit word — the same flag the
        # signature gate reads — and it installs what the hub did not
        # vouch for, through every stage after that.
        escaped = _installer(ledger, db, tmp_path / "home2", allow_unverified=True)
        again = await escaped.install("hub:demo.echo", hub=str(hub))
        assert again.installed

    async def test_a_requires_paa_that_excludes_the_running_version_refuses_pre_gates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # The signed manifest is the copy the install refuses against:
        # a hostile index may CLAIM compatibility, the manifest's own
        # range still excludes this runtime and the refusal names it.
        from paa import __version__

        files = {
            "pipeline.toml": _DECLARATION.encode("utf-8"),
            "pipeline.py": _ENTRYPOINT.encode("utf-8"),
        }
        manifest = PackageManifest(
            package_name="demo.echo",
            version="1.2.3",
            kind=PIPELINE_KIND,
            publisher="fixture",
            required_permissions=["PERM_SANDBOX_RUN"],
            entrypoints=["pipeline.py"],
            requires_paa=">=99,<100",
            protocol="paa-pipeline/1",
        )
        manifest.content_hash = SkillPackage.hash_files(files)
        hub = tmp_path / "hostile-hub"
        hub.mkdir()
        SkillPackage(manifest, files).write(hub / "demo.echo-1.2.3.paapkg")
        (hub / "index.json").write_text(
            json.dumps(
                [
                    {
                        "package_name": "demo.echo",
                        "version": "1.2.3",
                        "kind": PIPELINE_KIND,
                        "publisher": "fixture",
                        "requires_paa": ">=4.1,<5",  # the hub's claim
                        "protocol": "paa-pipeline/1",
                    }
                ]
            ),
            encoding="utf-8",
        )
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "compatibility"
        assert "'>=99,<100'" in (report.reason or "")
        assert __version__ in (report.reason or "")
        assert not installer.staged_dir("demo.echo", "1.2.3").exists()
        assert await _marketplace_rows(db) == []

    async def test_version_omitted_installs_the_newest_satisfiable(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        hub = tmp_path / "hub"
        await _publish_fixture(tmp_path, hub=hub, version="1.2.3")
        await _publish_fixture(tmp_path, hub=hub, version="2.0.0")

        # A third entry, newest of all, whose own restated range excludes
        # this runtime: the index's restatement is the first filter, so
        # "newest satisfiable" must skip it (fetching it would refuse —
        # there are no 3.0.0 bytes in this hub at all).
        def claim_unsatisfiable(index: list[dict[str, Any]]) -> None:
            index.append({**index[0], "version": "3.0.0", "requires_paa": ">=99,<100"})

        _edit_index(hub, claim_unsatisfiable)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert report.installed
        assert report.version == "2.0.0"
        assert report.source_ref == "demo.echo@2.0.0"

    async def test_an_ambiguous_index_refuses_naming_the_candidates(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        hub, _ = await _publish_fixture(tmp_path)

        def duplicate(index: list[dict[str, Any]]) -> None:
            index.append({**index[0], "signature": "B" + index[0]["signature"][1:]})

        _edit_index(hub, duplicate)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.echo", hub=str(hub))

        assert not report.installed
        assert report.stage == "source"
        assert "ambiguous" in (report.reason or "")
        assert "1.2.3" in (report.reason or "")
        assert not installer.installed_root.exists()

    async def test_a_hub_with_no_such_package_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        hub, _ = await _publish_fixture(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.none", hub=str(hub))

        assert not report.installed
        assert report.stage == "source"
        assert "holds no package named" in (report.reason or "")

    async def test_a_hub_source_without_a_hub_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.echo")

        assert not report.installed
        assert report.stage == "source"
        assert "needs --hub" in (report.reason or "")

    async def test_a_hub_path_that_cannot_be_opened_refuses(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        # A file where a hub directory belongs: the operator's own input,
        # refused in the project's own words rather than a traceback the
        # filesystem chose.
        not_a_dir = tmp_path / "not-a-hub"
        not_a_dir.write_text("occupied", encoding="utf-8")
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.echo", hub=str(not_a_dir))

        assert not report.installed
        assert report.stage == "source"
        assert "could not be opened" in (report.reason or "")

    async def test_the_http_hub_form_reads_through_httpregistry(
        self, tmp_path: Path, db: Database, ledger: LedgerStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        hub, _ = await _publish_fixture(tmp_path)
        import paa.pipelines.packages as packages_module

        # The seam ``registry_for_hub`` documents: the URL form routes
        # through ``HttpRegistry``, and the transport — not the network —
        # is what a test replaces.
        monkeypatch.setattr(
            packages_module,
            "registry_for_hub",
            lambda _hub: _http_hub(hub, "https://hub.test"),
        )
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("hub:demo.echo", hub="https://hub.test")

        assert report.installed
        assert report.source == SOURCE_HUB
        assert report.source_ref == "demo.echo@1.2.3"


class TestGitSource:
    """The pinned-ref discipline, on local repositories (no network)."""

    async def test_a_pinned_sha_installs(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        repo, sha = _git_repo(tmp_path)
        home = tmp_path / "home"
        installer = _installer(ledger, db, home)

        report = await installer.install(
            f"git+{repo.as_uri()}", ref=sha
        )

        assert report.installed
        assert report.source == SOURCE_GIT
        assert report.source_ref == sha
        assert installer.staged_dir("demo.echo", "1.2.3").is_file() or (
            installer.staged_dir("demo.echo", "1.2.3") / "pipeline.toml"
        ).is_file()
        row = PipelineRegistry(local_dir=home / "pipelines").get("demo.echo")
        assert row is not None and row.trust == "installed"

    async def test_a_tag_installs(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        repo, _ = _git_repo(tmp_path)
        _git(repo, "tag", "v1.2.3")
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(f"git+{repo.as_uri()}", ref="v1.2.3")

        assert report.installed
        assert report.source == SOURCE_GIT

    async def test_a_moving_branch_is_refused(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        repo, _ = _git_repo(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(f"git+{repo.as_uri()}", ref="main")

        assert not report.installed
        assert report.stage == "source"
        assert "moving branch" in (report.reason or "")
        assert not installer.installed_root.exists()

    async def test_a_missing_ref_is_refused(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        repo, _ = _git_repo(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(f"git+{repo.as_uri()}")

        assert not report.installed
        assert report.stage == "source"
        assert "pinned ref" in (report.reason or "")

    async def test_an_unresolvable_ref_is_refused(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        repo, _ = _git_repo(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(
            f"git+{repo.as_uri()}", ref="0" * 40
        )

        assert not report.installed
        assert report.stage == "source"
        assert "does not resolve" in (report.reason or "")

    async def test_a_url_without_a_scheme_is_refused(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install("git+../not-a-url", ref="v1")

        assert not report.installed
        assert report.stage == "source"
        assert "scheme" in (report.reason or "")


class TestPaapkgSource:
    """The signed form, and the signature's own refusals."""

    def _paapkg(
        self, tmp_path: Path, *, sign_with: Any = None
    ) -> tuple[Path, dict[str, bytes], PackageManifest]:
        files = {
            "pipeline.toml": _DECLARATION.encode("utf-8"),
            "pipeline.py": _ENTRYPOINT.encode("utf-8"),
        }
        manifest = PackageManifest(
            package_name="demo.echo",
            version="1.2.3",
            kind=PIPELINE_KIND,
            publisher="fixture",
            description="A fixture pipeline that echoes its goal.",
            required_permissions=["PERM_SANDBOX_RUN"],
            entrypoints=["pipeline.py"],
        )
        manifest.content_hash = SkillPackage.hash_files(files)
        if sign_with is not None:
            manifest.publisher_key = sign_with.public_b64
            manifest.signature = sign(manifest.signing_bytes(), sign_with.private_b64)
        path = tmp_path / "demo.paapkg"
        SkillPackage(manifest, files).write(path)
        return path, files, manifest

    async def test_a_signed_package_from_a_trusted_key_installs(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        kp = generate_keypair()
        path, _, _ = self._paapkg(tmp_path, sign_with=kp)
        # The publisher's key is trusted the way the marketplace kit
        # does it — through the installer's own trust store.
        await PackageInstaller(db).trust_key("fixture-key", kp.public_b64, "fixture")
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install(str(path))

        assert report.installed
        assert report.source == SOURCE_PAAPKG
        assert report.signature_state == "valid"
        assert report.gates == tuple(gate.value for gate in InstallGate)

    async def test_an_unsigned_paapkg_is_refused_without_the_opt_in(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        path, _, _ = self._paapkg(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=False)

        report = await installer.install(str(path))

        assert not report.installed
        assert report.stage == "signature"
        assert "unsigned" in (report.reason or "")

    async def test_a_corrupted_signature_is_refused_even_with_the_opt_in(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot authenticate")

        kp = generate_keypair()
        path, files, manifest = self._paapkg(tmp_path, sign_with=kp)
        # Corrupt the signature but keep the key: verification must fail
        # as INVALID, which no opt-in overrides.
        manifest.signature = ("A" if manifest.signature[0] != "A" else "B") + manifest.signature[1:]
        SkillPackage(manifest, files).write(path)
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=True)

        report = await installer.install(str(path))

        assert not report.installed
        assert report.stage == "signature"
        assert "invalid" in (report.reason or "")

    async def test_a_tampered_payload_is_refused_by_the_content_hash(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        kp = generate_keypair()
        path, files, manifest = self._paapkg(tmp_path, sign_with=kp)
        files["pipeline.py"] = _ENTRYPOINT.encode("utf-8") + b"\n# tampered\n"
        SkillPackage(manifest, files).write(path)
        installer = _installer(ledger, db, tmp_path / "home", allow_unverified=True)

        report = await installer.install(str(path))

        assert not report.installed
        assert report.stage == "content_hash"

    async def test_a_wrong_kind_manifest_is_refused_at_the_declaration(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        files = {
            "pipeline.toml": _DECLARATION.encode("utf-8"),
            "pipeline.py": _ENTRYPOINT.encode("utf-8"),
        }
        manifest = PackageManifest(
            package_name="demo.echo",
            version="1.2.3",
            kind="skill",  # the archive claims to be a skill…
            publisher="fixture",
            required_permissions=["PERM_SANDBOX_RUN"],
            entrypoints=["pipeline.py"],
        )
        manifest.content_hash = SkillPackage.hash_files(files)
        path = tmp_path / "wrong-kind.paapkg"
        SkillPackage(manifest, files).write(path)
        installer = _installer(ledger, db, tmp_path / "home")

        report = await installer.install(str(path))

        assert not report.installed
        assert report.stage == "declaration"
        assert "kind" in (report.reason or "")


class TestTheRunDoor:
    """An installed pipeline runs through the shared sandboxed runner."""

    async def test_the_staged_pipeline_runs_after_install(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        installer = _installer(ledger, db, tmp_path / "home")
        report = await installer.install(str(source))
        assert report.installed and report.staged is not None

        from paa.pipelines.journal import CallJournal
        from paa.pipelines.sandboxed import SandboxedPipelineRunner

        run_id = uuid.uuid4()
        journal = CallJournal(ledger, run_id)
        await journal.run_started("demo.echo", "1.2.3", run_kind="installed")
        runner = SandboxedPipelineRunner(
            sandbox=SubprocessSandbox(),
            journal=journal,
            staged=report.staged,
            declaration=PipelineRegistry(
                local_dir=(tmp_path / "home") / "pipelines"
            ).get("demo.echo").declaration,
        )
        result = await runner.run("hello from the run door")
        await journal.run_completed(result.status.value, metrics=dict(result.metrics))

        from paa.pipelines.base import PipelineStatus

        assert result.status is PipelineStatus.COMPLETED
        assert result.summary == "echo: hello from the run door"


class TestTheOneCallDoor:
    """The CLI's door and the tests' door are one function."""

    async def test_install_pipeline_package_leaves_the_same_shape(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        source = _package_dir(tmp_path)
        home = tmp_path / "home"

        report = await install_pipeline_package(
            str(source),
            ledger=ledger,
            db=db,
            home=home,
            sandbox=SubprocessSandbox(),
            scanner=AstSecurityScanner(),
            permission_mode=PermissionMode.ASK,
            allow_unverified=True,
            approver=_approve,
        )

        assert report.installed
        assert (home / "pipelines" / "installed" / "demo.echo" / "1.2.3").is_dir()
