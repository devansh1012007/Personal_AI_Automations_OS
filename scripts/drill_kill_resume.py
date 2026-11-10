"""Kill drill driver + S1 ScriptedModel proof (N3.1 / N3.2).

Two modes:

``--dry-run`` (default) — the only mode that runs today. It copies
``examples/fixtures/buggy_calc`` into a throwaway workspace, then
drives a real :class:`~paa.coder.agent.CoderAgent` against a
*scripted* model (no provider, no API spend, no tokens) through the
same sequence a frontier model is asked to make on the S1 task —
read, shell-test, edit, shell-test, stage, commit — and asserts the
evidence S1 demands:

* the fixture starts with exactly one failing test;
* the ledger lineage begins with an explicit ``MANDATE_GRANTED``;
* every call is journalled (PROPOSED then COMPLETED) with
  ``args_sha256`` (64-hex) attesting the post-hook arguments, and
  the ``edit_file`` PROPOSED carries persisted intent
  (``pre_sha256`` / ``post_sha256`` over the actual file bytes);
* the ``bash`` rows attest the *actual* isolation level reported by
  the sandbox backend, never a requested one;
* every mutating call prompts in ASK mode (and no read does);
* the hash chain verifies;
* afterwards the test suite is green, the file matches the
  recorded fix byte-for-byte, and the commit the tools made is the
  head with a clean tree;
* ``fix.patch`` applied to a pristine copy of the fixture also
  makes the suite green (the fix is reproducible without any
  agent at all).

The mandate write mirrors ``src/paa/cli/coder.py::agent`` (explicit
mandate, session named, work proceeds only after it), but the
script passes ``correlation_id`` explicitly so the grant and the
journal share one lineage: spec §7's "one coder session is one
correlation id". The CLI currently mints a fresh correlation per
build (W2-IMPL-05 §13 FINDING); this script threads the id by
hand, which is also the shape a future CLI fix should produce.

Shell-test execution *inside* the sandbox is host-dependent: the
``bash`` tool spawns ``pwsh`` on Windows and ``bash`` on POSIX, so
on a Windows host without pwsh those two calls attest a spawn
failure (still journalled, isolation still recorded) and the
script verifies the suite itself with a host subprocess instead.
That split is recorded in ``docs/operations/2026-11-09-next-spec/
s1-report.md`` rather than hidden.

``--iterations N`` — the N3.2 kill drill (real driver). Each
iteration launches the scripted agent in a subprocess, sends a
hard kill (``SIGKILL`` on POSIX, ``TerminateProcess`` on Windows)
*inside* a seeded-random tool call, restarts the same session with
the CLI's ``--resume`` path, and verifies the run with a
shadow-tree diff oracle: the working tree must equal the fixture
plus the recorded fix, the suite must be green, git must show
exactly the buggy head plus one fix commit with a clean tree
(zero duplicated committed effects), the ledger must close the
interruption exactly once with a verified chain, and the resume
note's classification must match the post-kill live bytes the
parent read itself. File edits are classified precisely
(``applied`` / ``not_applied`` / ``unknown``) and gate the
verdict; ``bash`` / ``git`` / ``read_file`` interruptions report
``unknown`` and are never auto-retried (D-6). 20 iterations on
Linux/WSL2, 5 on Windows CI. The recovery machinery the drill
exercises (sweep closure + resume classification) is pinned by
``tests/coder/test_recovery_chaos.py``; this file owns the
fixture, the oracle text (fix.patch + suite), and both harnesses.
The dry-run count (47 checks) is the N3.1 evidence and is
unchanged by the drill.

Usage (from the repo root, with the dev venv):

    .venv/Scripts/python.exe scripts/drill_kill_resume.py --dry-run
    .venv/Scripts/python.exe scripts/drill_kill_resume.py --iterations 20

Exit codes: 0 = every dry-run assertion held; 1 = an assertion
failed; 2 = the requested mode is NOT-RUN here.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from paa.coder.agent import CoderAgent
from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode
from paa.coder.session import Session, TurnRole
from paa.coder.workspace import Workspace, resolve_workspace
from paa.config import Settings
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.mandate import explicit_mandate
from paa.ledger.recovery import find_interrupted_calls

#: The fixture this script proves against, resolved from the repo
#: (``scripts/`` is one level below the root).
FIXTURE: Path = (
    Path(__file__).resolve().parent.parent / "examples" / "fixtures" / "buggy_calc"
)

#: The one fix: floor division becomes true division.
BUGGY_LINE: bytes = b"    return a // b"
FIXED_LINE: bytes = b"    return a / b"

COMMIT_MESSAGE: str = "Fix divide() to use true division so fractional results are correct"

#: In-process tools attest no containment (ADR-0046: NEVER the
#: requested level, always what the tool actually runs under).
NO_CONTAINMENT: str = "NONE"
#: The subprocess sandbox backend's actual level (see
#: ``src/paa/sandbox/subprocess_backend.py``).
PROCESS_ISOLATION: str = "PROCESS"

_HEX64: int = 64


def sha256_hex(data: bytes) -> str:
    """SHA-256 hex digest (the ledger's ``args_sha256`` shape)."""
    return hashlib.sha256(data).hexdigest()


def _host_shell_detail() -> str:
    """One line naming the shell the sandbox child would spawn.

    ``pwsh`` on Windows, ``bash`` on POSIX (ADR-0045 D-10); the
    detail names whether ``shutil.which`` finds it, so the
    INFO line above separates harness proof from host capability
    without guessing.
    """
    from paa.sandbox._win32 import IS_WINDOWS

    shell = "pwsh" if IS_WINDOWS else "bash"
    found = shutil.which(shell)
    return f"{shell}={'found: ' + found if found else 'absent'}"


def is_hex64(value: str) -> bool:
    """True when ``value`` is a lowercase 64-hex digest."""
    return len(value) == _HEX64 and all(c in "0123456789abcdef" for c in value)


def run_pytest(cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the fixture suite with the same interpreter as this script.

    ``sys.executable`` by absolute path: the sandbox's child env
    deliberately drops the host PATH, so a bare ``python`` would
    not resolve there — and the real-model run should name the
    same interpreter shape the dry-run proved.
    """
    return subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def git(args: list[str], cwd: Path) -> str:
    """Run one fixed git argv in ``cwd``; raise with stderr on failure.

    Module-scope helper for the script's own setup/verification —
    never a tool on the agent's path (the agent's git tools are
    ADR-0045 fixed-argv tools, unchanged).
    """
    git_exe = shutil.which("git")
    if git_exe is None:
        raise SystemExit("git is required for the S1 dry-run setup")
    proc = subprocess.run(  # noqa: S603  # argv is fixed: resolved git + literal args from this file, no shell, no caller input
        [git_exe, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def git_apply_patch(patch: Path, cwd: Path) -> bool:
    """Whether ``patch`` applies cleanly inside ``cwd``."""
    git_exe = shutil.which("git")
    if git_exe is None:
        raise SystemExit("git is required for the S1 dry-run setup")
    proc = subprocess.run(  # noqa: S603  # argv is fixed: resolved git + literal subcommand, no shell, no caller input
        [git_exe, "apply", str(patch)],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        shell=False,
    )
    return proc.returncode == 0


class ScriptedModel:
    """A tool-calling model that only says pre-programmed turns.

    The reference shape from ``tests/coder/test_agent.py``: one
    turn per ``propose`` call, and ``text="done"`` when the script
    runs out. This stands in for the frontier provider; it proves
    the harness (loop, permissions, journal, ledger), never the
    model's reasoning — see the module docstring for the split.
    """

    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = list(turns)
        self.calls: int = 0

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls += 1
        return self._turns.pop(0) if self._turns else ModelTurn(text="done")


async def dry_run(keep: bool) -> int:
    """Run the S1 harness proof; return 0 only if every check holds."""
    failures: list[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"[{status}] {label}{suffix}")
        if not condition:
            failures.append(label)

    root = Path(tempfile.mkdtemp(prefix="paa-s1-"))
    ws: Path = root / "ws"
    home: Path = root / "paa_home"
    try:
        # --- fixture copies -------------------------------------------------
        buggy_bytes = (FIXTURE / "calc.py").read_bytes()
        assert BUGGY_LINE in buggy_bytes, "fixture drift: buggy line missing"
        fixed_bytes = buggy_bytes.replace(BUGGY_LINE, FIXED_LINE)
        assert fixed_bytes != buggy_bytes, "fixture drift: fix is a no-op"
        check(True, "fixture carries the buggy line")

        shutil.copytree(FIXTURE, ws, ignore=shutil.ignore_patterns("__pycache__"))
        (ws / "calc.py").write_bytes(buggy_bytes)

        # --- the fixture is red before the agent touches it -----------------
        pre = run_pytest(ws)
        check(pre.returncode != 0, "pre-run suite is red", "exit != 0")
        check(
            "1 failed" in pre.stdout and "test_divide_returns_fraction" in pre.stdout,
            "exactly the divide test fails",
            "1 failed + test_divide_returns_fraction in output",
        )

        # --- git state the work tools need ----------------------------------
        # Repo-local identity: git tools execute children with a
        # dropping env, so a commit must find user.name/email in
        # ``.git/config``, never in the host's global config.
        git(["init", "-b", "main"], ws)
        git(["config", "user.name", "S1 Dry Run"], ws)
        git(["config", "user.email", "s1-dry-run@localhost"], ws)
        git(["add", "-A"], ws)
        git(["commit", "-q", "-m", "S1 fixture (buggy)"], ws)
        check(True, "workspace git repo initialised (buggy head)")

        # --- one mandate, one caller ----------------------------------------
        settings = Settings(home=home)
        settings.ensure_directories()
        # Subprocess sandbox: no Docker/WSL probe on the host path.
        settings.sandbox.backend = "subprocess"

        pytest_cmd: str = f'"{sys.executable}" -m pytest test_calc.py -q'
        turns: list[ModelTurn] = [
            ModelTurn(tool_calls=[ToolCall("read_file", {"path": "calc.py"})]),
            ModelTurn(tool_calls=[ToolCall("bash", {"command": pytest_cmd})]),
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old_str": "    return a // b",
                            "new_str": "    return a / b",
                        },
                    )
                ]
            ),
            ModelTurn(tool_calls=[ToolCall("bash", {"command": pytest_cmd})]),
            ModelTurn(tool_calls=[ToolCall("git_stage", {"paths": ["calc.py"]})]),
            ModelTurn(tool_calls=[ToolCall("git_commit", {"message": COMMIT_MESSAGE})]),
            ModelTurn(
                text="divide() now returns a / b; suite green and committed."
            ),
        ]
        model = ScriptedModel(turns)

        prompts: list[str] = []

        async def confirm(tool: str, args: dict[str, Any], reason: str) -> bool:
            prompts.append(tool)
            return True

        correlation_id = new_correlation_id()
        agent = await CoderAgent.build(
            settings,
            model=model,
            workspace=Workspace(root=resolve_workspace(str(ws))),
            mode=PermissionMode.ASK,
            confirm=confirm,
            correlation_id=correlation_id,
        )
        try:
            # Mandate first, work second (ledger-before-side-effect
            # for authority). Mirrors cli/coder.py::agent.
            mandate = explicit_mandate(
                kind="coder",
                requester_id="operator",
                policy=settings.policy,
                workspace=str(ws),
                permission_mode=PermissionMode.ASK.value,
                budget=25,
            )
            await agent.runtime.ledger.append(
                LedgerEvent(
                    correlation_id=correlation_id,
                    event_type=EventType.MANDATE_GRANTED,
                    payload=mandate.granted_payload(
                        session_id=agent.session.session_id
                    ),
                )
            )
            result = await agent.ask("fix the failing test")
            check(
                result.stopped_reason == "model_finished",
                "agent run finished",
                f"stopped_reason={result.stopped_reason}",
            )
            check(
                result.tool_calls == 6,
                "six tool calls executed",
                f"tool_calls={result.tool_calls}",
            )
            check(result.denied_calls == 0, "no call denied")

            # --- ledger evidence -------------------------------------------
            events = await agent.runtime.ledger.read_correlation(correlation_id)
            check(len(events) > 0, "ledger has rows", f"{len(events)} events")
            check(
                events[0].event_type == EventType.MANDATE_GRANTED,
                "lineage head is MANDATE_GRANTED",
            )
            mandate_payload: dict[str, Any] = events[0].payload
            check(
                mandate_payload.get("basis") == "explicit",
                "mandate basis is explicit",
            )
            check(
                mandate_payload.get("identity_assurance") == "asserted",
                "identity_assurance is 'asserted' (never authority)",
            )
            check(
                is_hex64(str(mandate_payload.get("policy_version", ""))),
                "policy_version is a sha256",
            )

            ok, problems = await agent.runtime.ledger.verify_chain(correlation_id)
            check(ok, "hash chain verifies", str(problems))

            proposed = [
                e
                for e in events
                if e.event_type == EventType.TOOL_CALL_PROPOSED
            ]
            completed = [
                e
                for e in events
                if e.event_type == EventType.TOOL_CALL_COMPLETED
            ]
            by_tool: dict[str, int] = {}
            for row in proposed:
                by_tool[str(row.payload["tool"])] = (
                    by_tool.get(str(row.payload["tool"]), 0) + 1
                )
            expected_counts: dict[str, int] = {
                "read_file": 1,
                "bash": 2,
                "edit_file": 1,
                "git_stage": 1,
                "git_commit": 1,
            }
            check(by_tool == expected_counts, "PROPOSED rows per tool", str(by_tool))

            # Every PROPOSED attests post-hook args by hash + isolation.
            for row in proposed:
                tool = str(row.payload["tool"])
                check(
                    is_hex64(str(row.payload["args_sha256"])),
                    f"{tool} PROPOSED args_sha256 is hex64",
                )
                check(
                    str(row.payload["isolation_level"]) in
                    {NO_CONTAINMENT, PROCESS_ISOLATION, "NAMESPACE", "VM"},
                    f"{tool} PROPOSED isolation is a real level",
                    str(row.payload["isolation_level"]),
                )
                check(
                    len(str(row.payload["args_preview_redacted"])) > 0,
                    f"{tool} preview is non-empty (shape, never content)",
                )
            bash_rows = [
                r for r in proposed if r.payload["tool"] == "bash"
            ]
            check(
                all(r.payload["isolation_level"] == PROCESS_ISOLATION for r in bash_rows),
                "bash rows attest actual PROCESS isolation",
            )
            read_rows = [
                r for r in proposed if r.payload["tool"] == "read_file"
            ]
            check(
                all(r.payload["isolation_level"] == NO_CONTAINMENT for r in read_rows),
                "read_file rows attest NONE (in-process)",
            )

            edit_rows = [
                r for r in proposed if r.payload["tool"] == "edit_file"
            ]
            check(len(edit_rows) == 1, "exactly one edit_file PROPOSED")
            edit_row: dict[str, Any] = edit_rows[0].payload
            check(
                edit_row["pre_sha256"] == sha256_hex(buggy_bytes),
                "edit pre_sha256 hashes the buggy file",
            )
            check(
                edit_row["post_sha256"] == sha256_hex(fixed_bytes),
                "edit post_sha256 hashes the fixed file",
            )

            check(
                {str(r.payload["tool"]) for r in completed} == set(expected_counts),
                "every tool also has a COMPLETED row",
            )

            # --- ASK prompts: mutating only ---------------------------------
            expected_prompts: list[str] = [
                "bash",
                "edit_file",
                "bash",
                "git_stage",
                "git_commit",
            ]
            check(prompts == expected_prompts, "ASK prompts are mutating calls only",
                  str(prompts))
            check("read_file" not in prompts, "read_file never prompted")

            # Sandbox-execution evidence, recorded not gated: on a
            # host without the shell (``pwsh`` on Windows, ``bash``
            # on POSIX) the spawn fails inside the sandbox and the
            # tool answers ``ok=false``; the journal rows above
            # still attest hash + isolation, and the green suite is
            # verified by this script's host subprocess instead.
            bash_results = [
                t
                for t in agent.session.turns
                if t.role.value == "tool_result" and t.tool_name == "bash"
            ]
            check(
                len(bash_results) == 2,
                "both bash observations reached the session",
                f"{len(bash_results)} bash results",
            )
            spawned = 0
            for turn_result in bash_results:
                try:
                    outcome: dict[str, Any] = json.loads(turn_result.content)
                except ValueError:
                    outcome = {}
                spawned += 1 if outcome.get("ok") is True else 0
            print(
                f"[INFO] sandbox bash children that actually executed: "
                f"{spawned}/{len(bash_results)} "
                f"(host shell: {_host_shell_detail()})"
            )

            print("\nledger (state_version order):")
            for row in events:
                tool = row.payload.get("tool", "")
                print(
                    f"  v{row.state_version} {row.event_type.value}"
                    f"{f' {tool}' if tool else ''}"
                )
        finally:
            await agent.close()

        # --- tree evidence ---------------------------------------------------
        post = run_pytest(ws)
        check(post.returncode == 0, "post-run suite is green", post.stdout.strip())
        check(
            (ws / "calc.py").read_bytes() == fixed_bytes,
            "calc.py matches the fixed content byte-for-byte",
        )
        head_subject = git(["log", "-1", "--format=%s"], ws).strip()
        check(head_subject == COMMIT_MESSAGE, "HEAD is the agent's commit",
              head_subject)
        porcelain = git(["status", "--porcelain"], ws)
        check(porcelain.strip() == "", "tree clean after commit",
              porcelain.strip() or "clean")

        # --- fix.patch on a pristine copy ------------------------------------
        pristine: Path = root / "pristine"
        shutil.copytree(FIXTURE, pristine, ignore=shutil.ignore_patterns("__pycache__"))
        # Synchronous helper: no blocking subprocess runs in the
        # async function below (ruff ASYNC221).
        check(
            git_apply_patch(FIXTURE / "fix.patch", pristine),
            "fix.patch applies to a pristine copy",
        )
        patched = run_pytest(pristine)
        check(patched.returncode == 0, "pristine+patch suite is green",
              patched.stdout.strip())

        if keep:
            print(f"\nkept workspace: {ws}")
            print(f"kept paa_home: {home}")
        else:
            shutil.rmtree(root, ignore_errors=True)
    except BaseException:
        # An unexpected harness crash (not a failed check): still
        # remove the throwaway dirs, then re-raise so the traceback
        # and non-zero exit remain the record.
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
        raise

    if failures:
        print(f"\n{len(failures)} check(s) failed:")
        for label in failures:
            print(f"  - {label}")
        return 1
    print("\ndry-run: all checks held")
    return 0


# ---------------------------------------------------------------------------
# N3.2 kill drill driver (real implementation; dry-run above is untouched).
# ---------------------------------------------------------------------------

#: The ScriptedModel tool sequence shared by the dry-run and every drill
#: child, in execution order. The kill index addresses this list.
TOOL_ORDER: list[str] = [
    "read_file",
    "bash",
    "edit_file",
    "bash",
    "git_stage",
    "git_commit",
]

#: The drill's default seed: the kill index for iteration ``i`` is
#: ``random.Random(SEED + i).randrange(len(TOOL_ORDER))``, so every run
#: draws the same indices from the same sequence and a failure is
#: reproducible from the summary line. ``--seed`` changes the draws.
DRILL_SEED: int = 20261109

#: How long the parent waits for a child to reach the kill window
#: (or to exit) before watchdog-killing it and failing the iteration.
_SIGNAL_TIMEOUT_S: float = 120.0
#: How long the parent waits for a resume child to finish.
_CHILD_TIMEOUT_S: float = 180.0

#: Manifest exclusions: the git database and bytecode caches are not
#: part of the shadow tree the oracle compares (mirrors the dry-run's
#: ``copytree`` ignore of ``__pycache__``).
_ORACLE_EXCLUDED_PARTS: frozenset[str] = frozenset({".git", "__pycache__", ".pytest_cache"})


def _fixture_pair() -> tuple[bytes, bytes]:
    """``(buggy_bytes, fixed_bytes)`` for the fixture, or die on drift.

    The same bytes the dry-run proves against: the shipped ``calc.py``
    with the buggy line, and the same bytes with the one-character fix.
    """
    buggy = (FIXTURE / "calc.py").read_bytes()
    assert BUGGY_LINE in buggy, "fixture drift: buggy line missing"
    fixed = buggy.replace(BUGGY_LINE, FIXED_LINE)
    assert fixed != buggy, "fixture drift: fix is a no-op"
    return buggy, fixed


def _pytest_command() -> str:
    """The suite command the scripted model and the oracle both name."""
    return f'"{sys.executable}" -m pytest test_calc.py -q'


def _scripted_turns() -> list[ModelTurn]:
    """The full S1 sequence the fresh drill child performs."""
    pytest_cmd = _pytest_command()
    return [
        ModelTurn(tool_calls=[ToolCall("read_file", {"path": "calc.py"})]),
        ModelTurn(tool_calls=[ToolCall("bash", {"command": pytest_cmd})]),
        ModelTurn(
            tool_calls=[
                ToolCall(
                    "edit_file",
                    {
                        "path": "calc.py",
                        "old_str": "    return a // b",
                        "new_str": "    return a / b",
                    },
                )
            ]
        ),
        ModelTurn(tool_calls=[ToolCall("bash", {"command": pytest_cmd})]),
        ModelTurn(tool_calls=[ToolCall("git_stage", {"paths": ["calc.py"]})]),
        ModelTurn(tool_calls=[ToolCall("git_commit", {"message": COMMIT_MESSAGE})]),
        ModelTurn(text="divide() now returns a / b; suite green and committed."),
    ]


def tree_manifest(root: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for every oracle-visible file under ``root``.

    ``.git`` and ``__pycache__`` and ``.pytest_cache`` are excluded (see
    ``_ORACLE_EXCLUDED_PARTS``); everything else — including
    ``.gitignore``, ``fix.patch``, ``README.md`` — is compared. ``*.pyc``
    files only exist inside ``__pycache__`` and go with it.
    """
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in _ORACLE_EXCLUDED_PARTS for part in relative.parts):
            continue
        manifest[str(relative).replace("\\", "/")] = sha256_hex(path.read_bytes())
    return manifest


