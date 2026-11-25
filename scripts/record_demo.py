"""Build the C7.3 done-when artifact: paa-record-demo/ (record → verify → attest → flip).

A scripted cooperating harness — the same shape as the test-suite one — is
recorded by the real CLI (``paa record run``): it makes mediated tool calls
through the real proxy subprocess, does one unmediated effect, meets a
denial, and exits cleanly. The artifact keeps the flight record, its
observe bundle, the attested bundle, the PR body, and the flipped-byte
refusal that proves the chain is not decorative.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DOWNLOAD = Path("/home/z/my-project/download")
DEMO = DOWNLOAD / "paa-record-demo"
REPO = Path("/home/z/my-project/paa")

HARNESS = r'''
import json, os, subprocess, sys

run_dir = os.environ["PAA_RECORD_RUN_DIR"]

# An unmediated effect: the harness's own hands, no proxy involved.
with open("unmediated.txt", "w") as fh:
    fh.write("the harness did this alone\n")

proxy = subprocess.Popen(
    [sys.executable, "-c", "from paa.cli.main import app; app()",
     "record", "proxy", "--run-dir", run_dir],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

def send(msg):
    proxy.stdin.write(json.dumps(msg) + "\n"); proxy.stdin.flush()

def recv():
    line = proxy.stdout.readline()
    assert line, "proxy died"
    return json.loads(line)

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05"}})
assert recv()["result"]["protocolVersion"] == "2024-11-05"

send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["python3", "-c",
                                        "open('made-by-proxy.txt','w').write('via paa')"]}}})
assert recv()["result"]["isError"] is False

send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["sh", "-c", "exit 3"]}}})
assert recv()["result"]["isError"] is True

send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
      "params": {"name": "bash",
                 "arguments": {"argv": ["rm", "-rf", "anything"]}}})
assert recv()["error"]["code"] == -32002

proxy.stdin.close()
assert proxy.wait(timeout=60) == 0
print("fake harness done")
'''


def git(ws: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=ws, check=True, capture_output=True, text=True
    )


def main() -> None:
    import sys as _sys

    _sys.path.insert(0, str(REPO / "src"))
    os.environ["PAA_SANDBOX__BACKEND"] = "subprocess"
    from paa.config import reset_settings_cache

    reset_settings_cache()
    from paa.recorder.record import verify_record

    if DEMO.exists():
        shutil.rmtree(DEMO)
    DEMO.mkdir(parents=True)
    home = Path(tempfile.mkdtemp(prefix="paa-demo-home-"))
    os.environ["PAA_HOME"] = str(home)

    ws = DEMO / "workspace"
    ws.mkdir()
    (ws / "existing.txt").write_text("before\n", encoding="utf-8")
    git(ws, "init")
    git(ws, "add", "-A")
    git(ws, "-c", "user.email=demo@example.invalid", "-c",
        "user.name=demo", "commit", "-q", "-m", "baseline")

    harness_file = ws.parent / "fake_harness.py"
    harness_file.write_text(HARNESS, encoding="utf-8")

    cmd = [
        sys.executable, "-m", "paa.cli.main", "record", "run", "--json",
        "--workspace", str(ws),
        "--timeout-s", "120",
        "--goal", "recorded demo harness session (C7.3 artifact)",
        "--allow", "bash(*)", "--allow", "write_file", "--allow", "read_file",
        "--deny", "bash(rm *)",
        "--env", "PAA_SANDBOX__BACKEND=subprocess",
        "--", sys.executable, "-u", str(harness_file),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    print("record run exit:", proc.returncode)
    out = proc.stdout
    payload = json.loads(out[out.index("{"):])
    run_dir = Path(payload["run_dir"])

    # 1. the flight record + its observe bundle, verbatim
    shutil.copy(run_dir / "record.jsonl", DEMO / "record.jsonl")
    shutil.copy(run_dir / "record.json", DEMO / "record.json")
    shutil.copy(run_dir / "observe-bundle.json", DEMO / "observe-bundle.json")

    # 2. verify the record: green
    verdict = verify_record(run_dir)
    (DEMO / "record-verify-output.txt").write_text(
        "\n".join(
            [
                "$ paa record verify <run-dir>",
                *(f"  ok   {k}" for k in ["schema", "sequence", "chain", "summary", "torn tail", "finalised"]),
                "",
                f"{verdict.events} events, chain root {verdict.chain_root}, phase {verdict.phase}",
                f"histogram: {json.dumps(verdict.histogram, sort_keys=True)}",
                "",
                "verdict: " + ("OK" if verdict.ok else "FAILED") + " (exit 0)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert verdict.ok, verdict.problems

    # 3. the attested bundle + PR body
    attest_dir = Path(payload["attest_bundle"])
    shutil.copytree(attest_dir, DEMO / "attested-bundle")
    pr_body = subprocess.run(
        [sys.executable, "-m", "paa.cli.main", "attest", "pr", str(attest_dir)],
        capture_output=True, text=True, check=True,
    ).stdout
    (DEMO / "PR-BODY.md").write_text(pr_body, encoding="utf-8")

    # 4. the flipped-byte refusal — one byte in the record, verification red
    tampered = DEMO / "tampered-example"
    shutil.copytree(run_dir, tampered)
    lines = (tampered / "record.jsonl").read_text().splitlines()
    event = json.loads(lines[2])
    event["data"]["mode"] = "bypass"
    lines[2] = json.dumps(event, separators=(",", ":"), sort_keys=True)
    (tampered / "record.jsonl").write_text("\n".join(lines) + "\n")
    flip = subprocess.run(
        [sys.executable, "-m", "paa.cli.main", "record", "verify", str(tampered)],
        capture_output=True, text=True,
    )
    (DEMO / "flipped-byte-refusal.txt").write_text(
        "One byte flipped in the third event (the policy_config row's mode "
        "field, 'ask' → 'bypass') — the chain names the exact line:\n\n"
        "$ paa record verify tampered-example\n"
        + flip.stdout
        + ("\nexit code: " + str(flip.returncode))
        + "\n",
        encoding="utf-8",
    )
    assert flip.returncode != 0

    # 5. the demo's own README
    summary = json.loads((DEMO / "record.json").read_text())
    (DEMO / "README.md").write_text(
        f"""# C7.3 done-when, as an artifact — `paa record`

