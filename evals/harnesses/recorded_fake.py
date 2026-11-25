"""A scripted cooperating harness — the mediated fake (C7.4's first subject).

This is not a model and not an agent: it is a deterministic script that
plays back what a *cooperating* harness does under ``paa record``, so
the eval harness has a real subject that needs no model key and never
touches the network. What it does, stated exactly:

1. it spawns the run's MCP policy proxy exactly the way the recorder's
   own stanza says a real harness's MCP config would (the run's env
   carries the run dir in ``PAA_RECORD_RUN_DIR``);
2. it completes the MCP initialize handshake;
3. it reads the task's ask (``TASK.md``) through the proxy's
   ``read_file`` tool — a mediated read, journalled and policy-checked;
4. it applies the task's reference solution file-by-file through the
   proxy's ``write_file`` tool — every edit mediated, journalled and
   policy-checked, never a direct write;
5. it closes the proxy and exits 0.

It solves every task in the set this way, because the point of this
harness is to be the *fully cooperative* end of the comparison: the row
that shows what "everything mediated, everything verified" scores. A
real harness (claude-code, codex, ...) plugs into the same adapter
registry and does the same dance with its own intelligence behind it.

Run under the eval runner, never by hand: the runner's adapter names
this script, and the recorder's env (``PAA_RECORD_RUN_DIR`` and
friends) is what makes the proxy findable. Exit codes: 0 for a clean
cooperative session, 1 if the run's record markers are missing (the
harness was started outside a recorded run) or the proxy session
failed (the failure lands in the flight record either way, which is
the recorder's whole point).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

#: The MCP protocol revision this harness speaks (the framing kit's own).
PROTOCOL_VERSION = "2024-11-05"


async def _send(proxy: asyncio.subprocess.Process, message: dict[str, Any]) -> None:
    assert proxy.stdin is not None
    proxy.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    await proxy.stdin.drain()


async def _recv(proxy: asyncio.subprocess.Process) -> dict[str, Any]:
    assert proxy.stdout is not None
    line = await proxy.stdout.readline()
    if not line:
        raise RuntimeError("the record proxy died before answering")
    reply: dict[str, Any] = json.loads(line)
    if "error" in reply:
        raise RuntimeError(
            f"the record proxy refused the call: {reply['error'].get('message')}"
        )
    return reply


async def _call(
    proxy: asyncio.subprocess.Process, request_id: int, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    await _send(
        proxy,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    )
    reply = await _recv(proxy)
    result = reply.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"the {tool} call failed: {result.get('content')}")
    return result


async def _run() -> int:
    task_dir = Path(os.environ.get("PAA_EVAL_TASK_DIR", ""))
    run_dir = os.environ.get("PAA_RECORD_RUN_DIR", "")
    if not task_dir.is_dir() or not run_dir:
        print(
            "recorded-fake: needs PAA_EVAL_TASK_DIR and a recorded run's "
            "PAA_RECORD_RUN_DIR (the eval runner and `paa record` provide both)",
            file=sys.stderr,
        )
        return 1

    solution = task_dir / "solution"
    files = sorted(path for path in solution.rglob("*") if path.is_file())
    if not files:
        print(f"recorded-fake: {solution} holds no reference solution", file=sys.stderr)
        return 1

    # The proxy is spawned exactly the way the recorder's stanza spells
    # it: a child of the harness, speaking MCP over stdio. A real
    # harness's MCP config would name the same command.
    proxy = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from paa.cli.main import app; app()",
        "record",
        "proxy",
        "--run-dir",
        run_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    try:
        await _send(
            proxy,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            },
        )
        reply = await _recv(proxy)
        agreed = (reply.get("result") or {}).get("protocolVersion")
        if agreed != PROTOCOL_VERSION:
            raise RuntimeError(f"proxy pinned protocol {agreed!r}, not {PROTOCOL_VERSION!r}")

        # A mediated read of the ask, the way a real harness reads the
        # task before acting on it.
        await _call(proxy, 2, "read_file", {"path": "TASK.md"})

        # Every edit rides the proxy: journalled, policy-checked, jailed.
        for index, path in enumerate(files, start=3):
            await _call(
                proxy,
                index,
                "write_file",
                {
                    "path": path.relative_to(solution).as_posix(),
                    "content": path.read_text(encoding="utf-8"),
                },
            )
    finally:
        if proxy.stdin is not None:
            proxy.stdin.close()
        code = await proxy.wait()

    if code != 0:
        print(f"recorded-fake: the record proxy exited {code}", file=sys.stderr)
        return 1
    print(f"recorded-fake: applied {len(files)} file(s) through the record proxy")
    return 0


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