def expected_manifest(buggy_bytes: bytes, fixed_bytes: bytes) -> dict[str, str]:
    """The shadow the oracle diffs against: the fixture, with the fix in.

    Built from the fixture's own files (so a new fixture file changes
    the shadow instead of failing it), with ``calc.py`` replaced by
    the fixed bytes.
    """
    manifest = tree_manifest(FIXTURE)
    manifest["calc.py"] = sha256_hex(fixed_bytes)
    # The shipped fixture's calc.py is the buggy one; if that assert
    # ever fails, the shadow is built on the wrong pre-state.
    assert manifest.get("calc.py") != sha256_hex(buggy_bytes)
    return manifest


def _git_subjects(ws: Path) -> list[str]:
    """Commit subjects, newest first, for the workspace repo."""
    return git(["log", "--format=%s"], ws).splitlines()


def _git_staged(ws: Path) -> list[str]:
    """Paths staged in the index right now."""
    out = git(["diff", "--cached", "--name-only"], ws)
    return [line for line in out.splitlines() if line]


def _init_buggy_repo(ws: Path, buggy_bytes: bytes) -> None:
    """Copy the fixture and pin a buggy git head (the dry-run shape)."""
    shutil.copytree(FIXTURE, ws, ignore=shutil.ignore_patterns("__pycache__"))
    (ws / "calc.py").write_bytes(buggy_bytes)
    git(["init", "-b", "main"], ws)
    git(["config", "user.name", "S1 Dry Run"], ws)
    git(["config", "user.email", "s1-dry-run@localhost"], ws)
    git(["add", "-A"], ws)
    git(["commit", "-q", "-m", "S1 fixture (buggy)"], ws)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write one JSON document to ``path``, atomically and flushed.

    The fresh child's marker and kill-signal must survive a ``kill -9``:
    the parent learns the session id / kill window from them after the
    child is dead, so the write closes the file instead of relying on a
    later flush. The write goes through a temp file + ``os.replace`` so
    a reader never observes a half-written document — a ``kill -9``
    landing mid-write leaves either the previous (or no) file, never a
    truncated one. ``fsync`` on the temp file covers the durable case.
    """
    import os
    import tempfile

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class _KillWindowTool:
    """Wrap one tool so the drill can kill the process *inside* a call.

    DECISION: the kill lands in ``tool.run`` — the one place that is
    provably after ``journal.before`` (``TOOL_CALL_PROPOSED`` durable)
    and before ``journal.after`` (``COMPLETED`` written). Parking the
    child here with ``asyncio.Event().wait()`` widens a crash window
    of milliseconds into an unbounded one, so a real ``SIGKILL`` /
    ``TerminateProcess`` reliably lands mid-call instead of being
    racily lost between calls. The kill itself is still a genuine
    hard process kill from the parent (``proc.kill()``); only the
    window is widened. Without the window the parent's watchdog
    (signal timeout) kills the child and the iteration fails loudly:
    the drill never passes without an interruption.

    ``after_effect`` selects the side of the effect the crash lands
    on: ``False`` parks before delegating (the tool never ran, a file
    edit reads ``not_applied``); ``True`` delegates to completion
    first (the effect landed, the outcome row does not, a file edit
    reads ``applied``). The signal is written only after the delegate
    completes in after-effect mode, so the parent's kill lands
    during the park in both modes (see :meth:`run`). Alternating
    the two across iterations covers both resume classifications
    for every tool — including subprocess tools — deterministically
    instead of depending on scheduling noise.

    Isolation delegation is load-bearing: :meth:`AgentLoop._isolation_for`
    reads ``isolation_level`` / ``preflight`` off the dispatched tool,
    so ``__getattr__`` delegates them to the real tool. Hiding them
    would make every shell call attest ``NONE`` instead of the
    sandbox backend's actual level (ADR-0046: never the requested
    level, always the actual one).
    """

    def __init__(
        self,
        inner: Any,
        name: str,
        *,
        sequencer: list[int],
        kill_at: int,
        after_effect: bool,
        signal_path: Path,
    ) -> None:
        self._inner = inner
        self._name = name
        self._sequencer = sequencer
        self._kill_at = kill_at
        self._after_effect = after_effect
        self._signal_path = signal_path

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return str(self._inner.description)

    def schema(self) -> dict[str, Any]:
        result: dict[str, Any] = self._inner.schema()
        return result

    def __getattr__(self, item: str) -> Any:
        # Only reached for attributes the wrapper does not define
        # itself (``isolation_level``, ``preflight``): delegate to the
        # real tool so the loop attests what execution actually runs
        # under.
        return getattr(self._inner, item)

    async def run(self, arguments: Mapping[str, Any]) -> str:
        index = self._sequencer[0]
        self._sequencer[0] += 1
        if index != self._kill_at:
            result: str = await self._inner.run(arguments)
            return result
        # The signal is written *after* the delegate in after-effect
        # mode and *before* any delegation in before-effect mode, so
        # the parent's hard kill lands during the park in both cases:
        # the effect has fully landed (after) or never started
        # (before). Writing the signal first and delegating second
        # would race the kill against the delegate for subprocess
        # tools — a slow ``git``/``bash`` delegate would be killed
        # mid-flight despite the after-effect label, which the seed-14
        # validation run demonstrated on ``git_commit``.
        if self._after_effect:
            await self._inner.run(arguments)
        _write_json(
            self._signal_path,
            {
                "kill_at": self._kill_at,
                "tool": self._name,
                "after_effect": self._after_effect,
            },
        )
        # Parked until the parent hard-kills this process. If the
        # parent never kills, its watchdog does (signal timeout), and
        # the iteration fails; this line never returns by design.
        await asyncio.Event().wait()
        raise AssertionError("kill window returned without a kill")


async def _child_settings(home: Path) -> Settings:
    settings = Settings(home=home)
    settings.ensure_directories()
    settings.sandbox.backend = "subprocess"
    return settings


async def _child_append_mandate(
    agent: CoderAgent, settings: Settings, correlation_id: Any, ws: Path
) -> None:
    """The pre-work explicit mandate (mirrors dry-run and the CLI)."""
    mandate = explicit_mandate(
        kind="coder",
        requester_id="operator",
        policy=settings.policy,
        workspace=str(ws),
        permission_mode=PermissionMode.ASK.value,
        budget=25,
    )
    await agent.runtime.ledger.append(
        LedgerEvent(
            correlation_id=correlation_id,
            event_type=EventType.MANDATE_GRANTED,
            payload=mandate.granted_payload(session_id=agent.session.session_id),
        )
    )


async def _auto_yes(tool: str, args: dict[str, Any], reason: str) -> bool:
    return True


async def child_fresh(
    *,
    ws: Path,
    home: Path,
    correlation_id: Any,
    kill_at: int,
    after_effect: bool,
    signal_path: Path,
    marker_path: Path,
) -> int:
    """One fresh drill run: the full S1 sequence, killed mid-call.

    Writes the session marker first (the parent resumes from it), then
    runs the agent with every tool wrapped in a kill window at
    ``kill_at``. Reaching the end means the window never fired (the
    wrap seam broke); the result file says so and the parent fails
    the iteration rather than passing a run nobody interrupted.
    """
    settings = await _child_settings(home)
    session = Session(transcript_dir=home / "sessions")
    _write_json(
        marker_path,
        {"session_id": session.session_id, "correlation_id": str(correlation_id)},
    )
    agent = await CoderAgent.build(
        settings,
        session=session,
        model=ScriptedModel(_scripted_turns()),
        workspace=Workspace(root=resolve_workspace(str(ws))),
        mode=PermissionMode.ASK,
        confirm=_auto_yes,
        correlation_id=correlation_id,
    )
    try:
        sequencer: list[int] = [0]
        for tool_name in list(agent.tools):
            agent.tools[tool_name] = _KillWindowTool(
                agent.tools[tool_name],
                tool_name,
                sequencer=sequencer,
                kill_at=kill_at,
                after_effect=after_effect,
                signal_path=signal_path,
            )
        await _child_append_mandate(agent, settings, correlation_id, ws)
        result = await agent.ask("fix the failing test")
        _write_json(
            marker_path,
            {
                "session_id": session.session_id,
                "correlation_id": str(correlation_id),
                "killed": False,
                "stopped_reason": result.stopped_reason,
                "tool_calls": result.tool_calls,
            },
        )
        return 0
    finally:
        await agent.close()


def _resume_inspection(ws: Path, fixed_bytes: bytes, buggy_bytes: bytes) -> dict[str, Any]:
    """What the resume child observes before choosing continuation turns.

    The observations are the D-6 discipline made concrete: the resumed
    model inspects the tree and the git state instead of blindly
    re-running whatever was interrupted. ``calc_state`` names all three
    file possibilities (``buggy`` / ``fixed`` / ``other``) because the
    oracle gates on distinguishing them.
    """
    live = (ws / "calc.py").read_bytes()
    if live == fixed_bytes:
        calc_state = "fixed"
    elif live == buggy_bytes:
        calc_state = "buggy"
    else:
        calc_state = "other"
    subjects = _git_subjects(ws)
    return {
        "calc_state": calc_state,
        "has_fix_commit": COMMIT_MESSAGE in subjects,
        "staged": _git_staged(ws),
    }


def _resume_turns(inspection: dict[str, Any]) -> list[ModelTurn]:
    """Continuation turns for a resumed run, from inspected state only.

    No ``bash`` turn is ever emitted: a shell interruption classifies
    ``unknown`` and is never auto-retried (D-6). The parent's host
    pytest is the suite check on this path, exactly as the dry-run
    split documents. ``git_stage`` / ``git_commit`` are emitted only
    when inspection proves the fix commit does not exist yet —
    inspect-then-act, not blind repeat — and never when the file is in
    an unrecognised state (``other``), where the only honest move is
    to stop and let the oracle name the torn tree.
    """
    if inspection["calc_state"] == "other":
        return [ModelTurn(text="resumed: unexpected calc.py state; stopped.")]
    turns: list[ModelTurn] = []
    if inspection["calc_state"] == "buggy":
        turns.append(
            ModelTurn(
                tool_calls=[
                    ToolCall(
                        "edit_file",
                        {
                            "path": "calc.py",
                            "old_str": "    return a // b",
                            "new_str": "    return a / b",
                        },
                    )
                ]
            )
        )
    if not inspection["has_fix_commit"]:
        if "calc.py" not in inspection["staged"]:
            turns.append(ModelTurn(tool_calls=[ToolCall("git_stage", {"paths": ["calc.py"]})]))
        turns.append(ModelTurn(tool_calls=[ToolCall("git_commit", {"message": COMMIT_MESSAGE})]))
    calc_state = str(inspection["calc_state"])
    fix_commit = inspection["has_fix_commit"]
    staged = inspection["staged"]
    turns.append(
        ModelTurn(
            text=f"resumed: calc_state={calc_state} fix_commit={fix_commit} staged={staged}"
        )
    )
    return turns


async def child_resume(
    *,
    ws: Path,
    home: Path,
    session_id: str,
    result_path: Path,
    fixed_bytes: bytes,
    buggy_bytes: bytes,
) -> int:
    """One resume drill run: ``--resume`` and finish the S1 sequence.

    ``Session.resume`` + ``CoderAgent.build`` is the CLI's ``--resume``
    path ([VERIFIED src/paa/coder/agent.py:267-268]):
    :func:`~paa.coder.agent._inject_resume_recovery_note` runs at build
    time, closing every interrupted call on its own lineage as
    ``FAILED(reason="interrupted")`` and injecting the classification
    note into the transcript. The result file carries the note and the
    inspection the continuation was chosen from, for the parent's
    oracle to check.
    """
    settings = await _child_settings(home)
    inspection = _resume_inspection(ws, fixed_bytes, buggy_bytes)
    session = Session.resume(session_id, home / "sessions")
    correlation_id = new_correlation_id()
    agent = await CoderAgent.build(
        settings,
        session=session,
        model=ScriptedModel(_resume_turns(inspection)),
        workspace=Workspace(root=resolve_workspace(str(ws))),
        mode=PermissionMode.ASK,
        confirm=_auto_yes,
        correlation_id=correlation_id,
    )
    try:
        await _child_append_mandate(agent, settings, correlation_id, ws)
        result = await agent.ask("fix the failing test (resumed)")
        notes = [
            turn.content
            for turn in session.turns
            if turn.role is TurnRole.SYSTEM
        ]
        ok, problems = await agent.runtime.ledger.verify_chain(correlation_id)
        _write_json(
            result_path,
            {
                "session_id": session.session_id,
                "correlation_id": str(correlation_id),
                "stopped_reason": result.stopped_reason,
                "tool_calls": result.tool_calls,
                "denied_calls": result.denied_calls,
                "inspection": inspection,
                "recovery_notes": notes,
                "chain_ok": ok,
                "chain_problems": problems,
            },
        )
        return 0
    finally:
        await agent.close()


async def _spawn_child(
    argv: list[str], *, log_path: Path
) -> asyncio.subprocess.Process:
    """Spawn one drill child with its output captured to a log file."""
    log_fh = log_path.open("wb")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=log_fh,
        stderr=log_fh,
    )
    return proc


async def _wait_for_signal_or_exit(
    proc: asyncio.subprocess.Process, signal_path: Path, timeout_s: float
) -> str:
    """Wait until the kill window signals or the child exits.

    Returns ``"signaled"`` (the caller sends the hard kill),
    ``"exited"`` (the child finished on its own), or ``"timeout"``
    (the caller watchdog-kills and fails the iteration). Polling for
    a signal file with the child reaped in slices keeps the wait
    event-bounded without threads; the sleeps below are the wait
    itself, not a race fix.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if signal_path.exists():
            return "signaled"
        if proc.returncode is not None:
            return "exited"
        if loop.time() >= deadline:
            return "timeout"
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.1)
        except TimeoutError:
            continue


