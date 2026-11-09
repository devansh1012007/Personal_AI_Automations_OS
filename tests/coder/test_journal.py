"""N2.2: the ledger journal for the coder loop (ADR-0046 producer).

What is pinned here
-------------------
* ``LedgerToolJournal`` writes the four v1 tool-call payloads exactly
  as the N2.1 golden rows declare (field-for-field, ``v`` = 1).
* Idempotency: a duplicate PROPOSED append returns the existing event
  through the store's dedupe (one row, not two), with the
  ``session:call_id:kind`` discriminator and ``attempt`` 0.
* Order: the PRE hook runs *before* PROPOSED, so the ledger attests the
  post-hook (redacted) args — a redaction the hook performs is what the
  permission check, the tool, and the PROPOSED row all see.
* A denied call never executes, and its DENIED row is written after its
  PROPOSED row; a failing tool writes FAILED with a null result hash.
* MINOR-4: a ``:``-bearing or empty id is rejected before anything is
  appended, so two calls can never share a discriminator.
* Persisted intent is computed from actual jailed file hashes.
* MINOR-1: ``ToolCtx``'s annotation resolves at runtime.
"""

from __future__ import annotations

import hashlib
import typing
import uuid
from collections.abc import Mapping
from typing import Any

import pytest

from paa.coder.hooks import HookContext, HookEvent, HookRegistry, HookResult
from paa.coder.journal import (
    LedgerToolJournal,
    args_preview_redacted,
    args_sha256_of,
    compute_persisted_intent,
    discriminator_for,
)
from paa.coder.loop import AgentLoop, ModelTurn, ToolCall, ToolCtx
from paa.coder.permissions import PermissionMode, ToolPermissionPolicy
from paa.coder.session import Session
from paa.coder.workspace import Workspace
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import canonical_json
from paa.ledger.store import LedgerStore
from paa.sandbox.base import IsolationLevel
from paa.validation.patch import compute_patch_sha256


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _ctx(**over: Any) -> ToolCtx:
    base: dict[str, Any] = {
        "session_id": "sess-1",
        "call_id": "call-1",
        "tool": "read_file",
        "args_sha256": _sha_text(canonical_json({"path": "a.py"})),
        "args_preview_redacted": "read_file(path=a.py)",
        "isolation_level": IsolationLevel.NONE,
    }
    base.update(over)
    return ToolCtx(**base)


