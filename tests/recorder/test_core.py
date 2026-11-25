"""The recorder core end to end (C7.3): the done-when, as a real run.

The spec's acceptance line — *a harness session runs under ``paa record``
and produces a verifying bundle; unsandboxable effects honestly
labelled* — is pinned here with a scripted fake harness that does what
a real one does: it runs under the sandbox session, it makes tool calls
through the MCP policy proxy (a real subprocess of the real CLI), it
does some work outside the proxy, and then it ends — cleanly, by
self-SIGKILL, or by overrunning the wall clock.

The chain that matters: fake harness (real child) → proxy (real
``paa record proxy`` subprocess over real stdio) → flight record (real
hash chain on disk) → attested bundle (real Ed25519 signature) →
``verify_bundle`` green. No mocks between the harness and the disk.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from paa.attest.verify import verify_bundle
from paa.recorder.core import PolicySpec, RecorderCore
from paa.recorder.record import verify_record
from paa.sandbox.subprocess_backend import SubprocessSandbox

#: The cooperating harness: mediates some calls, does some work alone,
#: meets a denial, then exits cleanly. This is the script a Claude
#: Code / Codex / Gemini CLI session plays back in miniature.
COOPERATING_HARNESS = '''
import json, os, subprocess, sys

run_dir = os.environ["PAA_RECORD_RUN_DIR"]
assert os.environ["PAA_RECORD_WORKSPACE"]

# An unmediated effect: the harness's own hands, no proxy involved.
with open("unmediated.txt", "w") as fh:
    fh.write("the harness did this alone")

proxy = subprocess.Popen(
    [sys.executable, "-c",
     "from paa.cli.main import app; app()",
     "record", "proxy", "--run-dir", run_dir],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

def send(msg):
    proxy.stdin.write(json.dumps(msg) + "\\n"); proxy.stdin.flush()

def recv():
    line = proxy.stdout.readline()
    assert line, "proxy died"
    return json.loads(line)

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05"}})
assert recv()["result"]["protocolVersion"] == "2024-11-05"

send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["echo", "mediated hello"]}}})
assert "mediated hello" in recv()["result"]["content"][0]["text"]

send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["sh", "-c", "exit 3"]}}})
assert recv()["result"]["isError"] is True

send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
      "params": {"name": "write_file",
                 "arguments": {"path": "made-by-proxy.txt",
                               "content": "via paa"}}})
assert recv()["result"]["isError"] is False

send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["rm", "-rf", "anything"]}}})
assert recv()["error"]["code"] == -32002

proxy.stdin.close()
assert proxy.wait(timeout=60) == 0
print("fake harness done")
'''

#: The harness that dies mid-flight: one mediated write, then SIGKILL.
KILLED_HARNESS = '''
import json, os, signal, subprocess, sys

run_dir = os.environ["PAA_RECORD_RUN_DIR"]
proxy = subprocess.Popen(
    [sys.executable, "-c",
     "from paa.cli.main import app; app()",
     "record", "proxy", "--run-dir", run_dir],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

def send(msg):
    proxy.stdin.write(json.dumps(msg) + "\\n"); proxy.stdin.flush()

def recv():
    return json.loads(proxy.stdout.readline())

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05"}})
recv()
send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "write_file",
                 "arguments": {"path": "before-the-kill.txt",
                               "content": "survives"}}})
recv()
proxy.stdin.close()
proxy.wait(timeout=60)
print("about to self-destruct", flush=True)
os.kill(os.getpid(), signal.SIGKILL)
'''


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PAA_HOME", str(h))
    monkeypatch.setenv("PAA_SANDBOX__BACKEND", "subprocess")
    from paa.config import reset_settings_cache

    reset_settings_cache()
    return h


def _harness_file(tmp_path: Path, source: str) -> tuple[str, ...]:
    script = tmp_path / "fake_harness.py"
    script.write_text(source, encoding="utf-8")
    return (sys.executable, "-u", str(script))


class TestTheDoneWhen:
    async def test_a_cooperating_harness_session_produces_a_verifying_bundle(
        self, tmp_path: Path, home: Path
    ) -> None:
        """The C7.3 acceptance line, played back by the scripted harness."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "existing.txt").write_text("before\n", encoding="utf-8")

        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            _harness_file(tmp_path, COOPERATING_HARNESS),
            workspace=workspace,
            timeout_s=120,
            policy=PolicySpec(
                mode="ask",
                allow=("bash(echo *)", "bash(sh *)", "write_file", "read_file"),
                deny=("bash(rm *)",),
            ),
            child_env={"PAA_SANDBOX__BACKEND": "subprocess"},
        )

        # The subject's fate is stated, not absorbed.
        assert outcome.state == "clean_exit"
        assert outcome.result.exit_code == 0

        # The flight record verifies: every link, finalised.
        verdict = verify_record(outcome.run_dir)
        assert verdict.ok, verdict.problems
        assert verdict.histogram.get("proxy_started") == 1
        assert verdict.histogram.get("proxy_closed") == 1
        assert verdict.histogram.get("tool_call") == 4
        assert verdict.histogram.get("tool_result") == 3
        assert verdict.histogram.get("policy_denied") == 1

        # The T1 floor: the observe bundle saw everything.
        bundle = json.loads(outcome.observe_bundle_path.read_text())
        assert bundle["phase"] == "final"
        assert bundle["classification"]["state"] == "clean_exit"
        created = {f["path"] for f in bundle["manifest"]["created"]}
        assert "unmediated.txt" in created  # the harness's own hands
        assert "made-by-proxy.txt" in created  # the mediated write

        # The honesty line: the unmediated file is in the manifest diff
        # and in NO tool event — observed, never inferred.
        events = [
            json.loads(line)
            for line in (outcome.run_dir / "record.jsonl").read_text().splitlines()
        ]
        for event in events:
            if event["kind"] in ("tool_result", "tool_call"):
                assert "unmediated.txt" not in json.dumps(event["data"])

        # The reviewer's artifact: the attested bundle verifies offline.
        assert outcome.attest_built
        assert outcome.attest_error is None
        result = verify_bundle(outcome.attest_bundle_dir or Path())
        assert result.ok, [c.reason for c in result.checks if not c.ok]

        # The mediated commands are observed rows backed by the record
        # blob — never asserted.
        manifest = json.loads(
            ((outcome.attest_bundle_dir or Path()) / "manifest.json").read_text()
        )
        sections = {
            s["name"]: s["sha256"] for s in manifest["sections"]
        }
        from paa.attest.verify import read_section

        commands = read_section(outcome.attest_bundle_dir or Path(), sections["commands"])
        rows = commands["entries"]
        observed = [r for r in rows if r["source"] == "observed"]
        # Three observed rows: the harness command line itself (T1,
        # from the embedded observe bundle) plus the two mediated bash
        # calls the flight record witnessed executing.
        assert len(observed) == 3
        assert observed[0]["command"][0] == sys.executable  # the harness line
        bash_rows = [r for r in observed if r["command"] and r["command"][0] == "echo"]
        assert bash_rows[0]["command"] == ["echo", "mediated hello"]
        assert bash_rows[0]["command_green"] is True
        assert any(r["command_green"] is False for r in observed)

    async def test_a_non_cooperating_harness_still_gets_the_t1_floor(
        self, tmp_path: Path, home: Path
    ) -> None:
        """No proxy, no mediation — and the record says so by omission."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            (sys.executable, "-c",
             "import os; open('alone.txt','w').write('x'); print('solo')"),
            workspace=workspace,
            timeout_s=60,
        )
        assert outcome.state == "clean_exit"
        verdict = verify_record(outcome.run_dir)
        assert verdict.ok
        assert "proxy_started" not in verdict.histogram
        assert "tool_result" not in verdict.histogram
        assert outcome.attest_built
        result = verify_bundle(outcome.attest_bundle_dir or Path())
        assert result.ok


class TestTheChaosMatrix:
    async def test_a_self_sigkill_harness_is_classified_and_the_record_survives(
        self, tmp_path: Path, home: Path
    ) -> None:
        """kill -9 the subject: the recorder's whole job is to stay honest."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            _harness_file(tmp_path, KILLED_HARNESS),
            workspace=workspace,
            timeout_s=120,
            policy=PolicySpec(mode="ask", allow=("write_file",)),
            child_env={"PAA_SANDBOX__BACKEND": "subprocess"},
        )
        assert outcome.state == "signalled"
        assert outcome.result.exit_code == -9
        verdict = verify_record(outcome.run_dir)
        assert verdict.ok, verdict.problems
        # The mediated write happened before the kill and is journalled.
        assert verdict.histogram.get("tool_result") == 1
        assert (workspace / "before-the-kill.txt").read_text() == "survives"

    async def test_a_harness_that_overruns_the_clock_is_timed_out(
        self, tmp_path: Path, home: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            workspace=workspace,
            timeout_s=2,
        )
        assert outcome.state == "timed_out"
        verdict = verify_record(outcome.run_dir)
        assert verdict.ok, verdict.problems
        events = [
            json.loads(line)["kind"]
            for line in (outcome.run_dir / "record.jsonl").read_text().splitlines()
        ]
        assert "child_exit" in events
        assert "record_finalised" in events