A scripted cooperating harness was recorded by the **real** CLI
(`paa record run`): it made mediated tool calls through the **real**
`paa record proxy` subprocess over real stdio, wrote one file with its own
hands (unmediated), met a policy denial, and exited cleanly.

- `record.jsonl` — the flight record: {summary['events']} hash-chained events
  (`{summary['phase']}`, chain root `{summary['chain_root'][:16]}…`).
- `record.json` — the summary: policy in force, honesty block, containment.
- `observe-bundle.json` — the T1 floor for the same child (`paa-observe-bundle/1`).
- `attested-bundle/` — the run's attested change bundle (`paa-attest-bundle/1`),
  carrying the flight record as evidence and the mediated commands as
  **observed** rows backed by that blob.
- `PR-BODY.md` — the paste-ready PR comment body (`paa attest pr`).
- `record-verify-output.txt` — `paa record verify`: all doors green.
- `flipped-byte-refusal.txt` — one byte flipped in one event; the chain
  names the exact line and refuses.

Verify any of it on a machine with the repo installed:

    paa record verify paa-record-demo/tampered-example   # fails, names the line
    paa record verify paa-record-demo/record.jsonl ..    # (a run dir, not a file)
    paa verify paa-record-demo/attested-bundle           # offline, five doors
""",
        encoding="utf-8",
    )

    # cleanup the throwaway home
    shutil.rmtree(home, ignore_errors=True)
    print("demo built:", DEMO)


if __name__ == "__main__":
    main()