class RecordingTool:
    def __init__(self, name: str, result: str = "ok", *, fail: bool = False) -> None:
        self.name = name
        self.description = f"the {name} tool"
        self.result = result
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def run(self, arguments: Mapping[str, Any]) -> str:
        self.calls.append(dict(arguments))
        if self.fail:
            raise RuntimeError("boom")
        return self.result


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self._turns = turns

    async def propose(
        self, window: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        return self._turns.pop(0) if self._turns else ModelTurn(text="done")


def _journal(ledger: LedgerStore) -> tuple[LedgerToolJournal, uuid.UUID]:
    cid = new_correlation_id()
    return LedgerToolJournal(ledger, cid), cid


async def _types(ledger: LedgerStore, cid: uuid.UUID) -> list[EventType]:
    return [row.event_type for row in await ledger.read_correlation(cid)]


class TestPayloadContracts:
    """The journal's rows are the N2.1 golden rows, field-for-field."""

    async def test_proposed_payload_is_the_golden_shape(self, ledger: LedgerStore) -> None:
        journal, cid = _journal(ledger)
        await journal.before(
            _ctx(
                tool="write_file",
                isolation_level=IsolationLevel.PROCESS,
                pre_sha256="b" * 64,
                post_sha256="c" * 64,
                patch_sha256="d" * 64,
            )
        )
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type is EventType.TOOL_CALL_PROPOSED
        assert row.attempt == 0
        assert row.discriminator == "sess-1:call-1:PROPOSED"
        assert row.payload == {
            "v": 1,
            "session_id": "sess-1",
            "call_id": "call-1",
            "tool": "write_file",
            "args_sha256": _sha_text(canonical_json({"path": "a.py"})),
            "args_preview_redacted": "read_file(path=a.py)",
            "isolation_level": "PROCESS",
            "pre_sha256": "b" * 64,
            "post_sha256": "c" * 64,
            "patch_sha256": "d" * 64,
        }
        assert "fact_id" not in row.payload

    async def test_completed_and_failed_payloads(self, ledger: LedgerStore) -> None:
        journal, cid = _journal(ledger)
        await journal.after(_ctx(call_id="c-ok"), True, "e" * 64)
        await journal.after(_ctx(call_id="c-fail"), False, "f" * 64)
        rows = await ledger.read_correlation(cid)
        assert [r.event_type for r in rows] == [
            EventType.TOOL_CALL_COMPLETED,
            EventType.TOOL_CALL_FAILED,
        ]
        assert rows[0].payload == {
            "v": 1,
            "session_id": "sess-1",
            "call_id": "c-ok",
            "tool": "read_file",
            "result_sha256": "e" * 64,
            "args_sha256": _sha_text(canonical_json({"path": "a.py"})),
            "isolation_level": "NONE",
        }
        # FAILED never attests a partial result hash, even when handed one.
        assert rows[1].payload["result_sha256"] is None
        assert rows[1].payload["isolation_level"] == "NONE"
        for row in rows:
            assert row.attempt == 0
            assert "fact_id" not in row.payload

    async def test_completed_without_result_sha_raises_and_appends_nothing(
        self, ledger: LedgerStore
    ) -> None:
        journal, cid = _journal(ledger)
        with pytest.raises(ValueError, match="result_sha"):
            await journal.after(_ctx(), True, None)
        assert await ledger.read_correlation(cid) == []

    async def test_denied_payload_carries_only_identity_and_reason(
        self, ledger: LedgerStore
    ) -> None:
        journal, cid = _journal(ledger)
        await journal.denied(_ctx(), "protected path: env")
        rows = await ledger.read_correlation(cid)
        assert rows[0].event_type is EventType.TOOL_CALL_DENIED
        assert rows[0].payload == {
            "v": 1,
            "session_id": "sess-1",
            "call_id": "call-1",
            "tool": "read_file",
            "reason": "protected path: env",
        }

    def test_discriminator_is_session_call_kind(self) -> None:
        assert discriminator_for("sess-1", "call-1", "PROPOSED") == "sess-1:call-1:PROPOSED"
        kinds = {
            discriminator_for("s", "c", k)
            for k in ("PROPOSED", "DENIED", "COMPLETED", "FAILED")
        }
        assert len(kinds) == 4


class TestIdempotency:
    async def test_duplicate_proposed_append_returns_existing(
        self, ledger: LedgerStore
    ) -> None:
        """A redelivered PROPOSED is one row, not two (ADR-0008 dedupe)."""
        journal, cid = _journal(ledger)
        await journal.before(_ctx())
        await journal.before(_ctx())
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 1
        assert rows[0].event_type is EventType.TOOL_CALL_PROPOSED
        ok, problems = await ledger.verify_chain(cid)
        assert ok, problems

    async def test_four_kinds_are_four_distinct_rows(self, ledger: LedgerStore) -> None:
        journal, cid = _journal(ledger)
        await journal.before(_ctx())
        await journal.denied(_ctx(), "nope")
        await journal.after(_ctx(), True, "e" * 64)
        await journal.after(_ctx(), False, None)
        rows = await ledger.read_correlation(cid)
        assert len(rows) == 4
        assert len({r.discriminator for r in rows}) == 4


class TestDiscriminatorValidation:
    """MINOR-4: ``:``-bearing and empty ids never reach the store."""

    @pytest.mark.parametrize(
        "session_id,call_id",
        [
            ("", "call-1"),
            ("sess-1", ""),
            ("sess:1", "call-1"),
            ("sess-1", "call:1"),
            ("s:1", "c"),
        ],
    )
    async def test_invalid_ids_raise_before_appending(
        self, ledger: LedgerStore, session_id: str, call_id: str
    ) -> None:
        journal, cid = _journal(ledger)
        with pytest.raises(ValueError):
            await journal.before(_ctx(session_id=session_id, call_id=call_id))
        assert await ledger.read_correlation(cid) == []

    async def test_collision_pair_cannot_share_a_discriminator(
        self, ledger: LedgerStore
    ) -> None:
        """The review's pair ("s:1","c") vs ("s","1:c") must not collide."""
        journal, cid = _journal(ledger)
        with pytest.raises(ValueError):
            await journal.before(_ctx(session_id="s:1", call_id="c"))
        with pytest.raises(ValueError):
            await journal.before(_ctx(session_id="s", call_id="1:c"))
        assert await ledger.read_correlation(cid) == []


def _loop(
    ledger: LedgerStore,
    tools: list[RecordingTool],
    *,
    session_id: str = "sess-1",
    hooks: HookRegistry | None = None,
    **kw: Any,
) -> tuple[AgentLoop, uuid.UUID]:
    """A journalled loop over a real AgentLoop + real LedgerStore."""
    journal, cid = _journal(ledger)
    loop = AgentLoop(
        kw.pop("model"),
        {t.name: t for t in tools},
        session=Session(session_id),
        permissions=kw.pop(
            "permissions",
            ToolPermissionPolicy(mode=PermissionMode.BYPASS),
        ),
        hooks=hooks,
        journal=journal,
        workspace=kw.pop("workspace", None),
        **kw,
    )
    return loop, cid


class TestLoopWiring:
    """Journal ordering through a real AgentLoop with a real store."""

    async def test_success_journals_proposed_then_completed(
        self, ledger: LedgerStore
    ) -> None:
        tool = RecordingTool("read_file", result="file contents")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("read_file", {"path": "a.py"}, "call-1")]),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model)
        await loop.run("read a.py")
        assert tool.calls == [{"path": "a.py"}]
        assert await _types(ledger, cid) == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_COMPLETED,
        ]
        rows = await ledger.read_correlation(cid)
        assert rows[1].payload["result_sha256"] == _sha_text("file contents")

    async def test_denied_call_never_executes(self, ledger: LedgerStore) -> None:
        tool = RecordingTool("bash")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("bash", {"command": "rm -rf /"}, "call-9")]),
                ModelTurn(text="understood"),
            ]
        )
        loop, cid = _loop(
            ledger,
            [tool],
            model=model,
            permissions=ToolPermissionPolicy(
                mode=PermissionMode.BYPASS, deny=["bash(rm*)"]
            ),
        )
        result = await loop.run("clean up")
        assert result.denied_calls == 1
        assert tool.calls == []  # the tool never ran
        assert await _types(ledger, cid) == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_DENIED,
        ]

    async def test_ask_decline_journals_denied_and_never_executes(
        self, ledger: LedgerStore
    ) -> None:
        tool = RecordingTool("bash")

        async def decline(_tool: str, _args: dict, _reason: str) -> bool:
            return False

        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("bash", {"command": "ls"}, "call-2")]),
                ModelTurn(text="ok"),
            ]
        )
        loop, cid = _loop(
            ledger,
            [tool],
            model=model,
            permissions=ToolPermissionPolicy(mode=PermissionMode.ASK),
            confirm=decline,
        )
        result = await loop.run("list")
        assert result.denied_calls == 1
        assert tool.calls == []
        assert await _types(ledger, cid) == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_DENIED,
        ]

    async def test_tool_exception_journals_failed(self, ledger: LedgerStore) -> None:
        tool = RecordingTool("bash", fail=True)
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("bash", {"command": "x"}, "call-3")]),
                ModelTurn(text="failed"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model)
        result = await loop.run("go")
        assert result.denied_calls == 1
        assert await _types(ledger, cid) == [
            EventType.TOOL_CALL_PROPOSED,
            EventType.TOOL_CALL_FAILED,
        ]

    async def test_post_hook_order_proven_by_redaction(
        self, ledger: LedgerStore
    ) -> None:
        """PRE hook redacts -> PROPOSED attests the redacted args only.

        The hook rewrites the secret out of the args before the journal
        sees them; the ledger's args hash must be the hash of the
        *redacted* form, and the raw secret must appear nowhere in the
        PROPOSED payload.
        """
        secret = "sk-1234567890abcdef"

        async def redact_hook(ctx: HookContext) -> HookResult:
            ctx.mutable_arguments["command"] = "echo ***"
            return HookResult()

        hooks = HookRegistry()
        hooks.register(HookEvent.PRE_TOOL_USE, redact_hook)
        tool = RecordingTool("bash")
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall("bash", {"command": f"echo {secret}"}, "call-redact")
                    ]
                ),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model, hooks=hooks)
        await loop.run("echo it")
        assert tool.calls == [{"command": "echo ***"}]  # tool saw the redacted args
        rows = await ledger.read_correlation(cid)
        proposed = rows[0]
        assert proposed.payload["args_sha256"] == args_sha256_of({"command": "echo ***"})
        assert proposed.payload["args_sha256"] != args_sha256_of({"command": f"echo {secret}"})
        assert secret not in canonical_json(proposed.payload)

    async def test_empty_call_ids_are_minted_distinctly(
        self, ledger: LedgerStore
    ) -> None:
        """The legacy "" call_id is minted per call, never deduped into one."""
        tool = RecordingTool("read_file")
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall("read_file", {"path": "a.py"}),
                        ToolCall("read_file", {"path": "b.py"}),
                    ]
                ),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model)
        await loop.run("read two")
        rows = await ledger.read_correlation(cid)
        proposed = [r for r in rows if r.event_type is EventType.TOOL_CALL_PROPOSED]
        assert len(proposed) == 2
        assert len({r.discriminator for r in proposed}) == 2
        assert all(r.payload["call_id"] for r in proposed)