async def _read_ledger_verdict(
    home: Path, fresh_id: Any, resume_id: Any
) -> dict[str, Any]:
    """Re-read both lineages from the throwaway PAA_HOME (parent side).

    Opens ``Database`` + ``LedgerStore`` directly — no ``Runtime``
    boot, so no recovery sweep appends here: this is a read-only
    oracle over committed rows. ``settings.storage.sqlite_path`` is
    ``<home>/state/paa.db`` via ``_propagate_home``.
    """
    from paa.ledger.store import LedgerStore
    from paa.storage.relational.database import Database

    settings = Settings(home=home)
    db = Database(settings.storage.sqlite_path)
    await db.connect()
    try:
        store = LedgerStore(db)
        fresh_events = await store.read_correlation(fresh_id)
        resume_events = await store.read_correlation(resume_id)
        fresh_ok, fresh_problems = await store.verify_chain(fresh_id)
        resume_ok, resume_problems = await store.verify_chain(resume_id)

        def row(e: Any) -> dict[str, str]:
            return {
                "tool": str(e.payload.get("tool", "")),
                "call_id": str(e.payload.get("call_id", "")),
            }

        def summarize(events: Any) -> dict[str, Any]:
            return {
                "count": len(events),
                "proposed": [
                    row(e)
                    for e in events
                    if e.event_type is EventType.TOOL_CALL_PROPOSED
                ],
                "completed": [
                    row(e)
                    for e in events
                    if e.event_type is EventType.TOOL_CALL_COMPLETED
                ],
                "interrupted": [
                    row(e)
                    for e in events
                    if e.event_type is EventType.TOOL_CALL_FAILED
                    and e.payload.get("reason") == "interrupted"
                ],
                "still_open": [str(c.tool) for c in find_interrupted_calls(list(events))],
                "chain_ok": None,  # filled below
            }

        fresh_summary = summarize(fresh_events)
        fresh_summary["chain_ok"] = fresh_ok
        fresh_summary["chain_problems"] = fresh_problems
        resume_summary = summarize(resume_events)
        resume_summary["chain_ok"] = resume_ok
        resume_summary["chain_problems"] = resume_problems
        return {"fresh": fresh_summary, "resume": resume_summary}
    finally:
        await db.close()


