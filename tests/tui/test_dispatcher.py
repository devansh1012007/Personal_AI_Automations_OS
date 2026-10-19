"""Pure command dispatcher tests — no runtime involved."""

from __future__ import annotations

import pytest

from paa.core.types import ComplexityModality, CorrelationId
from paa.ledger.replay import TaskPhase
from paa.tui.dispatcher import CommandKind, dispatch
from paa.tui.snapshot import CorrelationRow, Snapshot, parse_correlation_id


def _rows() -> Snapshot:
    return Snapshot(rows=[])


def test_parse_correlation_id_valid() -> None:
    cid = parse_correlation_id("12345678-1234-1234-1234-123456789012")
    assert str(cid) == "12345678-1234-1234-1234-123456789012"


def test_parse_correlation_id_invalid() -> None:
    with pytest.raises(ValueError):
        parse_correlation_id("not-a-uuid")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", ":q", ":quit", "exit", "Q"],
)
def test_quit_commands(raw: str) -> None:
    result = dispatch(raw, _rows())
    # Empty / whitespace -> IGNORE (swallow and continue); everything else quits.
    if raw in {"", "   "}:
        assert result.kind is CommandKind.IGNORE
    else:
        assert result.kind is CommandKind.QUIT


def test_help() -> None:
    result = dispatch("help", _rows())
    assert result.kind is CommandKind.HELP
    assert "command" in (result.note or "")


def test_new_requires_goal() -> None:
    with pytest.raises(ValueError, match="usage: new <goal>"):
        dispatch("new", _rows())


def test_new_parses_goal() -> None:
    result = dispatch("new summarise today's notes", _rows())
    assert result.kind is CommandKind.NEW
    assert result.args["goal"] == "summarise today's notes"


def test_drive_requires_id() -> None:
    with pytest.raises(ValueError, match="usage: drive <correlation-id>"):
        dispatch("drive", _rows())


def test_drive_parses_id(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = CorrelationId(__import__("uuid").UUID("12345678-1234-1234-1234-123456789012"))
    # The dispatcher doesn't touch the runtime, so we can't assert on the
    # exact cid value, but we can assert it parses and the kind is DRIVE.
    result = dispatch(f"drive {uid}", _rows())
    assert result.kind is CommandKind.DRIVE
    assert result.args["correlation_id"] == str(uid)


def test_status_unknown_task() -> None:
    with pytest.raises(ValueError, match="no such task"):
        dispatch("status 12345678-1234-1234-1234-123456789012", _rows())


def test_status_known_task(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = CorrelationId(__import__("uuid").UUID("12345678-1234-1234-1234-123456789012"))
    snapshot = Snapshot(rows=[
        CorrelationRow(
            correlation_id=uid,
            phase=TaskPhase.CREATED,
            modality=ComplexityModality.STANDARD,
            attempts=0,
            tokens_consumed=0,
        )
    ])
    result = dispatch(f"status {uid}", snapshot)
    assert result.kind is CommandKind.STATUS
    assert result.args["correlation_id"] == str(uid)


def test_ledger_unknown_task() -> None:
    with pytest.raises(ValueError, match="no such task"):
        dispatch("ledger 12345678-1234-1234-1234-123456789012", _rows())


def test_ledger_known_task(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = CorrelationId(__import__("uuid").UUID("12345678-1234-1234-1234-123456789012"))
    snapshot = Snapshot(rows=[
        CorrelationRow(
            correlation_id=uid, phase=TaskPhase.CREATED, modality=ComplexityModality.STANDARD,
            attempts=0, tokens_consumed=0,
        )
    ])
    result = dispatch(f"ledger {uid}", snapshot)
    assert result.kind is CommandKind.LEDGER


def test_gate_requires_id() -> None:
    with pytest.raises(ValueError, match="usage: gate"):
        dispatch("gate", _rows())


def test_gate_approve_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = CorrelationId(__import__("uuid").UUID("12345678-1234-1234-1234-123456789012"))
    snapshot = Snapshot(rows=[
        CorrelationRow(
            correlation_id=uid, phase=TaskPhase.AWAITING_HUMAN,
            modality=ComplexityModality.STANDARD, attempts=1, tokens_consumed=0,
        )
    ])
    for raw in [f"gate {uid} --approve", f"gate {uid} --reject", f"gate {uid} --approve --note=hi"]:
        result = dispatch(raw, snapshot)
        assert result.kind is CommandKind.GATE
        if "approve" in raw:
            assert result.args["approved"] == "y"
        else:
            assert result.args["approved"] == "n"


def test_gate_unknown_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = CorrelationId(__import__("uuid").UUID("12345678-1234-1234-1234-123456789012"))
    snapshot = Snapshot(rows=[
        CorrelationRow(
            correlation_id=uid, phase=TaskPhase.AWAITING_HUMAN,
            modality=ComplexityModality.STANDARD, attempts=1, tokens_consumed=0,
        )
    ])
    with pytest.raises(ValueError, match="unexpected flag"):
        dispatch(f"gate {uid} --approve --bogus", snapshot)


def test_unknown_command() -> None:
    with pytest.raises(ValueError, match="unknown command"):
        dispatch("frobnicate foo bar", _rows())


def test_empty_line() -> None:
    # Whitespace and newlines only → swallowed as an ignore, not an error.
    result = dispatch("   \n  ", _rows())
    assert result.kind is CommandKind.IGNORE
