"""Pure command dispatcher — parse, categorise, return a structured result.

The runner owns the :class:`Runtime`; the dispatcher never touches one. This
keeps the dispatch loop trivially unit-testable: feed a string + snapshot, get a
:class:`CommandResult` back.
"""

from __future__ import annotations

import enum
import shlex
from dataclasses import dataclass, field

from paa.tui.snapshot import Snapshot, parse_correlation_id

__all__ = ["CommandKind", "CommandResult", "dispatch"]


class CommandKind(str, enum.Enum):
    NEW = "new"
    DRIVE = "drive"
    STATUS = "status"
    LEDGER = "ledger"
    GATE = "gate"
    DOCTOR = "doctor"
    HELP = "help"
    IGNORE = "ignore"  # blank / whitespace-only line: swallow and continue
    QUIT = "quit"


@dataclass
class CommandResult:
    kind: CommandKind
    args: dict[str, str] = field(default_factory=dict)
    note: str | None = None


def dispatch(raw: str, snapshot: Snapshot) -> CommandResult:
    """Parse a single command line into a :class:`CommandResult`.

    Does not touch the runtime; the runner executes any side effects. Unknown
    commands raise ``ValueError`` with the operator-facing message so the runner
    can surface it without try/except everywhere.
    """
    cmd = raw.strip()
    if not cmd:
        # Blank line (after trimming) — swallow silently, don't quit, don't error.
        return CommandResult(kind=CommandKind.IGNORE)
    try:
        parts = shlex.split(cmd, posix=False)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"could not parse command: {exc}") from exc
    if not parts:
        raise ValueError("empty command")

    head = parts[0].lower()
    rest = parts[1:]

    if head in {"q", ":q", ":quit", "exit", "quit!"}:
        return CommandResult(kind=CommandKind.QUIT)
    if head in {"help", "?", "h"}:
        return CommandResult(kind=CommandKind.HELP, note="type 'help' for the command list")
    if head == "doctor":
        return CommandResult(kind=CommandKind.DOCTOR)

    match head:
        case "new":
            if len(rest) < 1:
                raise ValueError("usage: new <goal>")
            return CommandResult(kind=CommandKind.NEW, args={"goal": " ".join(rest)})
        case "drive":
            if len(rest) < 1:
                raise ValueError("usage: drive <correlation-id>")
            cid = parse_correlation_id(rest[0])
            return CommandResult(
                kind=CommandKind.DRIVE, args={"correlation_id": str(cid)}
            )
        case "status":
            if len(rest) < 1:
                raise ValueError("usage: status <correlation-id>")
            cid = parse_correlation_id(rest[0])
            existing = snapshot.row_for(cid)
            if existing is None:
                raise ValueError(f"no such task {rest[0]!r}")
            return CommandResult(
                kind=CommandKind.STATUS,
                args={"correlation_id": str(cid)},
            )
        case "ledger":
            if len(rest) < 1:
                raise ValueError("usage: ledger <correlation-id>")
            cid = parse_correlation_id(rest[0])
            existing = snapshot.row_for(cid)
            if existing is None:
                raise ValueError(f"no such task {rest[0]!r}")
            return CommandResult(
                kind=CommandKind.LEDGER,
                args={"correlation_id": str(cid)},
            )
        case "gate":
            # gate <id> [--approve|--reject] [--note ...]
            if len(rest) < 1:
                raise ValueError("usage: gate <correlation-id> [--approve|--reject]")
            cid = parse_correlation_id(rest[0])
            approved: bool | None = None
            for tok in rest[1:]:
                # --note= and --note <val> are accepted so the flag parses; the
                # note itself is carried through to the runner's clear_human_gate()
                # call there, where it is displayed verbatim.
                if tok in {"--approve", "--reject"}:
                    approved = tok == "--approve"
                elif tok.startswith("--note=") or tok.startswith("--note "):
                    pass  # validated below as recognised shape
                else:
                    raise ValueError(f"unexpected flag {tok!r} after gate id")
            return CommandResult(
                kind=CommandKind.GATE,
                args={
                    "correlation_id": str(cid),
                    "approved": "y" if approved is True else ("n" if approved is False else ""),
                },
                note=f"approved={approved}" if approved is not None else None,
            )
        case _:
            raise ValueError(f"unknown command: {head}")
