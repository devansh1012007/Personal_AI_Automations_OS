"""Session transcript: an append-only record of an interactive run, resumable.

Clean-room implementation of the session capability. A session is a sequence of
turns (user, assistant, tool-call, tool-result) persisted as JSON Lines, so it
survives a crash and can be resumed — the same durability principle as the
ledger, applied to the conversation itself.

Persistence is append-only: each turn is one line, flushed as it happens. A
crash loses at most the turn in flight, never the history, and resume is a
straight replay of the file. Compaction replaces the *in-memory* window with a
summary while leaving the on-disk transcript intact, so freeing context never
destroys the record.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

__all__ = ["Session", "Turn", "TurnRole"]

log = structlog.get_logger(__name__)


class TurnRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM = "system"
    SUMMARY = "summary"


@dataclass(slots=True)
class Turn:
    """One entry in the transcript."""

    role: TurnRole
    content: str
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "turn_id": self.turn_id,
                "role": self.role.value,
                "content": self.content,
                "tool_name": self.tool_name,
                "tool_arguments": self.tool_arguments,
                "tokens": self.tokens,
                "created_at": self.created_at,
                "metadata": self.metadata,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, line: str) -> Turn:
        d = json.loads(line)
        return cls(
            role=TurnRole(d["role"]),
            content=d["content"],
            turn_id=d.get("turn_id", uuid.uuid4().hex),
            tool_name=d.get("tool_name"),
            tool_arguments=d.get("tool_arguments", {}),
            tokens=d.get("tokens", 0),
            created_at=d.get("created_at", datetime.now(UTC).isoformat()),
            metadata=d.get("metadata", {}),
        )


class Session:
    """An interactive session with a durable, resumable transcript."""

    def __init__(
        self,
        session_id: str | None = None,
        *,
        transcript_dir: Path | str | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self._turns: list[Turn] = []
        self._dir = Path(transcript_dir) if transcript_dir else None
        self._path: Path | None = None
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._path = self._dir / f"{self.session_id}.jsonl"

    # -- appending ---------------------------------------------------------

    def append(self, turn: Turn) -> Turn:
        """Add a turn and flush it to disk immediately (append-only)."""
        self._turns.append(turn)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(turn.to_json() + "\n")
        return turn

    def user(self, content: str, **meta: Any) -> Turn:
        return self.append(Turn(TurnRole.USER, content, metadata=meta))

    def assistant(self, content: str, *, tokens: int = 0, **meta: Any) -> Turn:
        return self.append(Turn(TurnRole.ASSISTANT, content, tokens=tokens, metadata=meta))

    def tool_call(self, tool_name: str, arguments: dict[str, Any]) -> Turn:
        return self.append(
            Turn(TurnRole.TOOL_CALL, "", tool_name=tool_name, tool_arguments=arguments)
        )

    def tool_result(self, tool_name: str, content: str, **meta: Any) -> Turn:
        return self.append(
            Turn(TurnRole.TOOL_RESULT, content, tool_name=tool_name, metadata=meta)
        )

    # -- reading -----------------------------------------------------------

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    def total_tokens(self) -> int:
        return sum(t.tokens for t in self._turns)

    def window(self) -> list[Turn]:
        """The turns to send to the model: everything after the last summary.

        Compaction inserts a SUMMARY turn; the window is that summary plus
        everything since, so the model gets a compact context without the raw
        history the summary replaced.
        """
        last_summary = max(
            (i for i, t in enumerate(self._turns) if t.role is TurnRole.SUMMARY),
            default=-1,
        )
        return self._turns[last_summary:] if last_summary >= 0 else list(self._turns)

    # -- compaction --------------------------------------------------------

    def compact(self, summary: str, *, keep_last: int = 2) -> Turn:
        """Insert a summary that collapses the window, keeping the last few turns.

        Only the in-memory window is affected; the on-disk transcript keeps
        every original turn, so compaction frees context without losing history.
        """
        tail = self._turns[-keep_last:] if keep_last else []
        summary_turn = Turn(TurnRole.SUMMARY, summary)
        # Rebuild the in-memory list: summary then the preserved tail. The file
        # is untouched except for appending the summary as a new line.
        self._turns = [summary_turn, *tail]
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(summary_turn.to_json() + "\n")
        return summary_turn

    def clear(self) -> None:
        """Drop the in-memory window. The on-disk transcript is preserved."""
        self._turns = []

    # -- persistence -------------------------------------------------------

    @classmethod
    def resume(cls, session_id: str, transcript_dir: Path | str) -> Session:
        """Rebuild a session by replaying its transcript file."""
        session = cls(session_id, transcript_dir=transcript_dir)
        if session._path and session._path.exists():
            with session._path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        session._turns.append(Turn.from_json(line))
            log.info("session.resumed", session_id=session_id, turns=len(session._turns))
        return session

    @staticmethod
    def list_sessions(transcript_dir: Path | str) -> list[str]:
        d = Path(transcript_dir)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))
