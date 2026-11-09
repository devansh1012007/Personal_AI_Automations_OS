"""The filesystem read tools: jailed eyes before hands (N1.1, ADR-0045).

Every test here drives the tools the way the loop would — through
``run(arguments)`` with model-shaped string arguments — because the
jail-then-protect order lives inside ``run`` and a test that called the
workspace helpers directly would prove nothing about the tools.
"""

from __future__ import annotations

import asyncio
import codecs
import fnmatch
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import paa.coder.fs_read_tools as fs_read_tools
from paa.coder.fs_read_tools import (
    FS_READ_TOOLS,
    MAX_READ_BYTES,
    build_fs_read_tools,
)
from paa.coder.loop import AgentLoop, ModelTurn, ToolCall
from paa.coder.permissions import (
    DEFAULT_MUTATING_TOOLS,
    PermissionOutcome,
    ToolPermissionPolicy,
)
from paa.coder.permissions import PermissionMode as CoderMode
from paa.coder.session import Session
from paa.coder.workspace import Workspace, resolve_workspace
from paa.config import reset_settings_cache

#: Frozen jail reason (ADR-0045) — asserted verbatim so a wording change is
#: a breaking change the suite sees.
_ESCAPES = "path escapes the workspace jail"

_OUTSIDE_SECRET = "OUTSIDE_SENTINEL_MUST_NEVER_APPEAR"
_ENV_SECRET = "ENV_SENTINEL_MUST_NEVER_APPEAR"
_GIT_SECRET = "GIT_SENTINEL_MUST_NEVER_APPEAR"
_REAL_CONTENT = "real workspace content\n"


