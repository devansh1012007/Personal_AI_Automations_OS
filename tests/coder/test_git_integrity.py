"""Unit tests for the host-git integrity gate (Wave 7-F H-1, ADR-0047).

The behavioural tests — a planted ``core.fsmonitor`` / clean filter never
executes — live in ``tests/coder/test_git_host_exec_gate.py`` against a real
git. These are the table-level tests: the classifier's four denial classes,
the default-refuse last-segment rule, the case-insensitivity, the bounded
refusal, the attributes scan's root-file scope, and the ``-z`` probe
parsing — including the rule the table exists for: a *future* program-naming
key git might add is denied by default, not by enumeration.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from paa.coder.git_integrity import (
    attributes_refusal,
    classify_config_key,
    repo_config_gate,
)
from paa.core.errors import PaaError

# ---------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    ("key", "denial_class"),
    [
        # Exact program-running keys from the ADR-0047 table.
        ("core.fsmonitor", "program-running"),
        ("core.hookspath", "program-running"),
        ("core.sshcommand", "program-running"),
        ("core.editor", "program-running"),
        ("core.pager", "program-running"),
        ("core.askpass", "program-running"),
        ("gpg.program", "program-running"),
        ("diff.external", "program-running"),
        ("core.attributesfile", "program-running"),
        # Last-segment default-refuse: a class, not an enumeration.
        ("diff.mytool.command", "program-running"),
        ("merge.ours.driver", "program-running"),
        ("credential.https.helper", "indirection"),  # prefix wins first
        ("filter.lfs.process", "program-running"),
        ("filter.lfs.smudge", "program-running"),
        ("filter.lfs.clean", "program-running"),
        ("diff.big.textconv", "program-running"),
        ("future.unknown.program", "program-running"),
        ("future.unknown.command", "program-running"),
        # Indirection classes.
        ("credential.helper", "indirection"),
        ("alias.stash", "indirection"),
        ("alias.ohno", "indirection"),
        ("include.path", "indirection"),
        ("includeif.gitdir:foo.path", "indirection"),
        ("protocol.version", "indirection"),
        ("url.https://evil.example.insteadof", "indirection"),
        ("url.ssh://git@corp.pushinsteadof", "indirection"),
        # Case-insensitivity, as git's own vocabulary is.
        ("CORE.FSMONITOR", "program-running"),
        ("Filter.LFS.Clean", "program-running"),
        ("Include.Path", "indirection"),
        # Benign keys stay benign.
        ("user.name", None),
        ("user.email", None),
        ("core.bare", None),
        ("core.filemode", None),
        ("core.autocrlf", None),
        ("remote.origin.url", None),
        ("branch.main.remote", None),
        ("branch.main.merge", None),
        ("core.repositoryformatversion", None),
        ("diff.renames", None),
        ("merge.conflictstyle", None),
        ("status.short", None),
        ("", None),
        ("   ", None),
    ],
)
def test_classify_config_key(key: str, denial_class: str | None) -> None:
    assert classify_config_key(key) == denial_class


def test_url_insteadof_requires_url_prefix() -> None:
    """A last segment of ``insteadof`` outside ``url.*`` is not a denial:
    the rewriting class is scoped to where git reads it."""
    assert classify_config_key("something.insteadof") is None


# ------------------------------------------------------------- probe fakes


@dataclass
class _FakeResult:
    exit_code: int
    stdout: str
    timed_out: bool = False


def _fake_runner(
    stdout: str = "", exit_code: int = 1, timed_out: bool = False
) -> tuple[Any, list[tuple[Any, ...]]]:
    """A hardened-runner fake that records the argv the gate builds and
    returns the canned probe result."""
    calls: list[tuple[Any, ...]] = []

    async def run_git(argv: tuple[str, ...], cwd: Path) -> _FakeResult:
        calls.append((argv, cwd))
        return _FakeResult(
            exit_code=exit_code, stdout=stdout, timed_out=timed_out
        )

    return run_git, calls


# ---------------------------------------------------------------- the gate


async def test_gate_passes_clean_config(tmp_path: Path) -> None:
    run_git, calls = _fake_runner(stdout="user.name\0t\n", exit_code=0)
    await repo_config_gate(tmp_path, run_git)
    assert len(calls) == 1
    argv = calls[0][0]
    assert argv[:2] == ("git", "--no-pager")
    assert "config" in argv and "--local" in argv


async def test_gate_refuses_program_key_without_leaking_value(
    tmp_path: Path,
) -> None:
    async def run_git(argv: tuple[str, ...], cwd: Path) -> _FakeResult:
        # -z entries are "key\nvalue", NUL-separated.
        return _FakeResult(
            exit_code=0,
            stdout="core.fsmonitor\ntouch /tmp/pwned; echo\0user.name\0t\n",
        )

    with pytest.raises(PaaError) as err:
        await repo_config_gate(tmp_path, run_git)
    assert "core.fsmonitor" in str(err.value)
    assert "touch" not in str(err.value), "the refusal quoted the value"


async def test_gate_refusal_is_bounded(tmp_path: Path) -> None:
    # ``-z`` shape: key\nvalue pairs, NUL-separated — nine hostile aliases.
    payload = "".join(f"alias.bad{i}\nx" + "\0" for i in range(9))
    run_git, _calls = _fake_runner(stdout=payload, exit_code=0)

    with pytest.raises(PaaError) as err:
        await repo_config_gate(tmp_path, run_git)
    msg = str(err.value)
    assert "alias.bad0" in msg
    assert "alias.bad4" in msg
    assert "alias.bad5" not in msg, "the sixth key is counted, not listed"
    assert "+4 more" in msg


async def test_gate_refuses_unreadable_config(tmp_path: Path) -> None:
    async def run_git(argv: tuple[str, ...], cwd: Path) -> _FakeResult:
        return _FakeResult(exit_code=128, stdout="")

    with pytest.raises(PaaError, match="could not be probed"):
        await repo_config_gate(tmp_path, run_git)


async def test_gate_refuses_probe_timeout(tmp_path: Path) -> None:
    async def run_git(argv: tuple[str, ...], cwd: Path) -> _FakeResult:
        return _FakeResult(exit_code=0, stdout="", timed_out=True)

    with pytest.raises(PaaError, match="timed out"):
        await repo_config_gate(tmp_path, run_git)


async def test_gate_passes_exit_one_no_matches(tmp_path: Path) -> None:
    async def run_git(argv: tuple[str, ...], cwd: Path) -> _FakeResult:
        return _FakeResult(exit_code=1, stdout="")

    await repo_config_gate(tmp_path, run_git)  # no raise


# ---------------------------------------------------------- attributes scan


def test_attributes_scan_root_files(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text("*.txt filter=probe\n", encoding="utf-8")
    refusal = attributes_refusal(tmp_path)
    assert refusal is not None and "filter" in refusal
    assert "1" in refusal  # the line number


def test_attributes_scan_info_attributes(tmp_path: Path) -> None:
    info = tmp_path / ".git" / "info" / "attributes"
    info.parent.mkdir(parents=True)
    info.write_text("*.bin filter=lfs\n", encoding="utf-8")
    assert attributes_refusal(tmp_path) is not None


def test_attributes_scan_clean_when_no_filter(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text(
        "# comment\n*.py text diff=python\n", encoding="utf-8"
    )
    assert attributes_refusal(tmp_path) is None


def test_attributes_scan_ignores_comments_and_subdirs(tmp_path: Path) -> None:
    (tmp_path / ".gitattributes").write_text(
        "# *.txt filter=commented\n", encoding="utf-8"
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".gitattributes").write_text("*.txt filter=sub\n", encoding="utf-8")
    # The subdirectory file only *selects* a driver; the driver's program
    # lives in config, which the deny table refuses. Root scan only.
    assert attributes_refusal(tmp_path) is None


def test_attributes_scan_unreadable_root_is_not_a_driver(tmp_path: Path) -> None:
    # Absent files are simply absent: git cannot run a filter from a file
    # it cannot read, so absence is not evidence of a driver.
    assert attributes_refusal(tmp_path) is None


# --------------------------------------------------- integration: the tools


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_tools_refuse_attributes_only_tamper(tmp_path: Path) -> None:
    """A ``filter=`` selection with *no* config key: the A1 regex (config
    probe alone) passed this; the ADR-0047 gate refuses it — the selected
    driver's program could arrive via any later config write."""
    import asyncio
    import subprocess

    from paa.coder.git_tools import build_git_tools
    from paa.coder.workspace import Workspace

    root = tmp_path / "ws"
    root.mkdir()
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path)}
    for args in (
        ("init", "-q", "."),
        ("config", "user.email", "t@example.invalid"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "a.txt"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True, env=env
    )
    marker = tmp_path / "marker"
    (root / ".gitattributes").write_text("*.txt filter=probe\n", encoding="utf-8")
    (root / "a.txt").write_text("changed\n", encoding="utf-8")
    # The driver program is written into config AFTER the attributes file —
    # a two-step tamper the single-probe gate must still catch on the call.
    subprocess.run(
        ["git", "-C", str(root), "config", "filter.probe.clean", f"touch {marker}; cat"],
        check=True,
        env=env,
    )
    tools = build_git_tools(Workspace(root=root))
    out = asyncio.run(tools["git_stage"].run({"paths": ["a.txt"]}))
    assert '"error"' in out
    assert not marker.exists(), "git_stage executed a clean filter on the host"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_forced_fsmonitor_flag_reaches_every_call() -> None:
    """Defence in depth: ``_run_git`` itself forces ``-c core.fsmonitor=false``
    into every argv, so the flag is dead even between probe and call."""
    import inspect

    from paa.coder import git_tools

    src = inspect.getsource(git_tools._run_git)
    assert 'core.fsmonitor=false' in src
    # And the child env keeps the host's system config out of every call.
    assert git_tools._ENV_FORCED["GIT_CONFIG_NOSYSTEM"] == "1"