async def _one_drill_iteration(
    index: int, iterations: int, *, seed: int, keep: bool
) -> bool:
    """One kill+resume iteration; True only on a fully held verdict.

    The verdict has four conjuncts, each against independent
    evidence: (1) the shadow-tree diff is empty and the suite is
    green, (2) git shows exactly the buggy head plus one fix commit
    with a clean tree (zero duplicated committed effects), (3) the
    ledger closes the interruption exactly once per lineage with a
    verified chain, and (4) the interrupted call's resume
    classification matches the post-kill live bytes the parent read
    itself (the file-tool gate) — shell/git/read interruptions
    classify ``unknown`` by design (D-6) and their completion is the
    host-side check, never a blind agent retry.
    """
    # Seeded by design: reproducibility, not unpredictability — a
    # failing draw must replay identically from its summary line.
    rng = random.Random(seed + index)  # noqa: S311
    kill_at = rng.randrange(len(TOOL_ORDER))
    after_effect = index % 2 == 1
    expected_tool = TOOL_ORDER[kill_at]
    buggy_bytes, fixed_bytes = _fixture_pair()
    script_path = Path(__file__).resolve()

    root = Path(tempfile.mkdtemp(prefix=f"paa-drill-{index}-"))
    ws: Path = root / "ws"
    home: Path = root / "paa_home"
    signal_path: Path = root / "kill-signal.json"
    marker_path: Path = root / "child-marker.json"
    resume_result_path: Path = root / "resume-result.json"
    failures: list[str] = []

    def check(condition: bool, label: str, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(f"  [{status}] {label}{suffix}")
        if not condition:
            failures.append(label)

    print(
        f"[ITER {index + 1}/{iterations}] kill_at={kill_at} tool={expected_tool} "
        f"window={'after-effect' if after_effect else 'before-effect'}"
    )
    try:
        _init_buggy_repo(ws, buggy_bytes)
        expected = expected_manifest(buggy_bytes, fixed_bytes)

        fresh_id = new_correlation_id()
        fresh_argv = [
            sys.executable,
            str(script_path),
            "--child",
            "--child-kind",
            "fresh",
            "--workspace",
            str(ws),
            "--home",
            str(home),
            "--correlation-id",
            str(fresh_id),
            "--kill-at",
            str(kill_at),
        ]
        if after_effect:
            fresh_argv.append("--after-effect")
        fresh_argv += [
            "--signal-file",
            str(signal_path),
            "--marker-file",
            str(marker_path),
        ]
        fresh_proc = await _spawn_child(fresh_argv, log_path=root / "child-fresh.log")
        outcome = await _wait_for_signal_or_exit(
            fresh_proc, signal_path, _SIGNAL_TIMEOUT_S
        )
        if outcome == "signaled":
            # proc.kill() is SIGKILL (POSIX) / TerminateProcess
            # (Windows): the OS kills the agent process outright,
            # which is the crash the drill is named for. POSIX
            # reports returncode -9; Windows reports a non-zero
            # exit. Either way the process dies without running
            # destructors, so a stranded PROPOSED-without-outcome
            # is the expected ledger state afterwards.
            fresh_proc.kill()
            rc = await fresh_proc.wait()
            check(rc != 0, "fresh child died by hard kill", f"returncode={rc}")
        else:
            detail = f"outcome={outcome} returncode={fresh_proc.returncode}"
            if fresh_proc.returncode is None:
                fresh_proc.kill()
                await fresh_proc.wait()
                detail += " (watchdog-killed)"
            check(False, "fresh child reached the kill window", detail)
            # No interruption to resume from: the iteration cannot
            # prove anything, so it fails here with the logs kept.
            raise _IterationAborted

        check(marker_path.exists(), "child marker names the session")
        marker: dict[str, Any] = json.loads(marker_path.read_text(encoding="utf-8"))
        session_id = str(marker["session_id"])
        check(
            marker.get("correlation_id") == str(fresh_id),
            "child ran on the parent's correlation",
        )
        # The child writes the signal atomically (temp + os.replace), so
        # a reader never sees a half-written document. A short retry loop
        # covers the remaining race where the parent's poll observed the
        # path just before the rename landed.
        signal = None
        for _ in range(20):
            try:
                signal = json.loads(signal_path.read_text(encoding="utf-8"))
                break
            except (OSError, json.JSONDecodeError):
                await asyncio.sleep(0.05)
        check(signal is not None, "kill-signal file readable")
        assert signal is not None
        check(signal.get("tool") == expected_tool, "signal names the killed tool")
        check(
            signal.get("kill_at") == kill_at, "signal carries the kill index"
        )

        # The parent — not the child — reads the post-kill live bytes
        # that the resume classification must match (the gate).
        post_kill_bytes = (ws / "calc.py").read_bytes()
        if post_kill_bytes == fixed_bytes:
            kill_file_state = "applied"
        elif post_kill_bytes == buggy_bytes:
            kill_file_state = "not_applied"
        else:
            kill_file_state = "unknown"

        resume_argv = [
            sys.executable,
            str(script_path),
            "--child",
            "--child-kind",
            "resume",
            "--workspace",
            str(ws),
            "--home",
            str(home),
            "--session-id",
            session_id,
            "--result-file",
            str(resume_result_path),
        ]
        resume_proc = await _spawn_child(
            resume_argv, log_path=root / "child-resume.log"
        )
        try:
            resume_rc = await asyncio.wait_for(
                resume_proc.wait(), timeout=_CHILD_TIMEOUT_S
            )
        except TimeoutError:
            resume_proc.kill()
            resume_rc = await resume_proc.wait()
            check(False, "resume child finished in time", f"returncode={resume_rc}")
        else:
            check(resume_rc == 0, "resume child exited cleanly", f"returncode={resume_rc}")

        result: dict[str, Any] = {}
        if resume_result_path.exists():
            result = json.loads(resume_result_path.read_text(encoding="utf-8"))
        check(bool(result), "resume child wrote its result")

        # --- shadow-tree diff oracle -----------------------------------
        actual = tree_manifest(ws)
        diff = sorted(
            k for k in set(actual) | set(expected) if actual.get(k) != expected.get(k)
        )
        check(diff == [], "shadow-tree diff empty (fixed tree)", str(diff))
        suite = run_pytest(ws)
        check(suite.returncode == 0, "suite green after resume", suite.stdout.strip())

        # --- zero duplicated committed effects -------------------------
        subjects = _git_subjects(ws)
        fix_count = subjects.count(COMMIT_MESSAGE)
        check(len(subjects) == 2, "exactly buggy head + one commit", str(subjects))
        check(fix_count == 1, "fix commit exists exactly once", f"count={fix_count}")
        check(
            bool(subjects) and subjects[0] == COMMIT_MESSAGE,
            "HEAD is the fix commit",
            subjects[0] if subjects else "<no commits>",
        )
        porcelain = git(["status", "--porcelain"], ws)
        check(porcelain.strip() == "", "tree clean after resume", porcelain.strip() or "clean")

        # --- ledger oracle ----------------------------------------------
        resume_id = result.get("correlation_id", "")
        ledger: dict[str, Any] = {"fresh": {}, "resume": {}}
        if resume_id:
            ledger = await _read_ledger_verdict(home, fresh_id, resume_id)
        fresh_rows: dict[str, Any] = ledger["fresh"]
        resume_rows: dict[str, Any] = ledger["resume"]
        check(
            fresh_rows.get("chain_ok") is True,
            "fresh lineage hash chain verifies",
            str(fresh_rows.get("chain_problems", "")),
        )
        check(
            resume_rows.get("chain_ok") is True,
            "resume lineage hash chain verifies",
            str(resume_rows.get("chain_problems", "")),
        )
        interrupted_tools = [r["tool"] for r in fresh_rows.get("interrupted", [])]
        check(
            interrupted_tools == [expected_tool],
            "exactly one interrupted close, of the killed tool",
            str(interrupted_tools),
        )
        check(
            fresh_rows.get("still_open", ["?"]) == [],
            "no interrupted call left open on the fresh lineage",
        )

        # --- classification gate (file tools) / D-6 (shell+git) ---------
        # The token checks are exact note-line shapes (``: <state> —``,
        # see agent._resume_entry) rather than bare substrings: the note
        # trailer always mentions ``unknown``, and ``applied`` is a
        # substring of ``not_applied``, so a substring check would pass
        # even when the per-call verdict was the wrong one.
        notes = "\n".join(str(n) for n in result.get("recovery_notes", []))
        check(expected_tool in notes, "recovery note names the interrupted call")
        if expected_tool == "edit_file":
            check(
                f": {kill_file_state} —" in notes,
                f"edit classified {kill_file_state!r} (matches live bytes)",
                notes.replace("\n", " | ")[:240],
            )
            if kill_file_state == "applied":
                check(
                    ": not_applied —" not in notes,
                    "applied note does not also claim not_applied",
                )
            completed_edits = [
                r
                for r in list(fresh_rows.get("completed", []))
                + list(resume_rows.get("completed", []))
                if r["tool"] == "edit_file"
            ]
            if kill_file_state == "applied":
                check(
                    completed_edits == [],
                    "applied edit never re-completed (no duplicate effect)",
                    str(completed_edits),
                )
            elif kill_file_state == "not_applied":
                check(
                    len(completed_edits) == 1,
                    "not_applied edit retried exactly once",
                    str(completed_edits),
                )
            else:
                check(
                    completed_edits == [],
                    "unknown edit (torn bytes) not re-run",
                    str(completed_edits),
                )
        else:
            check(
                ": unknown —" in notes,
                f"{expected_tool} reported unknown (D-6: never auto-retried)",
                notes.replace("\n", " | ")[:240],
            )

        # Inspect-then-act evidence: a resumed git_commit is only
        # legitimate when inspection proved no fix commit existed.
        completed_commits = [
            r for r in resume_rows.get("completed", []) if r["tool"] == "git_commit"
        ]
        if completed_commits:
            inspection = result.get("inspection", {})
            check(
                inspection.get("has_fix_commit") is False,
                "resumed commit preceded by no-fix-commit inspection",
            )
        check(
            len(completed_commits) <= 1,
            "at most one resumed git_commit completed",
            str(completed_commits),
        )

    except _IterationAborted:
        pass
    except BaseException:
        # An unexpected harness crash (not a failed check): keep the
        # iteration dirs, print the traceback, and fail the run.
        print(f"  iteration root kept for inspection: {root}")
        raise
    finally:
        if failures or keep:
            print(
                f"  iteration {index + 1} workspace: {ws} (home: {home})"
            )
        if not failures and not keep:
            shutil.rmtree(root, ignore_errors=True)

    if failures:
        print(f"[ITER {index + 1}/{iterations}] FAIL ({len(failures)} checks)")
        return False
    print(f"[ITER {index + 1}/{iterations}] PASS (tree-correct, zero-duplicate)")
    return True


class _IterationAborted(Exception):
    """The iteration stopped early (e.g. the kill never landed)."""


async def drill_iterations(iterations: int, *, seed: int, keep: bool) -> int:
    """Run ``iterations`` kill+resume iterations; 0 only if all pass.

    Linux/WSL2 is the plan's 20-iteration surface (POSIX ``kill -9``);
    on a Windows host the same driver produces the TerminateProcess
    subset — a 5-iteration run is the documented Windows gate and any
    further iterations beyond that count are recorded as Linux-gated
    when the host is Windows. The header states the subset plainly
    so a Windows pass can never be read as the 20x Linux result.
    """
    from paa.sandbox._win32 import IS_WINDOWS

    subset = (
        "Windows TerminateProcess subset (Linux/WSL2 20x kill -9: NOT-RUN here)"
        if IS_WINDOWS
        else "POSIX kill -9"
    )
    print(f"kill drill: {iterations} iteration(s), seed={seed}, {subset}")
    passed = 0
    for index in range(iterations):
        if await _one_drill_iteration(index, iterations, seed=seed, keep=keep):
            passed += 1
    print(f"\nkill drill: {passed}/{iterations} iterations passed")
    return 0 if passed == iterations else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ScriptedModel S1 proof (the default mode).",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=None,
        help="Kill-drill iterations (subprocess driver, kill at a random "
        "tool call, --resume, shadow-tree oracle).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DRILL_SEED,
        help="Kill-index seed (reproducible draws; default %(default)s).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the throwaway workspace and PAA_HOME for inspection.",
    )
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--child-kind", choices=["fresh", "resume"], default="fresh", help=argparse.SUPPRESS
    )
    parser.add_argument("--workspace", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--home", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--correlation-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--session-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--kill-at", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument(
        "--after-effect", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--signal-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--marker-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--result-file", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.child:
        if args.workspace is None or args.home is None:
            print("child mode requires --workspace and --home", file=sys.stderr)
            return 2
        if args.child_kind == "fresh":
            if (
                args.correlation_id is None
                or args.signal_file is None
                or args.marker_file is None
            ):
                print("fresh child requires correlation/signal/marker paths", file=sys.stderr)
                return 2
            import uuid

            return asyncio.run(
                child_fresh(
                    ws=Path(args.workspace),
                    home=Path(args.home),
                    correlation_id=uuid.UUID(args.correlation_id),
                    kill_at=args.kill_at,
                    after_effect=args.after_effect,
                    signal_path=Path(args.signal_file),
                    marker_path=Path(args.marker_file),
                )
            )
        if args.session_id is None or args.result_file is None:
            print("resume child requires --session-id and --result-file", file=sys.stderr)
            return 2
        buggy_bytes, fixed_bytes = _fixture_pair()
        return asyncio.run(
            child_resume(
                ws=Path(args.workspace),
                home=Path(args.home),
                session_id=args.session_id,
                result_path=Path(args.result_file),
                fixed_bytes=fixed_bytes,
                buggy_bytes=buggy_bytes,
            )
        )

    if args.iterations is not None:
        if args.iterations < 1:
            print("--iterations must be >= 1", file=sys.stderr)
            return 2
        return asyncio.run(
            drill_iterations(args.iterations, seed=args.seed, keep=args.keep)
        )

    return asyncio.run(dry_run(keep=args.keep))


if __name__ == "__main__":
    sys.exit(main())