def _write(path: Path, data: str | bytes) -> None:
    """Write exact bytes (a text-mode write would translate newlines on
    Windows and the CRLF test would pass against the wrong bytes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


@pytest.fixture
def ws(tmp_path: Path, paa_home: Path) -> Workspace:
    root = tmp_path / "ws"
    root.mkdir()
    return Workspace(root=resolve_workspace(str(root)))


@pytest.fixture
def tools(ws: Workspace) -> dict[str, Any]:
    return build_fs_read_tools(ws)


@pytest.fixture
def _seeded(ws: Workspace) -> Workspace:
    """A workspace holding a real file, a protected tree and sentinels
    outside it — the fixtures every jail assertion is phrased against."""
    _write(ws.root / "real.txt", _REAL_CONTENT)
    _write(ws.root / ".env", _ENV_SECRET + "\n")
    _write(ws.root / ".git" / "HEAD", _GIT_SECRET + "\n")
    return ws


class TestCatalogue:
    def test_names_are_exactly_the_frozen_three(self, tools: dict[str, Any]) -> None:
        assert set(tools) == {"read_file", "glob", "grep"}
        assert frozenset(tools) == FS_READ_TOOLS

    def test_every_tool_advertises_name_description_schema(
        self, tools: dict[str, Any]
    ) -> None:
        for name, tool in tools.items():
            assert tool.name == name
            assert len(tool.description) >= 20, name
            assert tool.schema()["type"] == "object", name

    def test_read_tools_match_no_mutating_glob(self, tools: dict[str, Any]) -> None:
        """A read tool that matched a mutating glob would be denied in PLAN
        — the plan-mode guarantee for these tools. (Same pin as plan T-N2-9
        for git_stage, from the read side.)"""
        for name in tools:
            assert not any(
                fnmatch.fnmatch(name, pattern) for pattern in DEFAULT_MUTATING_TOOLS
            ), name


class TestReadFile:
    async def test_reads_a_text_file_verbatim(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "note.txt", "hello world\nsecond line\n")

        result = json.loads(await tools["read_file"].run({"path": "note.txt"}))

        assert result["content"] == "hello world\nsecond line\n"
        assert result["truncated"] is False
        assert result["had_bom"] is False
        assert result["encoding"] == "utf-8"
        assert result["size_bytes"] == len("hello world\nsecond line\n")

    async def test_utf8_non_ascii_round_trips(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "uni.txt", "café — ☃ — 日本語\n")

        result = json.loads(await tools["read_file"].run({"path": "uni.txt"}))

        assert result["content"] == "café — ☃ — 日本語\n"

    async def test_crlf_is_preserved_byte_for_byte(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "win.txt", b"first\r\nsecond\r\n")

        result = json.loads(await tools["read_file"].run({"path": "win.txt"}))

        # A text-mode read would deliver "first\nsecond\n" on Windows and
        # destroy the evidence a patch anchored on the bytes would need.
        assert result["content"] == "first\r\nsecond\r\n"
        assert "\r\n" in result["content"]
        assert "\n\n" not in result["content"]

    async def test_lf_only_stays_lf(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "unix.txt", b"a\nb\n")

        result = json.loads(await tools["read_file"].run({"path": "unix.txt"}))

        assert result["content"] == "a\nb\n"
        assert "\r" not in result["content"]

    async def test_bom_is_stripped_and_reported(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "bom.txt", codecs.BOM_UTF8 + b"hello")

        result = json.loads(await tools["read_file"].run({"path": "bom.txt"}))

        assert result["content"] == "hello"
        assert result["had_bom"] is True

    async def test_binary_is_refused_clearly(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "bin.dat", b"abc\x00def")

        result = json.loads(await tools["read_file"].run({"path": "bin.dat"}))

        assert "binary" in result["error"]
        assert "content" not in result

    async def test_non_utf8_is_refused_clearly(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "latin.txt", b"caf\xe9 lait\n")

        result = json.loads(await tools["read_file"].run({"path": "latin.txt"}))

        assert "UTF-8" in result["error"]
        assert "content" not in result

    async def test_missing_file_is_an_error_value(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(await tools["read_file"].run({"path": "nope.txt"}))
        assert result["error"] == "file not found"

    async def test_directory_is_an_error_value(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        (ws.root / "dir").mkdir()

        result = json.loads(await tools["read_file"].run({"path": "dir"}))
        assert "directory" in result["error"]

    async def test_bad_path_arguments_are_error_values(
        self, tools: dict[str, Any]
    ) -> None:
        for bad in ({}, {"path": "  "}, {"path": None}, {"path": 5}):
            result = json.loads(await tools["read_file"].run(bad))
            assert "error" in result, bad

    async def test_absolute_path_inside_workspace_is_allowed(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "abs.txt", "abs\n")

        result = json.loads(
            await tools["read_file"].run({"path": str(ws.root / "abs.txt")})
        )

        assert result["content"] == "abs\n"

    async def test_symlink_to_inside_file_is_allowed(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "real.txt", "data\n")
        try:
            (ws.root / "link.txt").symlink_to(ws.root / "real.txt")
        except OSError:
            pytest.skip("symlinks unavailable on this host")

        result = json.loads(await tools["read_file"].run({"path": "link.txt"}))

        assert result["content"] == "data\n"

    async def test_symlink_escaping_workspace_is_denied(
        self, tools: dict[str, Any], ws: Workspace, tmp_path: Path
    ) -> None:
        _write(tmp_path / "outside" / "secret.txt", _OUTSIDE_SECRET)
        try:
            (ws.root / "link.txt").symlink_to(tmp_path / "outside" / "secret.txt")
        except OSError:
            pytest.skip("symlinks unavailable on this host")

        result = json.loads(await tools["read_file"].run({"path": "link.txt"}))

        assert result["error"] == _ESCAPES
        assert result["reason"] == _ESCAPES
        assert _OUTSIDE_SECRET not in result["error"]

    async def test_symlink_into_protected_subtree_is_denied(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / ".env", _ENV_SECRET)
        try:
            (ws.root / "env-link").symlink_to(ws.root / ".env")
        except OSError:
            pytest.skip("symlinks unavailable on this host")

        result = json.loads(await tools["read_file"].run({"path": "env-link"}))

        assert result["error"] == "protected path: env"

    async def test_truncation_is_marked_not_silent(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        blob = "x" * (MAX_READ_BYTES * 3)
        _write(ws.root / "big.txt", blob)

        result = json.loads(await tools["read_file"].run({"path": "big.txt"}))

        assert result["truncated"] is True
        assert result["size_bytes"] == len(blob)
        assert len(result["content"].encode("utf-8")) == MAX_READ_BYTES
        assert "64 KB" in result["note"]

    async def test_truncation_never_splits_a_character(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        """The cap slice may end mid-character; the tool drops that partial
        character (already past the cap) instead of erroring or mangling."""
        payload = b"a" * (MAX_READ_BYTES - 1) + "☃".encode() + b"b" * 10
        _write(ws.root / "straddle.txt", payload)

        result = json.loads(await tools["read_file"].run({"path": "straddle.txt"}))

        assert result["truncated"] is True
        assert "☃" not in result["content"]
        assert result["content"] == "a" * (MAX_READ_BYTES - 1)

    async def test_invalid_byte_near_the_cap_is_still_refused(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        """A genuine invalid byte sitting two bytes from the 64 KB boundary
        in an over-cap file is binary content, not a cap straddle: the
        decoder must refuse it, never return the prefix as if it were
        valid text."""
        payload = b"a" * (MAX_READ_BYTES - 2) + b"\xe9" + b"b" * 10
        _write(ws.root / "tail-binary.txt", payload)

        result = json.loads(
            await tools["read_file"].run({"path": "tail-binary.txt"})
        )

        assert "UTF-8" in result["error"]
        assert "content" not in result


def _candidate_strategy(tmp_path: Path) -> st.SearchStrategy[str]:
    segments = st.sampled_from(
        [
            "..",
            ".",
            "real.txt",
            ".env",
            ".git",
            "HEAD",
            "sub",
            "outside",
            "secret.txt",
            "normal.txt",
        ]
    )
    joined = st.lists(segments, min_size=1, max_size=4).map(
        lambda parts: parts[0] + "".join("/" + p for p in parts[1:])
    )
    absolute = st.just(str(tmp_path / "outside" / "secret.txt"))
    driveish = st.sampled_from(
        ["C:/outside/secret.txt", "C:foo", "//host/share/x", r"\\host\share\x"]
    )
    return st.one_of(joined, absolute, driveish)


class TestPathJailProperty:
    """T-N1-1 (plan §H): generated paths never resolve outside the
    workspace, and protected content never surfaces — however the path is
    spelled. This is also the committed regression test reviewer NIT-4
    asked for.
    """

    # The function-scoped workspace is intentionally shared across
    # examples: the test only reads it, and the outside sentinel file is
    # rewritten with identical bytes per example, so a per-example reset
    # would add fixtures without changing the assertion.
    @given(candidate=st.data())
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_generated_paths_never_leak_outside_or_protected_content(
        self,
        candidate: st.DataObject,
        ws: Workspace,
        _seeded: Workspace,
        tmp_path: Path,
    ) -> None:
        _write(tmp_path / "outside" / "secret.txt", _OUTSIDE_SECRET)
        tools = build_fs_read_tools(ws)
        text = candidate.draw(_candidate_strategy(tmp_path))

        # A raising tool would surface here as a test failure: the tool
        # answers every candidate with a value, never an exception.
        result = json.loads(
            asyncio.run(tools["read_file"].run({"path": text}))
        )

        # Any content returned must be the workspace's own non-protected
        # content — outside/protected sentinels never surface.
        if "content" in result:
            assert result["content"] == _REAL_CONTENT
        else:
            assert "error" in result
            dumped = json.dumps(result)
            assert _OUTSIDE_SECRET not in dumped
            assert _ENV_SECRET not in dumped
            assert _GIT_SECRET not in dumped


class TestProtectedPaths:
    @pytest.mark.parametrize(
        ("rel", "reason"),
        [
            (".git/HEAD", "protected path: git"),
            (".GIT/head", "protected path: git"),
            (".env", "protected path: env"),
            (".env.local", "protected path: env"),
            (".ENV", "protected path: env"),
        ],
    )
    async def test_protected_paths_denied_with_frozen_reason(
        self, tools: dict[str, Any], ws: Workspace, rel: str, reason: str
    ) -> None:
        _write(ws.root / rel, "secret payload\n")

        result = json.loads(await tools["read_file"].run({"path": rel}))

        assert result["error"] == reason
        assert result["reason"] == reason

    async def test_nested_env_is_denied_at_any_depth(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "sub" / ".env", "secret payload\n")

        result = json.loads(
            await tools["read_file"].run({"path": "sub/.env"})
        )

        assert result["error"] == "protected path: env"

    async def test_protected_check_fires_before_existence(
        self, tools: dict[str, Any]
    ) -> None:
        """A missing protected name still reports protected, not 'not found':
        the refusal must not become an existence oracle for secrets."""
        result = json.loads(await tools["read_file"].run({"path": ".env"}))
        assert result["error"] == "protected path: env"

    async def test_paa_home_inside_workspace_is_denied(
        self, tools: dict[str, Any], ws: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = ws.root / "paa_home"
        _write(home / "ledger.db", "ledger bytes")
        monkeypatch.setenv("PAA_HOME", str(home))
        reset_settings_cache()
        try:
            result = json.loads(
                await tools["read_file"].run({"path": "paa_home/ledger.db"})
            )
        finally:
            reset_settings_cache()

        assert result["error"] == "protected path: paa-home"

    async def test_paa_home_equal_to_workspace_denies_everything(
        self, tools: dict[str, Any], ws: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write(ws.root / "anything.txt", "x\n")
        monkeypatch.setenv("PAA_HOME", str(ws.root))
        reset_settings_cache()
        try:
            result = json.loads(
                await tools["read_file"].run({"path": "anything.txt"})
            )
        finally:
            reset_settings_cache()

        assert result["error"] == "protected path: paa-home"

    async def test_escape_beats_protection(
        self, tools: dict[str, Any]
    ) -> None:
        """Frozen order: the jail runs first, so an escape that also names
        a protected name reports the escape, never the protected reason."""
        result = json.loads(await tools["read_file"].run({"path": "../.env"}))
        assert result["error"] == _ESCAPES


class TestGlob:
    async def test_recursive_pattern_and_sorted_results(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "a.py", "1\n")
        _write(ws.root / "sub" / "b.py", "2\n")
        _write(ws.root / "sub" / "deep" / "c.py", "3\n")
        _write(ws.root / "sub" / "ignore.txt", "4\n")

        result = json.loads(
            await tools["glob"].run({"pattern": "**/*.py"})
        )

        assert result["paths"] == ["a.py", "sub/b.py", "sub/deep/c.py"]
        assert result["truncated"] is False

    async def test_star_stays_in_one_segment(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "top.txt", "1\n")
        _write(ws.root / "sub" / "nested.txt", "2\n")

        result = json.loads(await tools["glob"].run({"pattern": "*.txt"}))

        assert result["paths"] == ["top.txt"]

    async def test_protected_entries_are_never_returned(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / ".env", "secret\n")
        _write(ws.root / "ok.txt", "ok\n")

        result = json.loads(await tools["glob"].run({"pattern": "**/*"}))

        assert "ok.txt" in result["paths"]
        assert ".env" not in result["paths"]
        assert result["skipped_protected"] >= 1

    async def test_git_contents_are_never_listed(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / ".git" / "HEAD", "ref\n")
        _write(ws.root / "ok.txt", "ok\n")

        result = json.loads(await tools["glob"].run({"pattern": "**/*"}))

        assert not any(p.startswith(".git") for p in result["paths"])

    async def test_escape_patterns_are_denied(
        self, tools: dict[str, Any]
    ) -> None:
        for pattern in ("../**", "C:/etc/*", "//host/share/*", r"\\host\share\x"):
            result = json.loads(await tools["glob"].run({"pattern": pattern}))
            assert result["error"] == _ESCAPES, pattern

    async def test_symlink_directory_escape_is_never_followed(
        self, tools: dict[str, Any], ws: Workspace, tmp_path: Path
    ) -> None:
        _write(tmp_path / "outside" / "leaked.txt", "leak\n")
        _write(ws.root / "keep.txt", "keep\n")
        try:
            (ws.root / "dir-link").symlink_to(
                tmp_path / "outside", target_is_directory=True
            )
        except OSError:
            pytest.skip("symlinks unavailable on this host")

        result = json.loads(
            await tools["glob"].run({"pattern": "**/*.txt"})
        )

        assert "keep.txt" in result["paths"]
        assert not any("leaked" in p for p in result["paths"])
        # The symlinked directory itself matches ``**/*``; the jail drops it
        # rather than returning a name that resolves outside.
        listing = json.loads(await tools["glob"].run({"pattern": "**/*"}))
        assert "dir-link" not in listing["paths"]
        assert listing["skipped_escaped"] >= 1

    async def test_limit_truncation_is_marked(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        for i in range(30):
            _write(ws.root / f"f{i:02d}.txt", "x\n")

        result = json.loads(
            await tools["glob"].run({"pattern": "*.txt", "limit": 7})
        )

        assert result["count"] == 7
        assert result["truncated"] is True

    async def test_walk_cap_truncation_is_marked(
        self, tools: dict[str, Any], ws: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fs_read_tools, "_MAX_WALK_ENTRIES", 2)
        for name in ("a.txt", "b.txt", "c.txt"):
            _write(ws.root / name, "x\n")

        result = json.loads(await tools["glob"].run({"pattern": "**/*"}))

        assert result["count"] == 2
        assert result["truncated"] is True
        assert "capped" in result["note"]
        assert "incomplete" in result["note"]

    async def test_empty_pattern_is_an_error_value(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(await tools["glob"].run({"pattern": "  "}))
        assert "error" in result


class TestGrep:
    async def test_matches_carry_path_line_text(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "a.py", "first\nneedle here\nlast\n")
        _write(ws.root / "b.py", "no match\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 1
        assert result["matches"] == [
            {"path": "a.py", "line": 2, "text": "needle here"}
        ]

    async def test_pattern_is_a_regex(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "a.txt", "color\ncolour\ncolouur\n")

        result = json.loads(await tools["grep"].run({"pattern": r"colou?r"}))

        assert result["count"] == 2

    async def test_path_and_glob_scope_the_search(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "a.py", "needle\n")
        _write(ws.root / "sub" / "b.py", "needle\n")
        _write(ws.root / "sub" / "c.txt", "needle\n")

        result = json.loads(
            await tools["grep"].run(
                {"pattern": "needle", "path": "sub", "glob": "*.py"}
            )
        )

        assert result["count"] == 1
        assert result["matches"][0]["path"] == "sub/b.py"

    async def test_invalid_regex_is_an_error_value(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(await tools["grep"].run({"pattern": "([unclosed"}))
        assert "invalid regular expression" in result["error"]

    async def test_binary_files_are_skipped_never_matched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "bin.dat", b"needle\x00needle\n")
        _write(ws.root / "ok.txt", "needle\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 1
        assert result["matches"][0]["path"] == "ok.txt"

    async def test_non_utf8_files_are_skipped_not_matched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "latin.txt", b"needle \xe9\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 0
        assert result["files_scanned"] == 0

    async def test_protected_files_are_never_searched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / ".env", "needle=secret\n")
        _write(ws.root / "ok.txt", "needle\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 1
        assert result["matches"][0]["path"] == "ok.txt"

    async def test_git_tree_is_never_searched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / ".git" / "HEAD", "needle\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 0

    async def test_symlink_escape_file_is_never_read(
        self, tools: dict[str, Any], ws: Workspace, tmp_path: Path
    ) -> None:
        _write(tmp_path / "outside" / "secret.txt", "needle\n")
        try:
            (ws.root / "link.txt").symlink_to(tmp_path / "outside" / "secret.txt")
        except OSError:
            pytest.skip("symlinks unavailable on this host")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 0
        assert _OUTSIDE_SECRET not in json.dumps(result)

    async def test_escape_root_is_denied(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(
            await tools["grep"].run({"pattern": "x", "path": "../.."})
        )
        assert result["error"] == _ESCAPES

    async def test_missing_search_path_is_an_error_value(
        self, tools: dict[str, Any]
    ) -> None:
        result = json.loads(
            await tools["grep"].run({"pattern": "x", "path": "ghost"})
        )
        assert result["error"] == "path not found"

    async def test_limit_truncation_is_marked(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        for i in range(30):
            _write(ws.root / f"f{i:02d}.txt", "needle\n")

        result = json.loads(
            await tools["grep"].run({"pattern": "needle", "limit": 5})
        )

        assert result["count"] == 5
        assert result["truncated"] is True

    async def test_single_file_is_searched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "only.txt", "first\nneedle here\nlast needle\n")

        result = json.loads(
            await tools["grep"].run({"pattern": "needle", "path": "only.txt"})
        )

        assert result["count"] == 2
        assert result["files_scanned"] == 1
        assert result["matches"][0] == {
            "path": "only.txt",
            "line": 2,
            "text": "needle here",
        }
        assert result["matches"][1]["line"] == 3

    async def test_single_file_in_subdirectory_is_searched(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "sub" / "only.txt", "needle\n")

        result = json.loads(
            await tools["grep"].run({"pattern": "needle", "path": "sub/only.txt"})
        )

        assert result["count"] == 1
        assert result["files_scanned"] == 1
        assert result["matches"][0]["path"] == "sub/only.txt"

    async def test_single_file_without_match_reports_one_scanned(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "only.txt", "nothing\n")

        result = json.loads(
            await tools["grep"].run({"pattern": "needle", "path": "only.txt"})
        )

        assert result["count"] == 0
        assert result["files_scanned"] == 1

    async def test_walk_cap_truncation_is_marked(
        self, tools: dict[str, Any], ws: Workspace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fs_read_tools, "_MAX_WALK_ENTRIES", 2)
        for name in ("a.txt", "b.txt", "c.txt"):
            _write(ws.root / name, "needle\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 2
        assert result["files_scanned"] == 2
        assert result["truncated"] is True
        assert "capped" in result["note"]
        assert "incomplete" in result["note"]

    async def test_binary_and_non_utf8_files_are_counted_as_skipped(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "bin.dat", b"needle\x00needle\n")
        _write(ws.root / "latin.txt", b"needle \xe9\n")
        _write(ws.root / "ok.txt", "needle\n")

        result = json.loads(await tools["grep"].run({"pattern": "needle"}))

        assert result["count"] == 1
        assert result["files_scanned"] == 1
        assert result["files_skipped"] == 2


class TestPermissionPosture:
    """Read tools must be allowed in PLAN, allowed without a prompt in ASK
    once the standing-allow set (``FS_READ_TOOLS``) is wired — and never
    refused outright by any coder mode. LOCKDOWN note: the coder
    ``PermissionMode`` has no LOCKDOWN member (ASK/PLAN/ACCEPT_EDITS/
    BYPASS only); the core ``PermissionMode.LOCKDOWN`` grants just
    ``SandboxRun``, which these tools never need — they are local reads,
    not sandbox runs — so the read-only pin is expressed below as
    "non-mutating under every mode that denies mutations".
    """

    @pytest.mark.parametrize("mode", [CoderMode.PLAN, CoderMode.BYPASS])
    def test_plan_and_bypass_allow_all_read_tools(self, mode: CoderMode) -> None:
        policy = ToolPermissionPolicy(mode=mode)

        for name in FS_READ_TOOLS:
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.ALLOW

    def test_ask_with_standing_allow_allows_all_read_tools(self) -> None:
        """Mirrors the runtime-tools mirror test: N1.5 wires
        ``allow=[*FS_READ_TOOLS, ...]``; this pins the policy half."""

        policy = ToolPermissionPolicy(
            mode=CoderMode.ASK, allow=list(FS_READ_TOOLS)
        )

        for name in FS_READ_TOOLS:
            assert policy.evaluate(name, {}).outcome is PermissionOutcome.ALLOW

    def test_ask_without_allow_still_never_denies_read_tools(self) -> None:
        policy = ToolPermissionPolicy(mode=CoderMode.ASK)

        for name in FS_READ_TOOLS:
            assert policy.evaluate(name, {}).outcome is not PermissionOutcome.DENY

    def test_core_lockdown_grants_no_sandbox_beyond_run(self) -> None:
        """LOCKDOWN (core) grants only SANDBOX_RUN; the read tools are
        plain local reads and never request a sandbox.
        """
        from paa.core.types import Permission, PermissionMode

        assert PermissionMode.LOCKDOWN.granted == frozenset({Permission.SANDBOX_RUN})
        # The read tool names match no mutating glob, so a hypothetical
        # coder LOCKDOWN that refused mutations would still allow them.
        for name in FS_READ_TOOLS:
            assert not any(
                fnmatch.fnmatch(name, pat) for pat in DEFAULT_MUTATING_TOOLS
            )

    async def test_reads_execute_under_plan_in_the_real_loop(
        self, tools: dict[str, Any], ws: Workspace
    ) -> None:
        _write(ws.root / "note.txt", "plan-visible\n")

        class ScriptedModel:
            def __init__(self) -> None:
                self.calls = 0

            async def propose(
                self,
                window: list[dict[str, Any]],
                tools: list[dict[str, Any]],
            ) -> ModelTurn:
                self.calls += 1
                if self.calls == 1:
                    return ModelTurn(
                        tool_calls=[ToolCall("read_file", {"path": "note.txt"})]
                    )
                return ModelTurn(text="done")

        loop = AgentLoop(
            ScriptedModel(),
            {"read_file": tools["read_file"]},
            session=Session(),
            permissions=ToolPermissionPolicy(mode=CoderMode.PLAN),
        )

        result = await loop.run("read the note")

        assert result.tool_calls == 1
        assert result.denied_calls == 0
        results = [t for t in loop._session.turns if t.role.value == "tool_result"]
        assert "plan-visible" in results[-1].content
