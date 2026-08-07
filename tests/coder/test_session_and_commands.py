"""Session transcript (persistence, resume, compaction) and slash commands."""

from __future__ import annotations

from pathlib import Path

from paa.coder.commands import CommandRegistry, SlashCommand, default_registry, parse_command_line
from paa.coder.session import Session, Turn, TurnRole


class TestSessionPersistence:
    def test_append_and_read(self) -> None:
        s = Session()
        s.user("hello")
        s.assistant("hi", tokens=5)
        assert [t.role for t in s.turns] == [TurnRole.USER, TurnRole.ASSISTANT]
        assert s.total_tokens() == 5

    def test_transcript_survives_resume(self, tmp_path: Path) -> None:
        s = Session(transcript_dir=tmp_path)
        sid = s.session_id
        s.user("remember this")
        s.assistant("noted", tokens=3)
        s.tool_call("read_file", {"path": "a"})
        s.tool_result("read_file", "contents")

        # Simulate a restart: resume from disk.
        resumed = Session.resume(sid, tmp_path)
        assert len(resumed.turns) == 4
        assert resumed.turns[0].content == "remember this"
        assert resumed.turns[3].content == "contents"

    def test_list_sessions(self, tmp_path: Path) -> None:
        Session("aaa", transcript_dir=tmp_path).user("x")
        Session("bbb", transcript_dir=tmp_path).user("y")
        assert Session.list_sessions(tmp_path) == ["aaa", "bbb"]

    def test_append_only_a_crash_loses_at_most_the_last_turn(self, tmp_path: Path) -> None:
        s = Session("s1", transcript_dir=tmp_path)
        s.user("one")
        s.user("two")
        # The file already has both lines flushed; a "crash" now loses nothing.
        path = tmp_path / "s1.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        assert len(lines) == 2


class TestCompaction:
    def test_compaction_shrinks_window_but_keeps_disk(self, tmp_path: Path) -> None:
        s = Session("s1", transcript_dir=tmp_path)
        for i in range(6):
            s.user(f"msg {i}")
        s.compact("summary of the first messages", keep_last=2)

        # In-memory window is the summary + last 2.
        window = s.window()
        assert window[0].role is TurnRole.SUMMARY
        assert len(window) == 3

        # On disk, every original turn plus the summary is preserved.
        resumed = Session.resume("s1", tmp_path)
        assert sum(1 for t in resumed.turns if t.role is TurnRole.USER) == 6

    def test_window_returns_all_turns_before_any_compaction(self) -> None:
        s = Session()
        s.user("a")
        s.user("b")
        assert len(s.window()) == 2


class TestCommandParsing:
    def test_parses_command_and_args(self) -> None:
        assert parse_command_line("/model claude-sonnet-5") == ("model", "claude-sonnet-5")

    def test_bare_slash_is_not_a_command(self) -> None:
        assert parse_command_line("/") is None

    def test_normal_text_is_not_a_command(self) -> None:
        assert parse_command_line("please fix the bug") is None


class TestCommandRegistry:
    async def test_builtin_dispatch(self) -> None:
        reg = default_registry()
        result = await reg.dispatch("/help")
        assert result.handled and result.output

    async def test_unknown_command(self) -> None:
        reg = default_registry()
        result = await reg.dispatch("/nonsense")
        assert result.handled and "Unknown command" in (result.output or "")

    async def test_non_command_passes_through(self) -> None:
        reg = default_registry()
        result = await reg.dispatch("just a normal message")
        assert not result.handled

    async def test_mode_command_emits_action(self) -> None:
        reg = default_registry()
        result = await reg.dispatch("/mode plan")
        assert result.action.get("set_mode") == "plan"

    async def test_markdown_commands_load_and_expand(self, tmp_path: Path) -> None:
        (tmp_path / "review.md").write_text(
            "Review this code carefully:\n$ARGUMENTS", encoding="utf-8"
        )
        reg = CommandRegistry()
        loaded = reg.load_markdown_commands(tmp_path)
        assert loaded == 1

        result = await reg.dispatch("/review the auth module")
        assert result.handled
        assert "the auth module" in (result.prompt or "")

    async def test_positional_substitution(self, tmp_path: Path) -> None:
        (tmp_path / "greet.md").write_text("Hello $1 from $2", encoding="utf-8")
        reg = CommandRegistry()
        reg.load_markdown_commands(tmp_path)
        result = await reg.dispatch("/greet Ada Grace")
        assert result.prompt == "Hello Ada from Grace"

    def test_missing_command_dir_is_not_an_error(self, tmp_path: Path) -> None:
        reg = CommandRegistry()
        assert reg.load_markdown_commands(tmp_path / "nope") == 0