class TestPersistedIntent:
    def test_write_file_intent_hashes_actual_files(self, tmp_path: Any) -> None:
        target = tmp_path / "note.txt"
        target.write_bytes(b"prior bytes")
        pre, post, patch = compute_persisted_intent(
            Workspace(root=tmp_path), "write_file", {"path": "note.txt", "content": "new bytes"}
        )
        assert pre == hashlib.sha256(b"prior bytes").hexdigest()
        assert post == _sha_text("new bytes")
        assert patch is None

    def test_write_file_creation_has_no_pre(self, tmp_path: Any) -> None:
        pre, post, patch = compute_persisted_intent(
            Workspace(root=tmp_path),
            "write_file",
            {"path": "fresh.txt", "content": "hello"},
        )
        assert pre is None
        assert post == _sha_text("hello")
        assert patch is None

    def test_edit_file_intent_predicts_the_replacement(self, tmp_path: Any) -> None:
        target = tmp_path / "a.txt"
        target.write_bytes(b"line one\nold\nline three\n")
        pre, post, patch = compute_persisted_intent(
            Workspace(root=tmp_path),
            "edit_file",
            {"path": "a.txt", "old_str": "old", "new_str": "new"},
        )
        assert pre == hashlib.sha256(b"line one\nold\nline three\n").hexdigest()
        assert post == _sha_text("line one\nnew\nline three\n")
        assert patch is None

    def test_apply_patch_carries_only_the_patch_digest(self, tmp_path: Any) -> None:
        patch_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n"
        pre, post, patch = compute_persisted_intent(
            Workspace(root=tmp_path), "apply_patch", {"patch": patch_text}
        )
        assert (pre, post) == (None, None)
        assert patch == compute_patch_sha256(patch_text)

    def test_non_file_tools_carry_explicit_nulls(self, tmp_path: Any) -> None:
        assert compute_persisted_intent(
            Workspace(root=tmp_path), "bash", {"command": "ls"}
        ) == (None, None, None)
        assert compute_persisted_intent(
            Workspace(root=tmp_path), "read_file", {"path": "a.txt"}
        ) == (None, None, None)
        # No workspace at all (a ledger-only session) is also nulls.
        assert compute_persisted_intent(None, "write_file", {"path": "a.txt"}) == (
            None,
            None,
            None,
        )

    def test_escaped_path_yields_null_intent_not_a_raise(self, tmp_path: Any) -> None:
        """The tool refuses escapes itself; intent must not become a gate."""
        assert compute_persisted_intent(
            Workspace(root=tmp_path),
            "write_file",
            {"path": "../outside.txt", "content": "x"},
        ) == (None, None, None)

    async def test_loop_records_intent_for_a_file_call(
        self, ledger: LedgerStore, tmp_path: Any
    ) -> None:
        target = tmp_path / "note.txt"
        target.write_bytes(b"prior")
        tool = RecordingTool("write_file")
        model = ScriptedModel(
            [
                ModelTurn(
                    tool_calls=[
                        ToolCall(
                            "write_file",
                            {"path": "note.txt", "content": "after"},
                            "call-w",
                        )
                    ]
                ),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(
            ledger, [tool], model=model, workspace=Workspace(root=tmp_path)
        )
        await loop.run("write it")
        rows = await ledger.read_correlation(cid)
        proposed = rows[0].payload
        assert proposed["pre_sha256"] == hashlib.sha256(b"prior").hexdigest()
        assert proposed["post_sha256"] == _sha_text("after")
        assert proposed["patch_sha256"] is None


class TestPreviewRedaction:
    def test_secret_key_value_is_masked_outright(self) -> None:
        preview = args_preview_redacted("bash", {"api_key": "super-secret-value"})
        assert "super-secret-value" not in preview
        assert "api_key=***" in preview

    def test_secret_shaped_value_is_not_disclosed(self) -> None:
        preview = args_preview_redacted("bash", {"command": "echo sk-1234567890abcdef"})
        assert "sk-1234567890abcdef" not in preview
        assert "<str len=" in preview  # a shape, never the content

    def test_opaque_value_is_not_disclosed(self) -> None:
        """The N2.5 route: an opaque secret in a free-form arg cannot
        survive a shape summary — there is no content to leak."""
        preview = args_preview_redacted("bash", {"command": "echo hx7f3a9c-opaque"})
        assert "hx7f3a9c-opaque" not in preview
        assert "<str len=" in preview

    def test_preview_is_bounded(self) -> None:
        preview = args_preview_redacted("write_file", {"content": "x" * 5000})
        assert len(preview) <= 240
        assert "x" * 100 not in preview  # the value itself is never shown


class TestIsolation:
    class _DeclaredTool(RecordingTool):
        isolation_level: IsolationLevel = IsolationLevel.PROCESS

    async def test_declared_isolation_is_attested(self, ledger: LedgerStore) -> None:
        tool = self._DeclaredTool("bash")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("bash", {"command": "ls"}, "call-iso")]),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model)
        await loop.run("list")
        rows = await ledger.read_correlation(cid)
        assert rows[0].payload["isolation_level"] == "PROCESS"

    async def test_undeclared_isolation_is_none(self, ledger: LedgerStore) -> None:
        tool = RecordingTool("read_file")
        model = ScriptedModel(
            [
                ModelTurn(tool_calls=[ToolCall("read_file", {"path": "a.py"}, "call-iso2")]),
                ModelTurn(text="done"),
            ]
        )
        loop, cid = _loop(ledger, [tool], model=model)
        await loop.run("read")
        rows = await ledger.read_correlation(cid)
        assert rows[0].payload["isolation_level"] == "NONE"


class TestAnnotationResolvability:
    """MINOR-1: ToolCtx's annotation must resolve at runtime."""

    def test_get_type_hints_resolves_tool_ctx(self) -> None:
        hints = typing.get_type_hints(ToolCtx)
        assert hints["isolation_level"] is IsolationLevel

    def test_journal_satisfies_the_protocol_structurally(
        self, ledger: LedgerStore
    ) -> None:
        """The loop awaits exactly these three async methods."""
        import inspect

        from paa.coder.loop import ToolJournal

        journal, _cid = _journal(ledger)
        assert isinstance(journal, LedgerToolJournal)
        for method in ("before", "denied", "after"):
            assert inspect.iscoroutinefunction(getattr(journal, method))
            assert inspect.iscoroutinefunction(getattr(ToolJournal, method))
