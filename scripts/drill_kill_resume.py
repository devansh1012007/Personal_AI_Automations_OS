"""Kill drill driver + S1 ScriptedModel proof (N3.1 / N3.2 skeleton).

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

``--iterations N`` — the N3.2 kill drill skeleton. Deliberately
NOT implemented: on any platform it exits 2 with a NOT-RUN note.
The design (see the s1-report's kill-resume section): launch the
agent in a subprocess, send ``kill -9`` at a random tool call,
restart with ``--resume``, diff the shadow tree; 20 iterations on
Linux/WSL2, 5 on Windows CI. The recovery machinery it would
exercise (sweep closure + resume classification) is already
pinned by ``tests/coder/test_recovery_chaos.py``; this file owns
the fixture, the oracle text (fix.patch + suite), and the
dry-run harness above. W3-IMPL-02 owns the driver.

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
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from paa.coder.agent import CoderAgent
from paa.coder.loop import ModelTurn, ToolCall
from paa.coder.permissions import PermissionMode
from paa.coder.workspace import Workspace, resolve_workspace
from paa.config import Settings
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent
from paa.ledger.mandate import explicit_mandate

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
        help="Kill-drill iterations (NOT-RUN skeleton; exits 2).",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep the throwaway workspace and PAA_HOME for inspection.",
    )
    args = parser.parse_args()

    if args.iterations is not None:
        print(
            "NOT-RUN: the N3.2 kill drill driver is a skeleton "
            f"(--iterations {args.iterations} requested). Design: subprocess "
            "driver, kill -9 at a random tool call, restart with --resume, "
            "shadow-tree diff oracle; 20 iterations on Linux/WSL2. Recovery "
            "machinery is pinned by tests/coder/test_recovery_chaos.py."
        )
        return 2

    return asyncio.run(dry_run(keep=args.keep))


if __name__ == "__main__":
    sys.exit(main())