class TestHonestLabels:
    async def test_the_summary_carries_the_containment_facts(
        self, tmp_path: Path, home: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            (sys.executable, "-c", "print('hi')"),
            workspace=workspace,
            timeout_s=60,
        )
        summary = outcome.record.summary()
        containment = summary["containment"]
        assert containment["backend"] == "subprocess"
        assert containment["isolation_level"] == "PROCESS"
        assert "never inferred" in containment["unmediated"]
        honesty = summary["honesty"]
        assert honesty["resume"].startswith("none")

    async def test_the_harness_env_carries_the_boundary_markers(
        self, tmp_path: Path, home: Path
    ) -> None:
        """A cooperating harness finds the run through the env, nothing else."""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        core = RecorderCore(SubprocessSandbox(), home=home)
        outcome = await core.record(
            (sys.executable, "-c",
             "import os, json; "
             "print(json.dumps({k: v for k, v in os.environ.items() "
             "if k.startswith('PAA_RECORD')}))"),
            workspace=workspace,
            timeout_s=60,
        )
        assert outcome.state == "clean_exit"
        markers = json.loads(outcome.result.stdout.strip().splitlines()[-1])
        assert markers["PAA_RECORD_RUN_ID"] == outcome.run_id
        assert markers["PAA_RECORD_RUN_DIR"] == str(outcome.run_dir)
        assert "record proxy --run-dir" in markers["PAA_RECORD_PROXY_CMD"]
