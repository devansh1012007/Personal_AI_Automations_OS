"""The ``paa_pipeline`` child SDK: speak ``paa-pipeline/1`` without importing paa.

The deliberate boundary
-----------------------
This package imports **nothing from ``paa``**. A pipeline child is a
separate process the host launched and does not trust, and coupling its
SDK to the runtime's import graph would make "upgrade the host" and
"upgrade the pipeline" one operation — the exact coupling the wire
protocol exists to remove. The coupling that remains is *protocol-only*:
this package re-states the wire vocabulary (the version string, the
method names, the error codes) that ``paa.pipelines.base`` and the spec
(``docs/plans/2026-11-21-pipeline-platform-spec.md`` §5) define, and a
test in ``tests/pipelines/test_rpc_host.py`` pins the two version
constants equal so the copies cannot drift silently. The tiny
encode/decode helpers are likewise local re-statements of the framing
kit's NDJSON rule (one compact JSON object per line, UTF-8), copied
rather than imported for the same reason.

What it gives a pipeline author: :func:`serve` — an asyncio main loop
that answers ``pipeline.initialize`` / ``run`` / ``cancel`` / ``shutdown``
on stdin/stdout — and :class:`PipelineCtx`, the ``ctx`` object whose
async methods are the facade's capability calls, each carrying its
``seq`` and capability ``token``. Determinism helpers are the two
always-served calls, :meth:`PipelineCtx.now` and
:meth:`PipelineCtx.random`: outside them the pipeline must not read the
clock or entropy, because the call journal (the checkpoint) can only
replay what went through it.
"""

from paa_pipeline.client import (
    PROTOCOL_VERSION,
    TOKENS_ENV,
    PipelineCtx,
    RpcError,
    serve,
)

__all__ = [
    "PROTOCOL_VERSION",
    "TOKENS_ENV",
    "PipelineCtx",
    "RpcError",
    "serve",
]
