"""The PAA MCP server: PAA's memory and chat as six tools over stdio.

C4.5 of SPEC 2 §3.1 — ``paa mcp serve``. This is the control-plane wedge:
Claude Code, Codex, Cursor, OpenCode gain PAA memory as a tool through the
same boundary PAA demands of everything else — every tool call is
policy-checked and journaled before it executes, and refusals are ledger
events too.

The six tools (SPEC 2 §3.3; policy class and journaling are part of the
contract):

=================  ============================  =========  =====================
Tool               Input → output                Class      Journaled as
=================  ============================  =========  =====================
memory_query       ``{fact_id | alias}`` →       read       ``MEMORY_RECALL_CONSULTED``
                   fact + supersession lineage              (capability ``recall``)
memory_search      ``{query, limit}`` → ranked   read       same
                   facts
memory_remember    ``{statement, provenance}``   write      ``MEMORY_SIGNAL_EXTRACTED``
                   → signal id                   (session-  via the MemoryCreator
                                                 scoped)
context_bundle     ``{session_id?, query}`` →    read       ``MEMORY_RECALL_CONSULTED``
                   packet + provenance
chat_send          ``{session_id?, message}``    write      full chat-turn event set
                   → reply (non-streaming        + model    (through ChatService;
                   aggregate — streaming over    (chat      C4.1/C4.2's loop)
                   MCP is awkward and deferred   pipeline)
                   to when the recorder needs
                   it)
sessions_list      — → session summaries         read       — (the tool-call audit
                                                            rows still apply)
=================  ============================  =========  =====================

**On the journaled-as column, honestly.** ``PIPELINE_CALL_COMPLETED`` does not
exist in :mod:`paa.core.types` — no generic pipeline-call event family has
shipped yet. Per the plan's fallback rule, each tool journals the memory/chat
events it *naturally* produces (the table above), exactly the rows the chat
surface writes for the same semantics (consults via
:class:`~paa.chat.session.ChatSessionJournal.recall_consulted`, signals via
``signal_extracted``, full turns through :class:`~paa.chat.service.ChatService`).
Those rows land on the **chat session lineage** — never on the tool-call
lineage below, because a lineage that mixes chat vocabulary with tool-call
vocabulary would defeat both recovery exemptions (``ledger/recovery.py``
skips coder-only and chat-only lineages; a mixed one would be swept as task
work and re-queued).

**Every tool call is also journaled as a tool call** — the coder's ADR-0046
tool-call family (``TOOL_CALL_PROPOSED`` → ``TOOL_CALL_DENIED`` /
``TOOL_CALL_COMPLETED`` / ``TOOL_CALL_FAILED``), written through
:class:`~paa.coder.journal.LedgerToolJournal` onto a **per-call lineage** with
``isolation_level=NONE`` (nothing here spawns a process). This is the
ledger-before-effect row: for the write tools, the PROPOSED row precedes the
signal write / turn drive, and a crash mid-call leaves a PROPOSED the boot
sweep closes as ``FAILED(reason=interrupted)``. The spec column's
``PIPELINE_CALL_COMPLETED (capability recall)`` is served by these rows plus
the consult row: between them, the ledger records that the call happened,
under which capability, what memory served, and what it returned.

The policy gate
---------------
``manifest ∩ mandate ∩ policy`` (the house pattern,
:func:`paa.pipelines.base.compute_grants`). The **manifest** is
:data:`TOOL_MANIFEST` — each tool declares the facade capabilities it
exercises. The **mandate** is a constructor seam
(``mandate_capabilities``): ``None`` today, meaning no constraint, exactly
the P1 semantics; when the mandate machinery lands (P2/P4) the seam already
exists. The **policy** ceiling is derived from the active
:class:`~paa.core.types.PermissionMode`: ``LOCKDOWN`` is the air-gap promise,
so anything that writes memory or calls a model is refused. A tool whose
capability ask does not survive the intersection is refused before
execution — with the refusal journaled (``TOOL_CALL_DENIED``, following the
coder hook-block path) and returned as JSON-RPC ``-32002`` /
``-32006`` per :mod:`paa.mcp.protocol`'s table. ``POLICY_BLOCKED`` is
deliberately not used here: it is a terminal *task* verdict that would close
the session lineage (the same reason the chat journal journals its fence
refusal on the consult row instead).

Auth
----
stdio is the transport: the server is spawned by its client and the pipe has
exactly one writer, so there is no socket and no listener (loopback-only by
construction — there is nothing to bind; a TCP mode must mirror
``config.Settings._enforce_loopback`` and is deliberately absent in this
MVP). A token hardens the pipe when one is configured: the server reads the
variable named by ``PAA_MCP__TOKEN_ENV`` (default ``PAA_MCP_TOKEN``) at
startup, and while it holds a non-empty value every client must present that
token in its ``initialize`` params (``params.token``) before any other
method is served. Config stores the *name* of the env var, never the value;
the value never enters the ledger — refusals are journaled with the method
and a bounded reason only, and no payload ever carries a ``token`` key.

Secret hygiene
--------------
Tool arguments are attested by :func:`~paa.coder.journal.args_sha256_of` and
:func:`~paa.coder.journal.args_preview_redacted` (shape-only previews, the
N2.5 canary discipline) — a harness that says "my api key is ..." into
``chat_send`` leaks nothing into the ledger; the verbatim words live in the
cold lake, the substrate designed to hold raw input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

from paa.chat.context_bundle import build_context_bundle
from paa.chat.loop import (
    CHAT_CAPABILITIES,
    ChatModel,
    ChatTurnFacade,
    build_chat_model,
)
from paa.chat.service import ChatService
from paa.chat.session import (
    CHAT_CHANNEL,
    ChatSessionJournal,
    SessionNotFoundError,
)
from paa.coder.journal import (
    LedgerToolJournal,
    args_preview_redacted,
    args_sha256_of,
)
from paa.coder.loop import ToolCtx
from paa.config import ChatSettings, ContextSettings, MCPSettings
from paa.core.errors import PaaError, PermissionDeniedError
from paa.core.types import EventType, PermissionMode
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.store import LedgerStore
from paa.mcp.protocol import (
    AUTH_REQUIRED,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    FrameSink,
    McpError,
    encode_frame,
    error_response,
    read_frame,
    result_response,
    wire_code_for,
)
from paa.memory.creator import MemoryCreator
from paa.memory.facts import Entity, Fact, FactRepository
from paa.models.base import redact
from paa.pipelines.base import (
    Capability,
    CapabilityNotGrantedError,
    compute_grants,
)
from paa.sandbox.base import IsolationLevel
from paa.storage.coldlake.signals import SignalRepository
from paa.storage.relational.base import RelationalBackend

if TYPE_CHECKING:
    from asyncio import StreamReader

    from paa.runtime import Runtime

__all__ = ["TOOL_MANIFEST", "MCPServer", "ToolSpec"]

log = structlog.get_logger(__name__)

#: Session scope stamped on chat sessions the server auto-creates for a
#: session-less ``chat_send``. Free-form in the journal payload; distinct from
#: ``"personal"`` so the ledger shows the session came from the MCP surface.
_MCP_SCOPE: Final = "mcp"

#: Capabilities a memory read consults (the spec column's "capability recall").
_READ_CAPS: Final[frozenset[Capability]] = frozenset({Capability.RECALL})
_WRITE_CAPS: Final[frozenset[Capability]] = frozenset({Capability.REMEMBER})

#: The search/list budget ceiling. ``memory_search``'s limit is capped here
#: rather than trusting the harness: one tool call must not drain the hot
#: tables into a reply, and the cap keeps the payload inside the ledger's
#: id-list bounds should the result ever be journaled in bulk.
_MAX_SEARCH_LIMIT: Final = 100
_DEFAULT_SEARCH_LIMIT: Final = 10

_MAX_REASON_CHARS: Final = 200


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool's manifest row: the wire shape and the capability ask."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    output_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    capabilities: frozenset[Capability] = frozenset()
    policy_class: str = "read"
    """The spec §3.3 policy class label, carried for the manifest check's
    audit story (the enforcement itself is the capability intersection)."""

    def describe(self) -> dict[str, Any]:
        """The ``tools/list`` wire form for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
        }


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    schema["additionalProperties"] = False
    return schema


