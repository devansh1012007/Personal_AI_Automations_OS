"""The worked examples — the docs' reference pipelines (P7, W9-C9.5).

The two examples the authoring kit ships as working references —
``examples/tdd-loop/`` (ordered stages: red, green, refactor) and
``examples/tree-of-thought/`` (branching: propose, critique, select) —
are not illustrations beside the contract; they are the contract, held
to everything a scaffolded pipeline is held to. What these tests pin:

* **on-contract as they stand in the tree** — each manifest parses
  through the same closed parser every door runs, the name matches its
  directory, all four limits are concrete, and pointing the registry's
  local scan straight at ``examples/`` lists the row with no
  ``local_errors`` (the bytes a reader finds, not a copy);
* **the author's own door works** — each example's ``test_pipeline.py``
  passes where it lands, the command the scaffold's README teaches;
* **the six-check standard passes on their bytes** — the install flow
  (the five gates, conformance, a scripted approval) installs each
  example green. The standalone ``paa pipeline test`` door arrives with
  the RPC transport (W9-C9.3, ``feat/rpc``) and runs the same six
  checks; until it lands, the install flow is the door that runs them.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from paa.ledger.store import LedgerStore
from paa.pipelines.registry import PipelineRegistry, declaration_from_manifest
from paa.storage.relational.database import Database

#: The repository's ``examples/`` directory — where the worked
#: references live beside the evals fixtures.
_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

#: The two worked examples, in the order the docs introduce them.
_WORKED = ("tdd-loop", "tree-of-thought")


def _example(name: str) -> Path:
    """One worked example's directory, as it stands in the tree."""
    return _EXAMPLES / name


class TestTheExamplesAreOnContract:
    @pytest.mark.parametrize("name", _WORKED)
    def test_the_manifest_parses_through_the_closed_parser(self, name: str) -> None:
        declaration = declaration_from_manifest(_example(name) / "pipeline.toml")
        assert declaration.name == name
        assert declaration.version == "0.1.0"
        assert declaration.resume == "journal"
        assert declaration.evidence_tier == "unrated"
        # The worked examples ask for nothing, same as the scaffold: an
        # installed pipeline holds no facade tokens in v1, so an ask
        # would be surface a human reviews for no runnable benefit.
        assert declaration.capabilities == {}

    @pytest.mark.parametrize("name", _WORKED)
    def test_all_four_limits_are_concrete_positive_ints(self, name: str) -> None:
        limits = declaration_from_manifest(_example(name) / "pipeline.toml").limits
        for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs"):
            value = getattr(limits, key)
            assert isinstance(value, int) and value >= 1, key

    @pytest.mark.parametrize("name", _WORKED)
    def test_each_example_carries_two_typed_params(self, name: str) -> None:
        params = declaration_from_manifest(_example(name) / "pipeline.toml").params
        assert len(params) == 2
        assert all(spec.type in ("int", "bool", "str") for spec in params.values())


class TestTheExamplesRegister:
    @pytest.mark.parametrize("name", _WORKED)
    def test_the_registry_lists_the_example_as_a_local_row(self, name: str) -> None:
        """The scan pointed straight at ``examples/`` — the tree's own bytes.

        The fixtures directory beside them carries no manifest, so the
        scan skips it silently; the two worked examples must be the
        only local rows it finds, with nothing named in
        ``local_errors``.
        """
        registry = PipelineRegistry(local_dir=_EXAMPLES)
        row = registry.get(name)
        assert row is not None
        assert row.trust == "local"
        assert row.runnable is False  # the local-manifest boundary, stated
        assert registry.local_errors == {}
        listed = {entry.name for entry in registry.list()}
        assert set(_WORKED) <= listed

    @pytest.mark.parametrize("name", _WORKED)
    def test_a_copy_into_the_local_dir_is_the_operators_start(
        self, name: str, tmp_path: Path
    ) -> None:
        """The README's first door, as the operator drives it.

        Copying the example under a home's ``pipelines/`` is the
        zero-infrastructure start the scaffold names; the registry must
        see exactly that copy, at local trust.
        """
        local = tmp_path / "pipelines"
        local.mkdir(parents=True)
        shutil.copytree(_example(name), local / name)
        registry = PipelineRegistry(local_dir=local)
        row = registry.get(name)
        assert row is not None
        assert row.trust == "local"
        assert registry.local_errors == {}


class TestTheAuthorsOwnDoor:
    @pytest.mark.parametrize("name", _WORKED)
    def test_the_examples_own_checks_pass_where_it_lands(self, name: str) -> None:
        """The command the scaffold's README teaches, on the example itself."""
        outcome = subprocess.run(
            [sys.executable, "-m", "pytest", "test_pipeline.py", "-p", "no:cacheprovider"],
            cwd=str(_example(name)),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr


class TestTheSixCheckStandard:
    @pytest.mark.parametrize("name", _WORKED)
    async def test_installs_green_with_all_six_checks_passing(
        self, name: str, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        """The install flow green end to end on the example's own bytes.

        The five gates, the six-check conformance standard and a
        scripted approval run against the directory exactly as
        ``paa pipeline install examples/<name> --allow-unverified``
        drives it; the staged row is what that door leaves behind —
        runnable, through the sandboxed adapter, under the installer's
        trust.
        """
        from paa.improve.gate import DECISION_APPROVED, HumanDecision
        from paa.pipelines.packages import install_pipeline_package
        from paa.sandbox.subprocess_backend import SubprocessSandbox
        from paa.validation.ast_scanner import AstSecurityScanner

        home = tmp_path / "home"

        async def approve(review: object) -> HumanDecision:
            return HumanDecision(
                decision=DECISION_APPROVED,
                approver_kind="human",
                approver_id="test-approver",
                note="the worked example's empty ask",
            )

        report = await install_pipeline_package(
            str(_example(name)),
            ledger=ledger,
            db=db,
            home=home,
            sandbox=SubprocessSandbox(),
            scanner=AstSecurityScanner(),
            allow_unverified=True,
            approver=approve,
        )
        assert report.installed, report.reason
        assert report.gates == (
            "signature",
            "content_hash",
            "ast_scan",
            "permissions",
            "smoke_test",
        )
        assert set(report.conformance) == {
            "double-run-sequence-equality",
            "divergence-halt",
            "unknown-outcome-never-auto-repeats",
            "cancel-within-grace",
            "budget-exhaustion",
            "no-secrets-in-journal",
        }
        assert all(status == "pass" for status in report.conformance.values())

        registry = PipelineRegistry(local_dir=home / "pipelines")
        row = registry.get(name)
        assert row is not None
        assert row.trust == "installed"
        assert row.runnable is True
