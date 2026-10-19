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
        """Every session id under ``transcript_dir``, sorted **by id**.

        The order is lexicographic over the id, and ids are UUID hex — so this
        list is in *no* meaningful chronological order. It answers "which
        sessions exist", not "which session came last"; use
        :meth:`latest_session` for recency.
        """
        d = Path(transcript_dir)
        if not d.exists():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))

    @staticmethod
    def latest_session(transcript_dir: Path | str) -> str | None:
        """The id of the most recently written transcript, or ``None``.

        **Recency is read off the transcript, not off the filesystem.** The
        last turn carries a ``created_at`` stamp with microsecond resolution,
        and it says when the *session* was last active. The file's mtime says
        when the file was last touched, at whatever granularity the
        filesystem offers: one second on ext3, HFS+ and exFAT, and on many
        network mounts. Two ``paa agent`` runs in the same second therefore
        tie — and the tie-break is the session id, a random UUID hex, so the
        tie was broken at random and the *older* transcript could be picked.
        The turn timestamp is already on disk and is strictly finer, so it is
        the primary signal; the mtime is the fallback for a transcript whose
        turns cannot be read.

        The id is a UUID hex, so the lexicographic order :meth:`list_sessions`
        returns is random with respect to time and cannot answer "what did I
        just run"; a timestamp can.

        A tie that survives both signals still breaks on the id, so repeated
        calls agree with each other and the answer does not depend on the
        order the filesystem hands the directory back.

        ``None`` means there is nothing to continue: the directory is missing,
        empty, or holds no ``.jsonl`` transcript. A transcript directory with
        no session in it is a *state*, not an error, and the caller decides
        what to do about it.
        """
        d = Path(transcript_dir)
        if not d.is_dir():
            return None
        newest: tuple[datetime, str] | None = None
        for path in d.glob("*.jsonl"):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            # Ties break on the id so repeated calls agree with each other:
            # two transcripts written in the same clock tick are otherwise
            # ordered by whatever order the filesystem hands them back.
            key = (_recency_moment(path, mtime), path.stem)
            if newest is None or key > newest:
                newest = key
        return newest[1] if newest else None


# ---------------------------------------------------------------------------
# Recency. The filesystem's mtime is too coarse to order two runs that
# happened in the same second, and the session id is a random UUID hex, so
# the tie-break it provides is random too. The transcript's own turn
# timestamps are finer and already on disk, so they are the signal.
# ---------------------------------------------------------------------------


def _last_turn_created_at(path: Path) -> str | None:
    """The ``created_at`` of a transcript's last readable turn, or ``None``.

    Read from the end of the file backwards. A transcript is append-only, so
    the last turn is the last line — but a crash can leave a torn final line
    (the write was interrupted mid-JSON), and a line that does not parse is
    skipped rather than raised: the turn before it is still a true statement
    about when the session was last active. A line that parses but carries no
    ``created_at`` is skipped the same way.

    The raw ISO-8601 stamp is returned as the transcript holds it.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        stamp = payload.get("created_at")
        if isinstance(stamp, str) and stamp.strip():
            return stamp
    return None


def _recency_moment(path: Path, mtime: float) -> datetime:
    """When a transcript was last active, as one comparable UTC moment.

    The last turn's ``created_at`` is the signal. The file's mtime is the
    fallback for a transcript with no readable turn, or one whose stamp
    cannot be parsed. Both are normalised to UTC so the two are comparable:
    a lexical comparison of raw ISO strings would only be chronological
    within a single UTC offset.
    """
    stamp = _last_turn_created_at(path)
    if stamp is not None:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                # A naive stamp in a transcript written in UTC means UTC.
                moment = moment.replace(tzinfo=UTC)
            return moment.astimezone(UTC)
    return datetime.fromtimestamp(mtime, UTC)