_FACT_PROPERTIES: Final[dict[str, Any]] = {
    "id": {"type": "string"},
    "statement": {"type": "string"},
    "confidence": {"type": "number"},
    "importance": {"type": "number"},
    "memory_domain": {"type": "string"},
    "entity_name": {"type": ["string", "null"]},
    "predicate": {"type": "string"},
    "object_value": {"type": "string"},
    "source_signal_id": {"type": ["string", "null"]},
}


def _fact_output_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "fact": {"type": ["object", "null"], "properties": _FACT_PROPERTIES},
            "lineage": {"type": "array", "items": {"type": "string"}},
            "entity": {"type": ["object", "null"]},
        },
        required=["fact", "lineage"],
    )


#: The server manifest — the first leg of ``manifest ∩ mandate ∩ policy``.
#: Declared in spec §3.3 order; the description strings double as the
#: ``tools/list`` documentation a harness renders for the model.
TOOL_MANIFEST: Final[dict[str, ToolSpec]] = {
    spec.name: spec
    for spec in (
        ToolSpec(
            name="memory_query",
            description=(
                "Fetch one PAA memory fact by id, or resolve an entity alias to "
                "its top live fact. Returns the fact with its supersession "
                "lineage."
            ),
            input_schema={
                **_object_schema(
                    {
                        "fact_id": {"type": "string", "minLength": 1},
                        "alias": {"type": "string", "minLength": 1},
                        # Optional: binds the consult row to a session lineage.
                        "session_id": {"type": "string"},
                    },
                    required=[],
                ),
                # Exactly one lookup key, declared on the wire the same way
                # _validate_arguments enforces it.
                "anyOf": [
                    {"required": ["fact_id"]},
                    {"required": ["alias"]},
                ],
            },
            output_schema=_fact_output_schema(),
            capabilities=_READ_CAPS,
            policy_class="read",
        ),
        ToolSpec(
            name="memory_search",
            description=(
                "Search PAA's live memory facts by keyword against predicates and "
                "object values, ranked by effective confidence."
            ),
            input_schema=_object_schema(
                {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_SEARCH_LIMIT},
                    # Optional: binds the consult row to a session lineage.
                    "session_id": {"type": "string"},
                },
                required=["query"],
            ),
            output_schema=_object_schema(
                {
                    "query": {"type": "string"},
                    "facts": {
                        "type": "array",
                        "items": {"type": "object", "properties": _FACT_PROPERTIES},
                    },
                },
                required=["query", "facts"],
            ),
            capabilities=_READ_CAPS,
            policy_class="read",
        ),
        ToolSpec(
            name="memory_remember",
            description=(
                "Write one statement into PAA's memory as a session-scoped signal; "
                "the memory creator extracts it into facts. Requires a chat session "
                "id (or the server's configured default session)."
            ),
            input_schema=_object_schema(
                {
                    "statement": {"type": "string", "minLength": 1},
                    "session_id": {"type": "string"},
                    "provenance": {"type": "object"},
                },
                required=["statement"],
            ),
            output_schema=_object_schema(
                {
                    "signal_id": {"type": ["string", "null"]},
                    "outcome": {"type": "string"},
                    "facts_written": {"type": "array", "items": {"type": "string"}},
                    "session_id": {"type": "string"},
                },
                required=["outcome", "facts_written", "session_id"],
            ),
            capabilities=_WRITE_CAPS,
            policy_class="write (session-scoped)",
        ),
        ToolSpec(
            name="context_bundle",
            description=(
                "Assemble the bounded context PAA's chat loop would pack for a "
                "query: recalled facts with provenance, the session's recent turns, "
                "under the context engine's token ceiling."
            ),
            input_schema=_object_schema(
                {
                    "session_id": {"type": "string"},
                    "query": {"type": "string", "minLength": 1},
                },
                required=["query"],
            ),
            output_schema=_object_schema(
                {
                    "session_id": {"type": ["string", "null"]},
                    "query": {"type": "string"},
                    "packet": {"type": "object"},
                    "provenance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "fact_id": {"type": "string"},
                                "statement": {"type": "string"},
                                "confidence": {"type": "number"},
                                "importance": {"type": "number"},
                                "origin_session": {"type": ["string", "null"]},
                            },
                        },
                    },
                    "turns": {"type": "array", "items": {"type": "string"}},
                    "token_ceiling": {"type": "integer"},
                },
                required=["packet", "provenance", "token_ceiling"],
            ),
            capabilities=_READ_CAPS,
            policy_class="read",
        ),
        ToolSpec(
            name="chat_send",
            description=(
                "Send one message into a PAA chat session and get the completed "
                "reply. Recalls memory, calls the chat model, extracts signals — "
                "the full journaled turn. Without a session id (and no configured "
                "default) a new session is created and its id is returned."
            ),
            input_schema=_object_schema(
                {
                    "session_id": {"type": "string"},
                    "message": {"type": "string", "minLength": 1},
                },
                required=["message"],
            ),
            output_schema=_object_schema(
                {
                    "session_id": {"type": "string"},
                    "turn_id": {"type": "string"},
                    "reply": {"type": "string"},
                    "status": {"type": "string"},
                    "recalled_fact_ids": {"type": "array", "items": {"type": "string"}},
                    "signal_ids": {"type": "array", "items": {"type": "string"}},
                    "tokens_consumed": {"type": "integer"},
                    "bundle_tokens": {"type": "integer"},
                    "token_ceiling": {"type": "integer"},
                },
                required=["session_id", "turn_id", "status"],
            ),
            capabilities=CHAT_CAPABILITIES,
            policy_class="write + model",
        ),
        ToolSpec(
            name="sessions_list",
            description=(
                "List PAA chat sessions, newest first, with their folded counters "
                "(turns, tokens, status) — the same fold the ledger replays."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=_object_schema(
                {
                    "sessions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "session_id": {"type": "string"},
                                "created_at": {"type": ["string", "null"]},
                                "status": {"type": "string"},
                                "scope": {"type": "string"},
                                "model": {"type": ["string", "null"]},
                                "turns_proposed": {"type": "integer"},
                                "turns_completed": {"type": "integer"},
                                "turns_failed": {"type": "integer"},
                                "turns_cancelled": {"type": "integer"},
                                "tokens_consumed": {"type": "integer"},
                            },
                        },
                    }
                },
                required=["sessions"],
            ),
            capabilities=frozenset(),
            policy_class="read",
        ),
    )
}


