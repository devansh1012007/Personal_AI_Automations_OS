"""Chat: the conversational surface on real memory (SPEC 2 Wave 4, C4.1-C4.3).

Three modules, one flow. :mod:`paa.chat.session` owns the event vocabulary's
producer (the journal) and the fold that rebuilds a session record from its
lineage — the ledger is the truth, so a session is its events. :mod:`
paa.chat.context_bundle` assembles one turn's bounded context (persona +
recalled facts + this session's last-k turns) under the context engine's
ceiling. :mod:`paa.chat.loop` is built-in pipeline #2 (``builtin.chat``) and
its chat-shaped facade; :mod:`paa.chat.service` is the host callers talk to.

The TUI and the CLI are later waves and deliberately absent here — the
service is their whole surface, and it imports nothing optional.
"""

from paa.chat.context_bundle import (
    BundleProvenance,
    ChatContextBundle,
    build_context_bundle,
)
from paa.chat.loop import (
    ChatLoop,
    ChatModel,
    ChatReply,
    ChatTurnFacade,
    DeltaSink,
    EchoChatModel,
    ProviderChatModel,
    build_chat_model,
    render_chat_prompt,
)
from paa.chat.service import ChatService, TurnResult
from paa.chat.session import (
    ChatSessionJournal,
    ChatSessionRecord,
    SessionNotFoundError,
    fold_session,
    list_sessions,
    session_record,
)

__all__ = [
    "BundleProvenance",
    "ChatContextBundle",
    "ChatLoop",
    "ChatModel",
    "ChatReply",
    "ChatService",
    "ChatSessionJournal",
    "ChatSessionRecord",
    "ChatTurnFacade",
    "DeltaSink",
    "EchoChatModel",
    "ProviderChatModel",
    "SessionNotFoundError",
    "TurnResult",
    "build_chat_model",
    "build_context_bundle",
    "fold_session",
    "list_sessions",
    "render_chat_prompt",
    "session_record",
]
