"""P0.4 spike: journal replay of an out-of-process pipeline (throwaway proof).

Proves the two halves of ADR-0050's replay rule that can be shown without
production code: a pipeline child speaks newline-delimited JSON-RPC over
stdio, the host journals every capability call (PROPOSED before effect,
COMPLETED after), a hard kill lands mid-run, and a restarted child
**replays** — the host answers already-completed calls from the journal
without re-executing them — while a deliberately changed call is flagged
``DIVERGED`` and the run stops.

The two semantics this spike does **not** exercise, deliberately: a crash
between PROPOSED and the outcome (the applied / not_applied / unknown
classification and the -32008 never-auto-repeat rule) and FAILED replay —
those are P2's chaos suite over the real ``CallJournal``, whose rows are
``PIPELINE_CALL_*`` ledger events; this file only has to show the mechanism
works over a real child process, which is why it imports nothing from
``paa`` and uses a plain JSONL journal in a temp dir.

Three legs, one invocation:

1. **crash leg** — the child is told (env ``SPIKE_CRASH_AFTER=2``) to
   ``os._exit`` after receiving its second call result; the journal then
   holds two completed calls and no final result.
2. **resume leg** — a fresh child is launched with
   ``resume.recorded_calls=2`` in the ``pipeline.initialize`` handshake; it
   re-issues calls 1–3, the host answers 1–2 from the journal (the effects
   log must not grow) and executes only call 3, and ``pipeline.run``
   completes.
3. **divergence leg** — on a fresh journal, the child is told
   (``SPIKE_MUTATE_SEQ=2``) to alter call 2's parameters; the host's
   fingerprint comparison fails, a ``DIVERGED`` row is appended, the call is
   answered with error ``-32004 Diverged``, and the run stops with call 3
   never issued.

Usage (from the repo root):

    .venv/bin/python scripts/pipeline_journal_spike.py

Exit codes: 0 = every assertion held (replay with zero re-execution, and
divergence flagged); 1 = an assertion failed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROTOCOL = "paa-pipeline/1"
CAPABILITY = "ctx.echo"
DIVERGED_CODE = -32004


def _canonical(params: dict[str, object]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"))


def _fingerprint(method: str, params: dict[str, object]) -> str:
    return hashlib.sha256(_canonical({"method": method, "params": params}).encode()).hexdigest()


# ---------------------------------------------------------------------------
# The journal: a temp JSONL "ledger" with seq + fingerprint + outcome.
# ---------------------------------------------------------------------------


class Journal:
    def __init__(self, path: Path) -> None:
        self._path = path

    def records(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        return [
            json.loads(line) for line in self._path.read_text().splitlines() if line.strip()
        ]

    def append(self, record: dict[str, object]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def completed(self, seq: int) -> dict[str, object] | None:
        for record in self.records():
            if record.get("seq") == seq and record.get("kind") == "COMPLETED":
                return record
        return None


# ---------------------------------------------------------------------------
# Host: launch the child, serve ctx.* calls, replay from the journal.
# ---------------------------------------------------------------------------


async def run_child(
    journal: Journal,
    effects: Path,
    *,
    crash_after: int = 0,
    mutate_seq: int = 0,
    recorded_calls: int = 0,
    received: list[int] | None = None,
) -> dict[str, object]:
    """One pipeline run; returns the child's final ``pipeline.run`` result.

    Every capability call received is appended to ``received`` (when given)
    so the driver can assert how many calls a stopped re-run actually issued —
    a count the journal cannot answer, because a replayed call appends no row.

    Raises ``RuntimeError`` when the child dies mid-run (the crash leg) or
    answers with a JSON-RPC error (the divergence leg).
    """
    env = dict(os.environ)
    env.update(
        SPIKE_CRASH_AFTER=str(crash_after),
        SPIKE_MUTATE_SEQ=str(mutate_seq),
        SPIKE_RECORDED=str(recorded_calls),
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--child",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None
    next_host_id, final_result = 100, None

    async def send(method: str, params: dict[str, object], msg_id: object) -> None:
        proc.stdin.write(
            (json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode()
        )
        await proc.stdin.drain()

    await send("pipeline.initialize",
        {"protocol": PROTOCOL, "run_id": "spike", "resume": {"recorded_calls": recorded_calls}},
        next_host_id,
    )
    next_host_id += 1
    await send("pipeline.run", {}, next_host_id)
    run_id = next_host_id

    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError(f"child died mid-run (exit={proc.returncode})")
            message = json.loads(line)
            if "method" in message:  # a capability call from the pipeline
                params = dict(message["params"])
                seq = int(params["seq"])
                if received is not None:
                    received.append(seq)
                fp = _fingerprint(message["method"], params)
                done = journal.completed(seq)
                if done is not None and done.get("fingerprint") != fp:
                    journal.append({"seq": seq, "kind": "DIVERGED", "fingerprint": fp})
                    reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": DIVERGED_CODE, "message": "Diverged"}}
                elif done is not None:  # replay: answer from the record, no effect
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": done["result"]}
                else:
                    journal.append({"seq": seq, "kind": "PROPOSED", "fingerprint": fp})
                    result = {"effects": effects.read_text().count("\n") + 1}  # the "effect"
                    with effects.open("a", encoding="utf-8") as fh:
                        fh.write(f"effect seq={seq}\n")
                    journal.append({"seq": seq, "kind": "COMPLETED", "fingerprint": fp, "result": result})
                    reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
                proc.stdin.write((json.dumps(reply) + "\n").encode())
                await proc.stdin.drain()
            elif message.get("id") == run_id:
                if "error" in message:
                    raise RuntimeError(f"run failed: {message['error']['message']}")
                final_result = dict(message["result"])
                break
    finally:
        proc.stdin.close()
        await proc.wait()
    return final_result or {}


# ---------------------------------------------------------------------------
# Child: the "mind" — three deterministic calls, then a final result.
# ---------------------------------------------------------------------------


def _child() -> int:
    crash_after = int(os.environ.get("SPIKE_CRASH_AFTER", "0"))
    mutate_seq = int(os.environ.get("SPIKE_MUTATE_SEQ", "0"))
    calls = [{"seq": 1, "text": "alpha"}, {"seq": 2, "text": "beta"}, {"seq": 3, "text": "gamma"}]

    def reply(msg_id: object, payload: dict[str, object]) -> None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, **payload}) + "\n")
        sys.stdout.flush()

    next_id = 1
    for line in sys.stdin:
        request = json.loads(line)
        if request.get("method") == "pipeline.initialize":
            reply(request["id"], {"result": {"ok": True, "name": "spike", "protocol": PROTOCOL}})
            continue
        total = 0
        for call in calls:  # pipeline.run: three deterministic capability calls
            params = dict(call)
            if params["seq"] == mutate_seq:
                params["text"] = params["text"] + "-mutated"
            cid = next_id
            next_id += 1
            sys.stdout.write(
                json.dumps({"jsonrpc": "2.0", "id": cid, "method": CAPABILITY, "params": params}) + "\n"
            )
            sys.stdout.flush()
            response = json.loads(sys.stdin.readline())
            if "error" in response:
                reply(request["id"], {"error": response["error"]})
                return 0
            total += int(response["result"]["effects"])
            if int(response["result"]["effects"]) == crash_after:
                os._exit(9)  # hard kill: the crash leg
        reply(request["id"], {"result": {"status": "completed", "summary": f"3 calls, {total} effects", "metrics": {"calls": 3}}})
        return 0
    return 0


# ---------------------------------------------------------------------------
# Driver: crash leg -> resume leg -> divergence leg, with assertions.
# ---------------------------------------------------------------------------


def main() -> int:
    checks: list[tuple[str, bool]] = []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        journal, effects = Journal(root / "journal.jsonl"), root / "effects.log"
        effects.write_text("")

        try:
            asyncio.run(run_child(journal, effects, crash_after=2))
            checks.append(("crash leg should kill the child", False))
        except RuntimeError as exc:
            checks.append(("crash leg killed the child mid-run", "died mid-run" in str(exc)))
        checks.append(("journal holds 2 completed calls", journal.completed(2) is not None))
        checks.append(("effects log holds exactly 2 effects", effects.read_text().count("\n") == 2))

        final = asyncio.run(run_child(journal, effects, recorded_calls=2))
        checks.append(("resume leg completed the run", final.get("status") == "completed"))
        checks.append(("replayed calls were NOT re-executed", effects.read_text().count("\n") == 3))
        checks.append(("journal holds 3 completed calls", journal.completed(3) is not None))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        journal, effects = Journal(root / "journal.jsonl"), root / "effects.log"
        effects.write_text("")
        asyncio.run(run_child(journal, effects))  # build the record the re-run must match
        received: list[int] = []
        try:
            asyncio.run(run_child(journal, effects, mutate_seq=2, received=received))
            checks.append(("divergence leg should stop the run", False))
        except RuntimeError as exc:
            checks.append(("divergence leg stopped the run", "Diverged" in str(exc)))
        checks.append(("the stopped re-run issued exactly two calls", received == [1, 2]))
        diverged = [r for r in journal.records() if r.get("kind") == "DIVERGED"]
        checks.append(("journal flagged the changed call DIVERGED", bool(diverged)))
        checks.append(("the DIVERGED record names the mutated call", diverged and diverged[0].get("seq") == 2))
        checks.append(("no effect re-executed after the divergence", effects.read_text().count("\n") == 3))

    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    if "--child" in sys.argv:
        sys.exit(_child())
    sys.exit(main())
