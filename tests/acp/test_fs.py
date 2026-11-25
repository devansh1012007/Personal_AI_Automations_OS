"""The scoped file read both ACP fronts answer ``fs/read_text_file`` with.

The rules are the coder fs tools' own (ADR-0045), reused through
:mod:`paa.coder.workspace` rather than restated: jail first (resolve against
the workspace root, collapsing ``..`` and following symlinks), protection
second (top-level ``.git``, any-depth ``.env*``), read cap at 64 KB with the
truncation *marked*, binary content refused. Two fronts share one helper
because two implementations of a security answer drift.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.acp import protocol as wire
from paa.acp.fs import read_scoped_text_file
from paa.coder.fs_read_tools import MAX_READ_BYTES


class TestScope:
    async def test_an_in_scope_file_is_served(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hello from the workspace")
        served = read_scoped_text_file(tmp_path, str(tmp_path / "notes.txt"))
        assert served.content == "hello from the workspace"
        assert served.truncated is False

    async def test_a_relative_path_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("x")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, "notes.txt")
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_dotdot_escape_is_refused(self, tmp_path: Path) -> None:
        secret = tmp_path.parent / "outside-secret.txt"
        secret.write_text("outside")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / ".." / "outside-secret.txt"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_symlink_escape_is_refused(self, tmp_path: Path) -> None:
        """The jail resolves symlinks: a link inside the workspace pointing
        outside is an escape, not a loophole."""
        outside = tmp_path.parent / "outside-secret.txt"
        outside.write_text("outside")
        (tmp_path / "link.txt").symlink_to(outside)
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "link.txt"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_an_absolute_path_outside_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, "/etc/hostname")
        assert raised.value.code == wire.INVALID_PARAMS


class TestProtectedPaths:
    async def test_toplevel_git_is_refused(self, tmp_path: Path) -> None:
        config = tmp_path / ".git"
        config.mkdir()
        (config / "config").write_text("[core]")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(config / "config"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_env_is_refused_at_the_root(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("KEY=secret")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / ".env"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_env_variants_are_refused_at_any_depth(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True)
        (nested / ".env.local").write_text("KEY=secret")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(nested / ".env.local"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_nested_git_directory_is_not_toplevel_git(self, tmp_path: Path) -> None:
        """ADR-0045's frozen set protects the *top-level* ``.git`` only; a
        ``.git`` directory in a subdirectory is ordinary data. Pinned here so
        the rule is a fact, not folklore."""
        nested = tmp_path / "sub" / ".git"
        nested.mkdir(parents=True)
        (nested / "config").write_text("[core]")
        served = read_scoped_text_file(tmp_path, str(nested / "config"))
        assert served.content == "[core]"

    async def test_the_refusal_names_the_reason_not_the_content(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".env").write_text("API_KEY=super-secret-value")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / ".env"))
        assert "super-secret-value" not in str(raised.value)


class TestFileSemantics:
    async def test_a_directory_is_invalid_params(self, tmp_path: Path) -> None:
        (tmp_path / "adir").mkdir()
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "adir"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_missing_file_is_resource_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "gone.txt"))
        assert raised.value.code == wire.RESOURCE_NOT_FOUND

    async def test_binary_content_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "blob.bin").write_bytes(b"ok\x00corrupted")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "blob.bin"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_non_utf8_content_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "latin.txt").write_bytes(b"caf\xe9")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "latin.txt"))
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_bom_is_stripped(self, tmp_path: Path) -> None:
        (tmp_path / "bom.txt").write_bytes("caf\u00e9".encode("utf-8-sig"))
        served = read_scoped_text_file(tmp_path, str(tmp_path / "bom.txt"))
        assert served.content == "café"


class TestLineWindows:
    async def test_line_is_one_based(self, tmp_path: Path) -> None:
        (tmp_path / "lines.txt").write_text("one\ntwo\nthree")
        served = read_scoped_text_file(tmp_path, str(tmp_path / "lines.txt"), line=2)
        assert served.content == "two\nthree"

    async def test_limit_bounds_the_count(self, tmp_path: Path) -> None:
        (tmp_path / "lines.txt").write_text("one\ntwo\nthree")
        served = read_scoped_text_file(
            tmp_path, str(tmp_path / "lines.txt"), line=1, limit=2
        )
        assert served.content == "one\ntwo"

    async def test_a_line_past_the_end_is_empty(self, tmp_path: Path) -> None:
        (tmp_path / "lines.txt").write_text("one")
        served = read_scoped_text_file(tmp_path, str(tmp_path / "lines.txt"), line=99)
        assert served.content == ""

    async def test_a_zero_line_is_invalid_params(self, tmp_path: Path) -> None:
        (tmp_path / "lines.txt").write_text("one")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "lines.txt"), line=0)
        assert raised.value.code == wire.INVALID_PARAMS

    async def test_a_string_limit_is_invalid_params(self, tmp_path: Path) -> None:
        (tmp_path / "lines.txt").write_text("one")
        with pytest.raises(wire.AcpError) as raised:
            read_scoped_text_file(tmp_path, str(tmp_path / "lines.txt"), limit="5")
        assert raised.value.code == wire.INVALID_PARAMS


class TestReadCap:
    async def test_an_oversized_file_is_served_capped_and_marked(self, tmp_path: Path) -> None:
        (tmp_path / "big.txt").write_text("x" * (MAX_READ_BYTES + 100))
        served = read_scoped_text_file(tmp_path, str(tmp_path / "big.txt"))
        assert served.truncated is True
        assert len(served.content) == MAX_READ_BYTES

    async def test_a_file_exactly_at_the_cap_is_not_marked(self, tmp_path: Path) -> None:
        (tmp_path / "edge.txt").write_text("x" * MAX_READ_BYTES)
        served = read_scoped_text_file(tmp_path, str(tmp_path / "edge.txt"))
        assert served.truncated is False
        assert len(served.content) == MAX_READ_BYTES
