"""The ``paa pipeline publish`` door and the hub half of ``install`` (W9 C9.1).

The same discipline ``tests/cli/test_pipeline_install_cli.py`` applies to
the install door: a real settings stack against an isolated ``PAA_HOME``,
the real publish flow (which signs with the ``PAA_HOME`` publisher key and
writes the ``LocalDirectoryRegistry`` format), and — for the hub installs —
the real gates, conformance standard and review prompt behind the CliRunner.
What these tests pin is the operator-facing contract: the exit codes
(0 published/installed, 1 refused with the reason named, 2 usage) and the
output lines a human reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from paa.cli.main import app
from paa.marketplace.signing import signing_is_secure

runner = CliRunner()

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


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Every publish-door test gets a throwaway home + offline stack."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PAA_HOME", str(home))
    # Rich sizes its tables to the terminal; the assertions are about
    # content, not wrapping.
    monkeypatch.setenv("COLUMNS", "220")
    monkeypatch.setenv("PAA_MODELS__LOCAL_PROVIDER", "echo")
    monkeypatch.setenv("PAA_MODELS__ESCALATION_PROVIDER", "none")
    monkeypatch.setenv("PAA_POLICY__MODE", "AUTO")
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    monkeypatch.setenv("PAA_OBSERVABILITY__LOG_LEVEL", "WARNING")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    yield home
    reset_settings_cache()


def _package_dir(
    root: Path,
    *,
    declaration: str = _DECLARATION,
    entrypoint: str = _ENTRYPOINT,
) -> Path:
    source = root / "pkg"
    source.mkdir(parents=True)
    (source / "pipeline.toml").write_text(declaration, encoding="utf-8")
    (source / "pipeline.py").write_text(entrypoint, encoding="utf-8")
    return source


class TestTheDoorItself:
    def test_help_names_the_door(self) -> None:
        result = runner.invoke(app, ["pipeline", "publish", "--help"])
        assert result.exit_code == 0
        assert "hub" in result.stdout
        assert "sign" in result.stdout

    def test_publish_without_a_hub_is_a_usage_error(self, tmp_path: Path) -> None:
        source = _package_dir(tmp_path)

        result = runner.invoke(app, ["pipeline", "publish", str(source)])

        assert result.exit_code == 2
        assert "needs --hub" in result.stdout

    def test_publish_with_no_source_is_a_usage_error(self) -> None:
        result = runner.invoke(app, ["pipeline", "publish"])

        assert result.exit_code == 2


class TestPublishFromADirectory:
    def test_a_signed_publish_exits_zero_and_writes_the_hub(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot sign")

        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"

        result = runner.invoke(
            app, ["pipeline", "publish", str(source), "--hub", str(hub)]
        )

        assert result.exit_code == 0, result.stdout
        assert "published demo.echo v1.2.3" in result.stdout
        assert "signed" in result.stdout
        # The first-use notice: a key that appeared by itself is a key
        # the operator should know about.
        assert "new publisher identity was generated" in result.stdout
        assert (hub / "demo.echo-1.2.3.paapkg").is_file()
        assert (hub / "index.json").is_file()
        # The next hop is named on the surface, so the economy is a copy
        # away, not a manual.
        assert "pipeline install hub:demo.echo@1.2.3" in result.stdout

    def test_allow_unverified_publishes_unsigned(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"

        result = runner.invoke(
            app,
            ["pipeline", "publish", str(source), "--hub", str(hub),
             "--allow-unverified"],
        )

        assert result.exit_code == 0, result.stdout
        assert "unsigned (--allow-unverified)" in result.stdout
        assert (hub / "demo.echo-1.2.3.paapkg").is_file()

    def test_the_insecure_fallback_refuses_to_sign(
        self, tmp_path: Path, _isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import paa.pipelines.publish as publish_module

        monkeypatch.setattr(publish_module, "signing_is_secure", lambda: False)
        source = _package_dir(tmp_path)

        result = runner.invoke(
            app, ["pipeline", "publish", str(source), "--hub", str(tmp_path / "hub")]
        )

        assert result.exit_code == 1
        assert "refused demo.echo" in result.stdout
        assert "insecure HMAC dev fallback" in result.stdout
        assert not (tmp_path / "hub" / "index.json").exists()

    def test_an_off_contract_source_refuses_with_the_reason_named(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()

        result = runner.invoke(
            app, ["pipeline", "publish", str(empty), "--hub", str(tmp_path / "hub")]
        )

        assert result.exit_code == 1
        assert "refused empty" in result.stdout
        assert "no pipeline.toml" in result.stdout


class TestPublishFromAnInstalledPipeline:
    def test_an_installed_pipeline_publishes_by_name(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        if not signing_is_secure():
            pytest.skip("real signing backend not available; dev fallback cannot sign")

        source = _package_dir(tmp_path)
        installed = runner.invoke(
            app,
            ["pipeline", "install", "--allow-unverified", str(source)],
            input="y\n",
        )
        assert installed.exit_code == 0, installed.stdout
        hub = tmp_path / "hub"

        result = runner.invoke(
            app, ["pipeline", "publish", "demo.echo", "--hub", str(hub)]
        )

        assert result.exit_code == 0, result.stdout
        assert "published demo.echo v1.2.3" in result.stdout
        # The staged bytes travelled minus the local review artifact:
        # a review is this machine's record, not another machine's payload.
        import zipfile

        with zipfile.ZipFile(hub / "demo.echo-1.2.3.paapkg") as zf:
            assert "capability-review.json" not in zf.namelist()

    def test_a_missing_staged_version_refuses(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        result = runner.invoke(
            app,
            ["pipeline", "publish", "demo.echo", "--version", "9.9.9",
             "--hub", str(tmp_path / "hub")],
        )

        assert result.exit_code == 1
        assert "refused demo.echo" in result.stdout
        assert "no installed pipeline named" in result.stdout


class TestHubInstallThroughTheDoor:
    def test_a_hub_source_installs_with_the_opt_in(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        published = runner.invoke(
            app,
            ["pipeline", "publish", str(source), "--hub", str(hub),
             "--allow-unverified"],
        )
        assert published.exit_code == 0, published.stdout

        result = runner.invoke(
            app,
            ["pipeline", "install", "hub:demo.echo", "--hub", str(hub),
             "--allow-unverified"],
            input="y\n",
        )

        assert result.exit_code == 0, result.stdout
        assert "installed demo.echo v1.2.3" in result.stdout
        assert "source: hub @ demo.echo@1.2.3" in result.stdout
        staged = _isolated_home / "pipelines" / "installed" / "demo.echo" / "1.2.3"
        assert (staged / "pipeline.toml").is_file()

    def test_an_unsigned_hub_entry_without_the_opt_in_refuses_pre_gates(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        published = runner.invoke(
            app,
            ["pipeline", "publish", str(source), "--hub", str(hub),
             "--allow-unverified"],
        )
        assert published.exit_code == 0, published.stdout

        result = runner.invoke(
            app, ["pipeline", "install", "hub:demo.echo", "--hub", str(hub)],
            input="y\n",
        )

        assert result.exit_code == 1
        assert "refused demo.echo at the source stage" in result.stdout
        assert "carries no publisher signature" in result.stdout
        staged = _isolated_home / "pipelines" / "installed" / "demo.echo"
        assert not staged.exists()

    def test_an_unknown_hub_package_refuses_named(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        source = _package_dir(tmp_path)
        hub = tmp_path / "hub"
        published = runner.invoke(
            app,
            ["pipeline", "publish", str(source), "--hub", str(hub),
             "--allow-unverified"],
        )
        assert published.exit_code == 0, published.stdout

        result = runner.invoke(
            app,
            ["pipeline", "install", "hub:demo.none", "--hub", str(hub),
             "--allow-unverified"],
            input="y\n",
        )

        assert result.exit_code == 1
        assert "refused demo.none at the source stage" in result.stdout
        assert "holds no package named" in result.stdout

    def test_a_hub_source_without_a_hub_refuses(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        result = runner.invoke(
            app,
            ["pipeline", "install", "hub:demo.echo", "--allow-unverified"],
            input="y\n",
        )

        assert result.exit_code == 1
        assert "refused demo.echo at the source stage" in result.stdout
        assert "needs --hub" in result.stdout