class MCPServer:
    """The stdio MCP server over one runtime's chat + memory surface.

    Constructed with the wiring pieces (:meth:`from_runtime` for production;
    explicit pieces for tests). One instance serves one client connection:
    :meth:`serve` frames NDJSON over the streams, :meth:`handle_message` is
    the pure request handler the framing loop calls.
    """

    def __init__(
        self,
        *,
        chat: ChatService,
        ledger: LedgerStore,
        db: RelationalBackend,
        creator: MemoryCreator,
        signals: SignalRepository | None,
        chat_settings: ChatSettings,
        context_settings: ContextSettings | None = None,
        permission_mode: PermissionMode = PermissionMode.ASK,
        mcp_settings: MCPSettings | None = None,
        expected_token: str | None = None,
        mandate_capabilities: frozenset[Capability] | None = None,
    ) -> None:
        self._chat = chat
        self._ledger = ledger
        self._db = db
        self._creator = creator
        self._signals = signals
        self._facts = FactRepository(db)
        self._chat_settings = chat_settings
        self._context_settings = context_settings
        self._permission_mode = permission_mode
        self._mcp_settings = mcp_settings if mcp_settings is not None else MCPSettings()
        self._expected_token = expected_token
        # The mandate seam (P2/P4): ``None`` imposes no constraint — the same
        # "tighten-only, absent means unconstrained" semantics
        # :func:`compute_grants` states.
        self._mandate_capabilities = mandate_capabilities
        # The bundle facade needs a chat model only to satisfy its constructor
        # (a bundle consult never thinks); the deterministic seam keeps the
        # offline demo offline.
        self._bundle_model: ChatModel = build_chat_model(chat_settings)
        self._authenticated = False
        self._initialized = False

    # -- construction ------------------------------------------------------

    @classmethod
    async def from_runtime(
        cls,
        runtime: Runtime,
        *,
        mcp_settings: MCPSettings | None = None,
        expected_token: str | None = None,
    ) -> MCPServer:
        """Wire the server off a built runtime, the way the CLI builds it.

        The cold-lake signal repository is built through the blob-store
        factory exactly as :meth:`ChatService.from_runtime` builds its own —
        a degrade to ``None`` loses episodic recall, never the turn.
        """
        signals: SignalRepository | None = None
        try:
            from paa.storage.coldlake import get_blob_store

            signals = SignalRepository(runtime.db, get_blob_store(runtime.settings.storage))
        except Exception as exc:
            log.warning("mcp_server.signal_repo_unavailable", error=str(exc))
        creator = runtime.memory_creator
        if creator is None:
            raise PaaError("the MCP server requires a MemoryCreator; build the runtime with one")
        return cls(
            chat=ChatService.from_runtime(runtime),
            ledger=runtime.ledger,
            db=runtime.db,
            creator=creator,
            signals=signals,
            chat_settings=runtime.settings.chat,
            context_settings=runtime.settings.context,
            permission_mode=runtime.settings.policy.mode,
            mcp_settings=mcp_settings or runtime.settings.mcp,
            expected_token=expected_token,
        )

    # -- transport ---------------------------------------------------------

    async def serve(self, reader: StreamReader, sink: FrameSink) -> None:
        """Serve NDJSON requests from ``reader`` until end of stream.

        One frame in, one reply out, in order — the JSON-RPC 2.0 stdio shape.
        Notifications produce no frame; a malformed frame produces its error
        reply and the loop continues, exactly the resilience contract the
        client adapter states for its own read side.
        """
        while True:
            payload = await read_frame(reader)
            if payload is None:
                return  # EOF: the client closed the pipe; shut down cleanly
            response = await self.handle_payload(payload)
            if response is None:
                continue  # a notification — acknowledged by silence
            sink.write(encode_frame(response))
            await sink.drain()

    async def handle_payload(self, payload: str) -> dict[str, Any] | None:
        """Parse one raw frame and dispatch it. Parse failures reply ``-32700``."""
        try:
            message: Any = json.loads(payload)
        except json.JSONDecodeError:
            return error_response(None, PARSE_ERROR, "frame is not valid JSON")
        return await self.handle_message(message)

    # -- the request handler -------------------------------------------------

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """Route one decoded JSON-RPC message. ``None`` for notifications.

        Total over any input: a non-object, a response-shaped object, or a
        malformed notification each get the honest protocol answer rather
        than an exception — one bad frame must not end the connection, the
        same resilience the client adapter applies to its read loop.
        """
        if not isinstance(message, dict):
            return error_response(None, INVALID_REQUEST, "frame is not a JSON object")

        method = message.get("method")
        request_id = message.get("id")
        has_id = "id" in message

        if method is None:
            # A response object (or garbage) arriving at a server: refuse.
            detail = (
                "object is a response, not a request — the server does not accept replies"
                if has_id or "result" in message or "error" in message
                else "request object carries no method"
            )
            return error_response(None, INVALID_REQUEST, detail)

        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                return error_response(request_id, INVALID_PARAMS, "params must be an object")
            return None  # a malformed notification is dropped, not answered

        if not has_id:
            return await self._handle_notification(method)

        try:
            result = await self._dispatch_request(method, params)
        except McpError as exc:
            return error_response(request_id, exc.code, exc.message, exc.details or None)
        except PaaError as exc:
            code = wire_code_for(exc)
            return error_response(request_id, code, redact(str(exc)), exc.details or None)
        except Exception as exc:
            # Unexpected: classify internal, keep the message bounded, and let
            # the traceback reach the (stderr) log — never the client frame.
            log.exception("mcp_server.unhandled_error", error=redact(str(exc)))
            return error_response(
                request_id, INTERNAL_ERROR, f"internal error: {type(exc).__name__}"
            )
        return result_response(request_id, result)

    async def _handle_notification(self, method: str) -> dict[str, Any] | None:
        """Notifications are acknowledged by silence; the protocol's one."""
        if method == "notifications/initialized":
            log.debug("mcp_server.client_initialized")
        # notifications/cancelled and anything else: nothing to unwind (this
        # server answers synchronously), nothing to reply (notifications have
        # no id). Deliberately swallowed.
        return None

    async def _dispatch_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Authenticated-method dispatch. ``initialize`` carries its own gate."""
        if method == "initialize":
            return await self._handle_initialize(params)

        if not self._initialized:
            await self._journal_auth_refusal(method, "request before the initialize handshake")
            raise McpError(
                AUTH_REQUIRED,
                "handshake not completed: send initialize (with the configured token) first",
            )

        if method == "tools/list":
            return {"tools": [spec.describe() for spec in TOOL_MANIFEST.values()]}
        if method == "tools/call":
            return await self._handle_call_tool(params)
        if method == "ping":
            return {}
        raise McpError(
            METHOD_NOT_FOUND,
            f"unknown method {method!r}; this server serves "
            "initialize, tools/list, tools/call, ping",
        )

    # -- handshake ---------------------------------------------------------

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """The MCP handshake: token gate first, then the version contract.

        Auth precedes everything — an unauthenticated caller learns the
        server's name and version from the refusal's absence of detail, not
        from a handshake result. A supported ``protocolVersion`` is echoed;
        an unsupported-but-present one gets a successful reply carrying this
        server's pinned version (the MCP spec's contract: the *client*
        decides to disconnect); a missing one is malformed params.
        """
        if self._token_required():
            token = params.get("token")
            if not isinstance(token, str) or not _token_matches(token, self._expected_token):
                await self._journal_auth_refusal("initialize", "token missing or invalid")
                raise McpError(AUTH_REQUIRED, "initialize refused: token missing or invalid")
        self._authenticated = True

        version = params.get("protocolVersion")
        if not isinstance(version, str) or not version:
            raise McpError(INVALID_PARAMS, "initialize requires params.protocolVersion")
        self._initialized = True

        return {
            # Pinned per the module docstring: the same revision the client
            # adapter speaks, so PAA-to-PAA loopback is symmetric.
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "PAA memory and chat. Query and search facts, remember statements "
                "into session-scoped memory, assemble context bundles, and hold "
                "stateful conversations. Every call is policy-checked and journaled."
            ),
        }

    def _token_required(self) -> bool:
        """Auth is on iff a token was configured at startup."""
        return bool(self._expected_token)

    async def _journal_auth_refusal(self, method: str, reason: str) -> None:
        """One refusal, one lineage — the mandate-refusal pattern.

        ``POLICY_BLOCKED`` is a terminal verdict, so a refusal lineage is
        terminal by construction (the sweep marks it ``ALREADY_TERMINAL`` and
        never re-queues it). The payload names the method and a bounded
        reason; it never names a token — there is no token value anywhere in
        this server's payloads, only the env-var *name* in config.
        """
        refusal_id = uuid.uuid4()
        await self._ledger.append(
            LedgerEvent.create(
                refusal_id,
                EventType.POLICY_BLOCKED,
                session_id=None,
                attempt=0,
                discriminator=f"mcp-auth:{refusal_id.hex}",
                payload={
                    "v": 1,
                    "surface": "mcp",
                    "method": method,
                    "reason": reason[:_MAX_REASON_CHARS],
                    "authenticated": False,
                },
            )
        )

    # -- tools/call --------------------------------------------------------

    async def _handle_call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        """One tool call: validate → propose → gate → execute → close.

        The order is the coder loop's ledger-before-effect order:
        ``TOOL_CALL_PROPOSED`` lands before the policy decision and before
        any effect; a gate refusal gets ``TOOL_CALL_DENIED``; a crash after
        the proposal leaves a row the boot sweep closes as interrupted.
        """
        name = params.get("name")
        spec = TOOL_MANIFEST.get(name) if isinstance(name, str) else None
        if spec is None:
            raise McpError(
                INVALID_PARAMS,
                f"unknown tool {name!r}; this server serves {', '.join(TOOL_MANIFEST)}",
            )
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise McpError(INVALID_PARAMS, "tools/call arguments must be an object")

        args = _validate_arguments(spec.name, arguments)
        # Session resolution runs before the journal so a caller error costs
        # no lineage: nothing was proposed for an unformable call.
        session_id = await self._resolve_tool_session(spec.name, args)

        call_id = uuid.uuid4().hex
        lineage = uuid.uuid4()
        ctx = ToolCtx(
            session_id=str(session_id) if session_id is not None else str(lineage),
            call_id=call_id,
            tool=spec.name,
            args_sha256=args_sha256_of(arguments),
            args_preview_redacted=args_preview_redacted(spec.name, arguments),
            isolation_level=IsolationLevel.NONE,
        )
        journal = LedgerToolJournal(self._ledger, lineage)
        await journal.before(ctx)  # TOOL_CALL_PROPOSED — before gate and effect

        try:
            self._require_grants(spec)
        except PaaError as exc:
            await journal.denied(ctx, str(exc)[:_MAX_REASON_CHARS])
            raise

        try:
            payload, ok = await self._execute_tool(spec.name, args, call_id, session_id)
        except Exception:
            await journal.after(ctx, ok=False, result_sha=None)
            raise
        # An ``isError`` result is still a completed protocol operation whose
        # *tool* failed — the FAILED row mirrors the coder journal's contract
        # that a failed call attests no result hash.
        if ok:
            await journal.after(ctx, ok=True, result_sha=_result_sha256(payload))
        else:
            await journal.after(ctx, ok=False, result_sha=None)
        return _tool_result(payload, ok=ok)

    def _require_grants(self, spec: ToolSpec) -> None:
        """The gate: ``manifest ∩ mandate ∩ policy``, tighten-only.

        A policy-caused miss names :class:`PermissionDeniedError` (the
        operator's mode refused it); anything else is a missing grant
        (:class:`CapabilityNotGrantedError`). Both are journaled by the
        caller as ``TOOL_CALL_DENIED`` before this method's exception
        reaches the wire as ``-32002`` / ``-32006``.
        """
        ceiling = self._policy_ceiling()
        granted = compute_grants(spec.capabilities, self._mandate_capabilities, ceiling)
        missing = spec.capabilities - granted
        if not missing:
            return
        if ceiling is not None and missing - ceiling:
            # The ceiling excluded at least one asked capability: the active
            # permission mode refused this call, which is a permission denial
            # (the LOCKDOWN air-gap doing its job), not a wiring fault.
            raise PermissionDeniedError(
                missing=sorted(cap.value for cap in missing),
                mode=self._permission_mode.value,
            )
        raise CapabilityNotGrantedError(
            ", ".join(sorted(cap.value for cap in missing)),
            (cap.value for cap in granted),
        )

    def _policy_ceiling(self) -> frozenset[Capability] | None:
        """The policy leg of the intersection, from the active mode.

        ``LOCKDOWN`` is the air-gap promise: nothing that writes memory or
        calls a model may run, so reads-only is its ceiling. Every other mode
        constrains nothing here yet — the full policy agent arrives with P4,
        and this seam is where its verdict plugs in.
        """
        if self._permission_mode is PermissionMode.LOCKDOWN:
            return _READ_CAPS
        return None

    # -- tool execution ------------------------------------------------------

    async def _execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        call_id: str,
        session_id: uuid.UUID | None,
    ) -> tuple[dict[str, Any], bool]:
        """Dispatch one validated tool. Returns ``(payload, ok)``.

        ``ok=False`` is a *tool* failure (a failed chat turn) — the MCP
        result carries ``isError=true`` and the call lineage gets
        ``TOOL_CALL_FAILED``; it is not a JSON-RPC error, because the
        request itself was valid (see :mod:`paa.mcp.protocol`).
        """
        if name == "memory_query":
            return await self._tool_memory_query(args, call_id, session_id)
        if name == "memory_search":
            return await self._tool_memory_search(args, call_id, session_id)
        if name == "memory_remember":
            return await self._tool_memory_remember(args, call_id, session_id)
        if name == "context_bundle":
            return await self._tool_context_bundle(args, call_id, session_id)
        if name == "chat_send":
            return await self._tool_chat_send(args, session_id)
        if name == "sessions_list":
            return await self._tool_sessions_list()
        # Unreachable: _handle_call_tool validated the name against the
        # manifest, and every manifest row has an arm above.
        raise McpError(INVALID_PARAMS, f"unknown tool {name!r}")  # pragma: no cover

    # -- memory_query --------------------------------------------------------

    async def _tool_memory_query(
        self, args: dict[str, Any], call_id: str, session_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """One fact by id, or an alias resolved to its entity's top fact."""
        fact: Fact | None = None
        entity: Entity | None = None
        if "fact_id" in args:
            fact = await self._facts.get_fact(args["fact_id"])
        else:
            entity = await self._facts.resolve_entity(args["alias"])
            if entity is not None:
                matched = await self._facts.query_facts(entity_id=entity.id, limit=1)
                fact = matched[0] if matched else None

        lineage: list[str] = []
        if fact is not None:
            lineage = await self._facts.supersession_chain(fact.id)

        payload: dict[str, Any] = {
            "fact": _fact_dict(fact) if fact is not None else None,
            "lineage": lineage,
            "entity": _entity_dict(entity) if entity is not None else None,
        }
        await self._journal_consult(
            session_id,
            call_id=call_id,
            query=args.get("fact_id") or args.get("alias") or "",
            k=1,
            fact_ids=[fact.id] if fact is not None else [],
        )
        return payload, True

    # -- memory_search -------------------------------------------------------

    async def _tool_memory_search(
        self, args: dict[str, Any], call_id: str, session_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """Ranked live facts for a keyword query."""
        query = args["query"]
        limit = args.get("limit", _DEFAULT_SEARCH_LIMIT)
        try:
            facts = await self._facts.search_facts_by_text(query, limit=limit)
        except ValueError as exc:
            # A NUL in the query is refused by the repository (a tool-supplied
            # argument, refused loudly rather than silently narrowed).
            raise McpError(INVALID_PARAMS, str(exc)) from None
        payload = {
            "query": query,
            "facts": [_fact_dict(fact) for fact in facts],
        }
        await self._journal_consult(
            session_id,
            call_id=call_id,
            query=query,
            k=limit,
            fact_ids=[fact.id for fact in facts],
        )
        return payload, True

    # -- memory_remember -------------------------------------------------------

    async def _tool_memory_remember(
        self, args: dict[str, Any], call_id: str, session_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """One statement in, one signal filed, extracted by the creator.

        The session lineage receives ``MEMORY_SIGNAL_EXTRACTED`` after the
        creator runs (it is an outcome row, mirroring the chat facade's
        ``remember``); the ledger-before-effect row is this call's
        ``TOOL_CALL_PROPOSED``, which preceded the signal write.
        """
        if session_id is None:  # enforced by _resolve_tool_session; belt and braces
            raise McpError(  # pragma: no cover - the resolver refuses first
                INVALID_PARAMS,
                "memory_remember is session-scoped: pass session_id or set "
                "PAA_MCP__DEFAULT_SESSION",
            )
        statement = args["statement"]
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "turn_id": call_id,
            "role": "user",
            "text": statement,
            "provenance": args.get("provenance") or {},
        }
        if self._signals is None:
            # Degrade-never-raise, the facade's own shape: no cold lake means
            # no persisted signal, but the semantic path still runs on the
            # bare payload envelope.
            result = await self._creator.process_signal(payload)
            signal_id: str | None = None
        else:
            signal = await self._signals.record(
                CHAT_CHANNEL,
                payload,
                external_id=f"{session_id}:{call_id}:mcp",
            )
            signal_id = signal.id
            result = await self._creator.process_signal(signal.id)
        await ChatSessionJournal(self._ledger, session_id).signal_extracted(
            turn_id=call_id,
            signal_id=signal_id or f"ephemeral:{call_id}:mcp",
            outcome=result.outcome.value,
            facts_written=result.facts_written,
        )
        return {
            "signal_id": signal_id,
            "outcome": result.outcome.value,
            "facts_written": list(result.facts_written),
            "session_id": str(session_id),
        }, True

    # -- context_bundle --------------------------------------------------------

    async def _tool_context_bundle(
        self, args: dict[str, Any], call_id: str, session_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """The packet the chat loop would pack, with its provenance list.

        Assembled through a host-minted :class:`~paa.chat.loop.ChatTurnFacade`
        (the MCP server is a chat host; the facade is the grant): recall and
        the episodic read are exactly the loop's calls, so quarantine
        exclusion, the isolation fence and the consult rows behave identically
        to a real turn's — minus think and remember, which a bundle does not
        do.
        """
        query = args["query"]
        bundle_session = session_id if session_id is not None else uuid.uuid4()
        facade = self._bundle_facade(bundle_session, call_id)
        recalled = await facade.recall(query, k=self._chat_settings.recall_k)
        turns = await facade.episodic_turns(bundle_session, k=self._chat_settings.last_k_turns)
        bundle = build_context_bundle(
            persona=self._chat_settings.persona,
            recalled_facts=recalled,
            recent_turns=turns,
            token_ceiling=self._chat_settings.token_budget,
            settings=self._context_settings,
        )
        payload = {
            "session_id": str(session_id) if session_id is not None else None,
            "query": query,
            "packet": bundle.packet.model_dump(mode="json"),
            "provenance": [
                {
                    "fact_id": p.fact_id,
                    "statement": p.statement,
                    "confidence": p.confidence,
                    "importance": p.importance,
                    "origin_session": p.origin_session,
                }
                for p in bundle.provenance
            ],
            "turns": list(bundle.turns),
            "token_ceiling": bundle.token_ceiling,
        }
        return payload, True

    def _bundle_facade(self, session_id: uuid.UUID, call_id: str) -> ChatTurnFacade:
        """The host-minted facade one bundle consult runs on."""
        return ChatTurnFacade(
            session_id=session_id,
            turn_id=call_id,
            journal=ChatSessionJournal(self._ledger, session_id),
            ledger=self._ledger,
            model=self._bundle_model,
            creator=self._creator,
            facts=self._facts,
            db=self._db,
            settings=self._chat_settings,
            permission_mode=self._permission_mode,
            signals=self._signals,
            context_settings=self._context_settings,
        )

    # -- chat_send ---------------------------------------------------------------

    async def _tool_chat_send(
        self, args: dict[str, Any], session_id: uuid.UUID | None
    ) -> tuple[dict[str, Any], bool]:
        """One turn through :class:`~paa.chat.service.ChatService` — the full
        journaled event set (PROPOSED before recall, consults, signals,
        COMPLETED last). Non-streaming by MVP choice: streaming over MCP is
        awkward (the protocol has no server-initiated delta channel in the
        2024-11-05 surface this server pins) and is deferred to the recorder
        wave; the ledger records the final reply either way."""
        message = args["message"]
        if session_id is None:
            # Auto-create, so a harness can hold a conversation without a
            # bootstrap call; the result names the session for the next turn.
            session_id = await self._chat.create_session(scope=_MCP_SCOPE)
        turn = await self._chat.send(session_id, message)
        completed = turn.status == _STATUS_COMPLETED
        payload: dict[str, Any] = {
            "session_id": str(session_id),
            "turn_id": turn.turn_id,
            "status": turn.status,
            "reply": turn.reply if completed else "",
            "recalled_fact_ids": list(turn.recalled_fact_ids),
            "signal_ids": list(turn.signal_ids),
            "tokens_consumed": turn.tokens_consumed,
            "bundle_tokens": turn.bundle_tokens,
            "token_ceiling": turn.token_ceiling,
        }
        if not completed:
            payload["error"] = turn.error or "chat turn failed"
        return payload, completed

    # -- sessions_list -------------------------------------------------------------

    async def _tool_sessions_list(self) -> tuple[dict[str, Any], bool]:
        """Session summaries, newest first, folded from the ledger."""
        records = await self._chat.list_sessions(limit=_SESSIONS_LIST_LIMIT)
        sessions = [
            {
                "session_id": str(record.session_id),
                "created_at": (
                    record.created_at.isoformat() if record.created_at is not None else None
                ),
                "status": record.status,
                "scope": record.scope,
                "model": record.model,
                "turns_proposed": record.turns_proposed,
                "turns_completed": record.turns_completed,
                "turns_failed": record.turns_failed,
                "turns_cancelled": record.turns_cancelled,
                "tokens_consumed": record.tokens_consumed,
            }
            for record in records
        ]
        return {"sessions": sessions}, True

    # -- shared helpers ---------------------------------------------------------

    async def _journal_consult(
        self,
        session_id: uuid.UUID | None,
        *,
        call_id: str,
        query: str,
        k: int,
        fact_ids: list[str],
    ) -> None:
        """The ``MEMORY_RECALL_CONSULTED`` row a memory read naturally produces.

        With a session, the consult lands on that session's lineage (the
        harness consulted memory on the session's behalf). Without one it
        lands on its own consult-only lineage — chat vocabulary only, so the
        boot sweep's chat exemption holds and no TOOL_CALL row ever shares
        the lineage (a mixed lineage would be swept as task work).
        """
        lineage = session_id if session_id is not None else uuid.uuid4()
        await ChatSessionJournal(self._ledger, lineage).recall_consulted(
            turn_id=call_id,
            query=query,
            k=k,
            fact_ids=fact_ids,
        )

    async def _resolve_tool_session(
        self, tool: str, args: dict[str, Any]
    ) -> uuid.UUID | None:
        """The session a tool call binds to, resolved before any journal row.

        The explicit ``session_id`` argument wins; ``PAA_MCP__DEFAULT_SESSION``
        is the fallback. A named session must exist (``resume`` is the fold's
        own check) — a session id that names no lineage is invalid params,
        raised here so no ``TOOL_CALL_PROPOSED`` row is written for a call
        that could never run.
        """
        raw = args.get("session_id")
        if raw is None and tool in _SESSION_FALLBACK_TOOLS:
            raw = self._mcp_settings.default_session
        if raw is None:
            if tool == "memory_remember":
                raise McpError(
                    INVALID_PARAMS,
                    "memory_remember is session-scoped: pass session_id or set "
                    "PAA_MCP__DEFAULT_SESSION",
                )
            return None
        return await self._resume_session(raw)

    async def _resume_session(self, raw: str) -> uuid.UUID:
        """Parse and resume a session id; caller errors are invalid params."""
        if not isinstance(raw, str) or not raw.strip():
            raise McpError(INVALID_PARAMS, "session_id must be a non-empty string")
        try:
            session_id = uuid.UUID(raw.strip())
        except ValueError:
            raise McpError(INVALID_PARAMS, f"session_id {raw!r} is not a uuid") from None
        try:
            await self._chat.resume_session(session_id)
        except SessionNotFoundError:
            raise McpError(
                INVALID_PARAMS, f"no chat session lineage for {session_id}"
            ) from None
        return session_id


# ---------------------------------------------------------------------------
# module-level helpers (stateless; kept outside the class for testability)
# ---------------------------------------------------------------------------

#: PipelineStatus.COMPLETED's wire value, as a module constant so the
#: comparison in ``chat_send`` needs no import of the enum at use time.
_STATUS_COMPLETED: Final = "completed"

#: The tools that fall back to ``PAA_MCP__DEFAULT_SESSION`` when the call
#: carries no explicit session id. ``memory_remember`` is in the set so the
#: default session satisfies its session-scoped contract; the reads use it to
#: bind their consult rows to a stable lineage.
_SESSION_FALLBACK_TOOLS: Final[frozenset[str]] = frozenset(
    {"memory_remember", "memory_query", "memory_search", "context_bundle", "chat_send"}
)

#: The sessions_list budget. A harness that wants more pages through nothing
#: today — the ledger fold is cheap, but an unbounded list in one reply is
#: not a tool result, it is a dump.
_SESSIONS_LIST_LIMIT: Final = 100


def _token_matches(presented: str, expected: str | None) -> bool:
    """Constant-time token comparison over UTF-8 bytes.

    ``compare_digest`` on ``str`` rejects non-ASCII; encoding both sides
    keeps a unicode token comparable without leaking timing.
    """
    if expected is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


#: Tools whose arguments must carry exactly one key from a group (the wire
#: schema declares the same constraint via ``anyOf``).
_ONE_OF_ARGUMENTS: Final[dict[str, tuple[str, ...]]] = {
    "memory_query": ("fact_id", "alias"),
}


def _validate_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Enforce the manifest schema for one call — hand-rolled, no SDK.

    The JSON Schema in :data:`TOOL_MANIFEST` is the wire contract a harness
    reads; this validator is its enforcement twin. Unknown keys are refused
    (``additionalProperties: false``), required keys must be present, and
    each value's type is checked exactly as the schema declares. A schema
    change without a matching validator change fails the conformance tests,
    which drive both surfaces.
    """
    spec = TOOL_MANIFEST[tool]
    properties = spec.input_schema.get("properties", {})
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        raise McpError(
            INVALID_PARAMS,
            f"tool {tool} does not accept argument(s) {unknown}",
        )
    required = spec.input_schema.get("required", [])
    missing = [key for key in required if key not in arguments]
    if missing:
        raise McpError(INVALID_PARAMS, f"tool {tool} requires argument(s) {missing}")
    one_of = _ONE_OF_ARGUMENTS.get(tool)
    if one_of is not None and not any(key in arguments for key in one_of):
        raise McpError(
            INVALID_PARAMS,
            f"tool {tool} requires exactly one of {sorted(one_of)}",
        )

    validated: dict[str, Any] = {}
    for key, value in arguments.items():
        rule = properties[key]
        expected = rule.get("type")
        if expected == "string" and not _is_str(value, min_length=rule.get("minLength")):
            raise McpError(INVALID_PARAMS, f"argument {key!r} must be a non-empty string")
        if expected == "integer" and not _is_int_in_range(value, rule):
            raise McpError(
                INVALID_PARAMS,
                f"argument {key!r} must be an integer in "
                f"[{rule.get('minimum', 'any')}, {rule.get('maximum', 'any')}]",
            )
        if expected == "object" and not isinstance(value, dict):
            raise McpError(INVALID_PARAMS, f"argument {key!r} must be an object")
        if expected == "array" and not isinstance(value, list):
            raise McpError(INVALID_PARAMS, f"argument {key!r} must be an array")
        validated[key] = value
    return validated


def _is_str(value: Any, *, min_length: int | None = None) -> bool:
    """A string meeting the declared minimum, and never blank when bounded.

    A bounded string argument (``minLength`` in the schema — a query, a
    statement, a message) must also be non-blank: the echo chat path refuses
    empty messages downstream, and surfacing that as invalid params here
    keeps the refusal on the params path (no journal row) instead of letting
    it become a proposed-then-failed call.
    """
    if not isinstance(value, str):
        return False
    if min_length is None:
        return True
    return len(value) >= min_length and bool(value.strip())


def _is_int_in_range(value: Any, rule: dict[str, Any]) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    minimum = rule.get("minimum")
    maximum = rule.get("maximum")
    if minimum is not None and value < minimum:
        return False
    return not (maximum is not None and value > maximum)


def _fact_dict(fact: Fact) -> dict[str, Any]:
    """One fact as the tool result mapping (statement included)."""
    return {
        "id": fact.id,
        "statement": fact.statement,
        "entity_id": fact.entity_id,
        "entity_name": fact.entity_name,
        "predicate": fact.predicate,
        "object_value": fact.object_value,
        "memory_domain": fact.domain,
        "scope": fact.scope,
        "confidence": fact.confidence,
        "importance": fact.importance,
        "source_signal_id": fact.source_signal_id,
        "created_at": fact.created_at.isoformat(),
        "superseded_by": fact.superseded_by,
    }


def _entity_dict(entity: Entity) -> dict[str, Any]:
    """One resolved entity, with the match story the ladder produced."""
    return {
        "id": entity.id,
        "canonical_name": entity.canonical_name,
        "aliases": list(entity.aliases),
        "match": entity.match,
        "score": entity.score,
    }


def _result_sha256(payload: dict[str, Any]) -> str:
    """The result hash a COMPLETED row attests (canonical JSON)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _tool_result(payload: dict[str, Any], *, ok: bool) -> dict[str, Any]:
    """The MCP ``tools/call`` result: content blocks plus structured content.

    ``structuredContent`` is what the PAA client adapter prefers and what a
    modern harness parses; the JSON text block is the 2024-11-05 surface and
    the fallback every other client takes. Both carry the same payload, so
    the two views cannot disagree.
    """
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": not ok,
    }
