"""The authoring scaffold — what `paa pipeline new` writes (P7, W9-C9.5).

What these tests pin:

* **the four files, and only the four** — the scaffold's receipt is the
  whole of its write promise, and ``--force`` never touches anything
  else either;
* **valid against the real closed parser, never a copy** — the
  scaffolded manifest parses through ``declaration_from_manifest``,
  the declaration's facts are the scaffold's own (name, version,
  protocol pinned by the parse, a ``requires_runtime`` the live facade
  satisfies, all four limits concrete, tier ``unrated``), and the
  registry's local scan lists the row with no ``local_errors``;
* **the scaffolded pipeline is installable and runnable through the
  existing doors** — the five-gate install flow, the six-check
  conformance standard and a scripted approval stage the scaffold's
  own bytes, and the staged row is runnable (the row's trust is the
  installer's, not the local manifest's);
* **the author's own door works** — the generated ``test_pipeline.py``
  passes when pytest runs it in the scaffolded directory;
* **refusals name reasons** — a name that cannot be a directory, the
  reserved ``installed`` name, a taken target (naming what it holds),
  a ``--force`` that would clobber somebody else's pipeline, a parent
  that is a file.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from paa.core.errors import PaaError
from paa.ledger.store import LedgerStore
from paa.pipelines.base import FACADE_RUNTIME_VERSION
from paa.pipelines.registry import PipelineRegistry, declaration_from_manifest
from paa.pipelines.scaffold import (
    SCAFFOLD_FILES,
    SCAFFOLD_VERSION,
    ScaffoldError,
    scaffold_pipeline,
)
from paa.storage.relational.database import Database


def _scaffold(root: Path, name: str = "my-loop", *, force: bool = False) -> Path:
    """One scaffold under ``root/pipelines``; the directory it wrote."""
    return scaffold_pipeline(name, root / "pipelines", force=force).directory


class TestTheHappyPath:
    def test_the_four_files_and_only_them(self, tmp_path: Path) -> None:
        directory = _scaffold(tmp_path)
        assert sorted(entry.name for entry in directory.iterdir()) == sorted(
            SCAFFOLD_FILES
        )

    def test_the_manifest_parses_through_the_real_parser(self, tmp_path: Path) -> None:
        declaration = declaration_from_manifest(
            _scaffold(tmp_path) / "pipeline.toml"
        )
        assert declaration.name == "my-loop"
        assert declaration.version == SCAFFOLD_VERSION
        # The protocol never appears on the declaration — the parse
        # above refused anything but paa-pipeline/1 — so the facts the
        # scaffold can state are these:
        assert declaration.resume == "journal"
        assert declaration.evidence_tier == "unrated"
        assert declaration.capabilities == {}
        assert declaration.params["max_cycles"].type == "int"
        assert declaration.params["max_cycles"].maximum == 20

    def test_requires_runtime_is_satisfied_by_the_live_facade(
        self, tmp_path: Path
    ) -> None:
        """The range the scaffold writes names the running facade.

        A scaffold born off-contract on this point would be refused by
        every host that checks compatibility, so the range is pinned
        against :data:`FACADE_RUNTIME_VERSION` itself — the value the
        compatibility checks read.
        """
        declaration = declaration_from_manifest(
            _scaffold(tmp_path) / "pipeline.toml"
        )
        major, minor = (int(part) for part in FACADE_RUNTIME_VERSION.split(".")[:2])
        assert declaration.requires_runtime == f">={major}.{minor},<{major + 1}"

    def test_all_four_limits_are_concrete_positive_ints(self, tmp_path: Path) -> None:
        limits = declaration_from_manifest(_scaffold(tmp_path) / "pipeline.toml").limits
        for key in ("max_tokens", "max_wall_s", "max_calls", "max_sub_runs"):
            value = getattr(limits, key)
            assert isinstance(value, int) and value >= 1, key

    def test_the_registry_lists_the_scaffold_as_a_local_row(
        self, tmp_path: Path
    ) -> None:
        """The zero-infrastructure start: list sees it the moment it lands."""
        _scaffold(tmp_path)
        registry = PipelineRegistry(local_dir=tmp_path / "pipelines")
        row = registry.get("my-loop")
        assert row is not None
        assert row.trust == "local"
        assert row.runnable is False  # the local-manifest boundary, stated
        assert registry.local_errors == {}


class TestTheAuthoringDoors:
    async def test_the_scaffold_installs_green_and_registers_runnable(
        self, tmp_path: Path, db: Database, ledger: LedgerStore
    ) -> None:
        """The scaffold's own bytes pass the whole install flow.

        The five gates, the six-check conformance standard and a
        scripted approval run against the directory exactly as
        ``paa pipeline install <dir> --allow-unverified`` drives it; the
        staged row is what that door leaves behind — runnable, through
        the sandboxed adapter, under the installer's trust.
        """
        from paa.improve.gate import DECISION_APPROVED, HumanDecision
        from paa.pipelines.packages import install_pipeline_package
        from paa.sandbox.subprocess_backend import SubprocessSandbox
        from paa.validation.ast_scanner import AstSecurityScanner

        directory = _scaffold(tmp_path)
        home = tmp_path / "home"

        async def approve(review: object) -> HumanDecision:
            return HumanDecision(
                decision=DECISION_APPROVED,
                approver_kind="human",
                approver_id="test-approver",
                note="the scaffold's empty ask",
            )

        report = await install_pipeline_package(
            str(directory),
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
        row = registry.get("my-loop")
        assert row is not None
        assert row.trust == "installed"
        assert row.runnable is True

    def test_the_generated_test_skeleton_passes_where_it_lands(
        self, tmp_path: Path
    ) -> None:
        """The author's first command works: pytest, in the scaffolded dir."""
        directory = _scaffold(tmp_path)
        outcome = subprocess.run(
            [sys.executable, "-m", "pytest", "test_pipeline.py", "-p", "no:cacheprovider"],
            cwd=str(directory),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert outcome.returncode == 0, outcome.stdout + outcome.stderr

    def test_the_entrypoint_demo_is_deterministic(self, tmp_path: Path) -> None:
        """The double-run rule, checked on the demo the scaffold ships."""
        import importlib.util

        directory = _scaffold(tmp_path)
        spec = importlib.util.spec_from_file_location(
            "scaffold_demo_pipeline", directory / "pipeline.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        first = module.run("a goal", {"max_cycles": 3})
        second = module.run("a goal", {"max_cycles": 3})
        assert first == second
        assert first["status"] == "completed"
        assert first["metrics"] == {"cycles": 3}

    def test_the_readme_names_the_three_doors(self, tmp_path: Path) -> None:
        readme = (_scaffold(tmp_path) / "README.md").read_text(encoding="utf-8")
        assert "paa pipeline list" in readme
        assert "paa pipeline inspect my-loop" in readme
        assert "paa pipeline install" in readme
        assert "six-check standard" in readme
        assert "[limits]" in readme and "declares_tier" in readme


class TestRefusalsNameReasons:
    @pytest.mark.parametrize(
        "bad",
        [
            "../escape",
            "sub/dir",
            ".hidden",
            "",
            " ",
            "a" * 65,
            "name with spaces",
        ],
    )
    def test_unscaffoldable_names_refuse_by_reason(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ScaffoldError, match="not scaffoldable"):
            scaffold_pipeline(bad, tmp_path / "pipelines")

    def test_the_reserved_installed_name_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ScaffoldError, match="reserved"):
            _scaffold(tmp_path, "installed")

    def test_a_taken_target_refuses_naming_what_it_holds(self, tmp_path: Path) -> None:
        directory = _scaffold(tmp_path)
        with pytest.raises(ScaffoldError) as excinfo:
            _scaffold(tmp_path)
        assert str(directory) in excinfo.value.message
        assert "holds" in excinfo.value.message
        assert "pipeline.toml" in excinfo.value.message

    def test_force_overwrites_the_scaffolds_own_files_only(
        self, tmp_path: Path
    ) -> None:
        directory = _scaffold(tmp_path)
        (directory / "notes.md").write_text("the author's own notes", encoding="utf-8")
        (directory / "pipeline.py").write_text("# replaced by the author", encoding="utf-8")

        forced = _scaffold(tmp_path, force=True)

        assert (directory / "notes.md").read_text(encoding="utf-8") == (
            "the author's own notes"
        )
        entrypoint = (directory / "pipeline.py").read_text(encoding="utf-8")
        assert "# replaced by the author" not in entrypoint
        assert forced == directory
        # And the overwritten manifest still parses.
        assert declaration_from_manifest(directory / "pipeline.toml").name == "my-loop"

    def test_force_refuses_a_foreign_pipeline(self, tmp_path: Path) -> None:
        """``--force`` never clobbers a directory that is not this pipeline's."""
        foreign = tmp_path / "pipelines" / "my-loop"
        foreign.mkdir(parents=True)
        (foreign / "pipeline.toml").write_text(
            '[pipeline]\nname = "someone-else"\nversion = "1.0.0"\n'
            'protocol = "paa-pipeline/1"\nrequires_runtime = ">=4.1,<5"\n\n'
            "[limits]\nmax_tokens = 1\nmax_wall_s = 1\nmax_calls = 1\n"
            "max_sub_runs = 1\n",
            encoding="utf-8",
        )
        with pytest.raises(ScaffoldError, match="someone-else"):
            _scaffold(tmp_path, force=True)
        assert "someone-else" in (foreign / "pipeline.toml").read_text(encoding="utf-8")

    def test_force_refuses_an_unparseable_manifest(self, tmp_path: Path) -> None:
        broken = tmp_path / "pipelines" / "my-loop"
        broken.mkdir(parents=True)
        (broken / "pipeline.toml").write_text("[banana]\nx = 1\n", encoding="utf-8")
        with pytest.raises(ScaffoldError, match="does not parse"):
            _scaffold(tmp_path, force=True)
        assert "banana" in (broken / "pipeline.toml").read_text(encoding="utf-8")

    def test_a_file_where_the_target_should_be_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pipelines").mkdir(parents=True)
        (tmp_path / "pipelines" / "my-loop").write_text("a file", encoding="utf-8")
        with pytest.raises(ScaffoldError, match="a file already exists"):
            _scaffold(tmp_path)

    def test_a_parent_that_is_a_file_refuses(self, tmp_path: Path) -> None:
        parent = tmp_path / "not-a-dir"
        parent.write_text("a file", encoding="utf-8")
        with pytest.raises(ScaffoldError, match="not a directory"):
            scaffold_pipeline("my-loop", parent)

    def test_the_error_is_a_paa_error_with_details(self, tmp_path: Path) -> None:
        _scaffold(tmp_path)
        with pytest.raises(ScaffoldError) as excinfo:
            _scaffold(tmp_path)
        assert isinstance(excinfo.value, PaaError)
        assert excinfo.value.details["name"] == "my-loop"
        assert json.dumps(excinfo.value.to_payload())
