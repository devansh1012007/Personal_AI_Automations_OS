"""Secret hygiene canary test (N2.5 / T-N2-8): the canary never leaks.

A canary credential is planted in the host environment AND in a ``.env`` file
(via ``tmp_path`` + ``reset_dotenv_cache`` — never the repo ``.env``), and this
suite proves it never survives into a ledger payload, a log line, a context
packet, or any tool output — including a ``bash`` call that echoes its own
environment.

Three mechanisms compose here, and the test pins each link:

1. **The env allowlist** (:func:`~paa.coder.shell_tools._parse_env` +
   ``build_child_env``): the child environment is built from empty, so a
   ``bash`` echo of ``$env:<canary var>`` can only leak when the caller
   explicitly allowlists the variable.
2. **``redact()``** (:func:`paa.models.base.redact`): the literal canary is
   scrubbed only when the caller hands it over as a known secret. The generic
   patterns (``sk-…``, ``Bearer …``, ``api-key=…``) catch credential-*shaped*
   values, but an opaque canary matches none — so a payload builder that
   never passes the literal secret is a leak, and the negative pin
   ``test_redact_without_the_literal_misses_an_opaque_canary`` exists to make
   that obligation executable rather than prose. This is the contract the
   N2.2 ledger journal must satisfy:
   :attr:`~paa.coder.loop.ToolCtx.args_preview_redacted` says "Never contains
   a raw secret (N2.5 canary test)" — this file is that test.
3. **Protected-path denial** (``coder/workspace.py``): a refusal names the
   protection (``protected path: env``), never the file's content, so the
   denial of a canary-bearing ``.env`` is itself clean.

**What is not claimed.** The runtime tools (``submit_task``,
``remember_fact``…) are out of the sweep: they take no ambient environment
and run no shell, so their canary surface is identical to the work tools'
(outputs never inject env), and building a full ``Runtime`` for zero marginal
leak surface was judged not worth the shared-fixture import.

Every assertion goes through :func:`assert_canary_absent` — one helper, so a
fifth copy of the check cannot drift from the first four.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from paa.coder import shell_tools as st
from paa.coder.fs_read_tools import build_fs_read_tools
from paa.coder.fs_write_tools import FsWriteSession, build_fs_write_tools
from paa.coder.git_tools import build_git_tools
from paa.coder.journal import (
    LedgerToolJournal,
    args_preview_redacted,
    args_sha256_of,
)
from paa.coder.loop import ToolCtx
from paa.coder.workspace import Workspace, resolve_workspace
from paa.config import dotenv_value, reset_dotenv_cache, reset_settings_cache
from paa.context.gatherer import ContextElement, ContextPacket
from paa.core.types import EventType, new_correlation_id
from paa.ledger.events import LedgerEvent, canonical_json
from paa.ledger.store import LedgerStore
from paa.models.base import redact, redact_detail
from paa.sandbox._win32 import IS_WINDOWS
from paa.sandbox.base import IsolationLevel
from paa.sandbox.dryrun_backend import DryRunSandbox
from paa.sandbox.subprocess_backend import SubprocessSandbox

#: The environment variable the canary lives under. Mirrors the naming of the
#: host canary in ``tests/coder/test_shell_tools.py`` (``PAA_TEST_HOST_SECRET``)
#: but is a different variable so this suite owns both of its canaries and a
#: reader can grep either one.
CANARY_VAR = "PAA_CANARY_SECRET"

#: An opaque canary: long enough to be redactable (``>= 8`` chars, the
#: ``redact`` floor) but matching NONE of the generic secret patterns, so it
#: is removed only when the caller passes it as a known secret. That is the
#: honest shape of a real credential (a project token has no ``sk-`` prefix).
CANARY = "hx7f3a9c-canary-secret-4e91bd"

#: A credential-*shaped* canary: matched by the generic ``sk-`` pattern, so
#: ``redact`` catches it even when the caller never saw it. Same value shape
#: as ``tests/models/conftest.py::FAKE_KEY``.
SK_CANARY = "sk-canary-abcdef0123456789ABCDEF"

#: Substrings that must never appear in anything derived from the canary's
#: environment: the value itself, and the value as a bearer credential.
_FORBIDDEN = (CANARY, SK_CANARY, f"Bearer {CANARY}", f"Bearer {SK_CANARY}")


def assert_canary_absent(*blobs: object, label: str = "payload") -> None:
    """Fail if any canary form appears in any blob.

    The single assertion of this suite: run against tool output, captured log
    entries, ledger payloads, and serialized context packets alike.
    """
    for blob in blobs:
        text = str(blob)
        for forbidden in _FORBIDDEN:
            assert forbidden not in text, (
                f"canary leaked into {label} (matched {forbidden!r}): {text[:400]!r}"
            )


def _ws(root: Path) -> Workspace:
    return Workspace(root=resolve_workspace(str(root)))


def _payload(raw: str) -> dict[str, Any]:
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data


def _require_shell() -> None:
    if shutil.which(st.shell_executable()) is None:
        pytest.skip(f"shell {st.shell_executable()} not on PATH")


def _print_env_cmd(name: str) -> str:
    return f"Write-Output $env:{name}" if IS_WINDOWS else f'printf "%s" "${name}"'


def _echo_cmd(text: str) -> str:
    return f'Write-Output "{text}"' if IS_WINDOWS else f"echo {text}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path) -> Workspace:
    """A real git repository with one committed seed file (mirrors the
    fixture pattern of ``tests/coder/test_git_tools.py``)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "--", "seed.txt")
    _git(repo, "commit", "-m", "seed")
    return Workspace(root=repo)


# ---------------------------------------------------------------------------
# 1. The redact contract: both passes, and the boundary between them.
# ---------------------------------------------------------------------------


class TestRedactContract:
    def test_redact_removes_literal_canary_when_passed(self) -> None:
        scrubbed = redact(f"command: echo {CANARY_VAR}={CANARY} (done)", CANARY)
        assert_canary_absent(scrubbed)
        assert "***REDACTED***" in scrubbed

    def test_redact_without_the_literal_misses_an_opaque_canary(self) -> None:
        """The boundary N2.2 must respect: without the literal secret the
        opaque canary survives ``redact`` and ``redact_detail``.

        This is not a flaw in the assertion — it is the documented shape of
        the two-pass policy (``models/base.py``): the generic patterns cannot
        know a value the caller was never handed. A journal that previews raw
        args without passing the known secrets to ``redact`` leaks, and this
        pin says so in executable form.
        """
        text = f"owner={CANARY}"
        assert CANARY in redact(text)
        assert CANARY in str(redact_detail({"preview": text}))

    def test_generic_pattern_catches_sk_shaped_canary_without_being_told(
        self,
    ) -> None:
        scrubbed = redact(f"token: {SK_CANARY} expired")
        assert SK_CANARY not in scrubbed
        assert "***REDACTED***" in scrubbed

    def test_redact_detail_scrubs_shaped_canary_at_any_depth(self) -> None:
        """The recursion scrubs at every depth — for a credential-*shaped*
        value, which the generic patterns catch. (The opaque canary needs
        the literal handed to ``redact``; the negative pin above records
        that boundary.)"""
        detail = {"nested": [{"preview": f"x={SK_CANARY}"}], "count": 3}
        scrubbed = redact_detail(detail)
        assert_canary_absent(scrubbed, label="redact_detail result")
        assert isinstance(scrubbed, dict)
        nested = scrubbed["nested"]
        assert isinstance(nested, list)
        first = nested[0]
        assert isinstance(first, dict)
        assert first["preview"] == "x=***REDACTED***"
        assert scrubbed["count"] == 3

    def test_short_secret_below_the_floor_is_not_redacted(self) -> None:
        """A value under 8 chars is more likely a common substring than a
        credential (``models/base.py:251-253``); the canary in this suite is
        deliberately long, and this pins why."""
        assert redact("code=abc123 ok", "abc123") == "code=abc123 ok"

    def test_helper_catches_an_injected_leak(self) -> None:
        """Sensitivity proof: the house helper must FAIL when the canary is
        present. If this test ever passes with a leaked blob, the helper is
        vacuous and every other test in the file is too."""
        with pytest.raises(AssertionError, match="canary leaked"):
            assert_canary_absent(f"leak: {CANARY}", label="injected control")
        with pytest.raises(AssertionError, match="canary leaked"):
            assert_canary_absent(f"leak: {SK_CANARY}", label="injected control")
        # And the clean case passes.
        assert_canary_absent("nothing to see here", label="clean control")


# ---------------------------------------------------------------------------
# 2. The canary is live: env + .env via tmp_path, never the repo .env.
# ---------------------------------------------------------------------------


class TestCanaryIsLive:
    def test_dotenv_canary_is_readable_from_a_tmp_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".env").write_text(
            f"{CANARY_VAR}={CANARY}\nOTHER=1\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        reset_dotenv_cache()
        try:
            assert dotenv_value(CANARY_VAR) == CANARY
        finally:
            reset_dotenv_cache()

    def test_host_env_canary_is_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        assert os.environ[CANARY_VAR] == CANARY


# ---------------------------------------------------------------------------
# 3. Tool output hygiene: every work tool, fast (dry-run / builders).
# ---------------------------------------------------------------------------


class TestToolOutputHygiene:
    async def test_bash_child_env_never_inherits_the_host_canary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        dry = DryRunSandbox(stdout="canned")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)

        with structlog.testing.capture_logs() as logs:
            raw = await tool.run({"command": _echo_cmd("hi")})

        assert_canary_absent(raw, label="bash payload")
        assert_canary_absent(logs, label="bash dry-run logs")
        invocation = dry.last_invocation
        assert invocation is not None
        # The recorded env is the real build_child_env output (dryrun_backend.py).
        assert_canary_absent(invocation["env"], label="child env")
        assert CANARY_VAR not in invocation["env"]

    async def test_bash_allowlist_is_real_not_everything_dropped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control: an explicitly allowlisted variable DOES reach the
        child, so the absence above is the allowlist working, not a broken
        pipe that drops everything."""
        monkeypatch.setenv(CANARY_VAR, CANARY)
        dry = DryRunSandbox(stdout="canned")
        tool = st.BashTool(_ws(tmp_path), sandbox=dry)

        raw = await tool.run(
            {
                "command": _echo_cmd("hi"),
                "env": {"PAA_SHELL_ALLOW": "allowed-value"},
            }
        )

        assert_canary_absent(raw, label="bash payload")
        invocation = dry.last_invocation
        assert invocation is not None
        assert invocation["env"].get("PAA_SHELL_ALLOW") == "allowed-value"
        assert CANARY_VAR not in invocation["env"]

    async def test_read_tools_emit_no_canary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        # write_bytes: write_text would translate "\n" to CRLF on Windows and
        # read_file preserves it — the assertion below is about hygiene.
        (tmp_path / "plain.txt").write_bytes(b"hello world\n")
        (tmp_path / "code.py").write_bytes(b"print('hi')\n")
        tools = build_fs_read_tools(_ws(tmp_path))

        with structlog.testing.capture_logs() as logs:
            read_raw = await tools["read_file"].run({"path": "plain.txt"})
            glob_raw = await tools["glob"].run({"pattern": "**/*.txt"})
            grep_raw = await tools["grep"].run({"pattern": "hello"})

        for name, raw in (
            ("read_file", read_raw),
            ("glob", glob_raw),
            ("grep", grep_raw),
        ):
            assert_canary_absent(raw, label=f"{name} payload")
        assert_canary_absent(logs, label="read-tool logs")
        # Sanity: the tools actually did the work (not vacuously failing).
        assert _payload(read_raw)["content"] == "hello world\n"
        grep = _payload(grep_raw)
        assert len(grep["matches"]) == 1
        assert grep["matches"][0]["path"] == "plain.txt"

    async def test_write_tools_emit_no_canary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        session = FsWriteSession(_ws(tmp_path))
        tools = build_fs_write_tools(session.workspace, session=session)

        with structlog.testing.capture_logs() as logs:
            write_raw = await tools["write_file"].run(
                {"path": "a.txt", "content": "clean content\n"}
            )
            edit_raw = await tools["edit_file"].run(
                {"path": "a.txt", "old_str": "clean", "new_str": "tidy"}
            )
            patch = (
                "--- /dev/null\n+++ b/patch.txt\n@@ -0,0 +1,1 @@\n+patched clean\n"
            )
            patch_raw = await tools["apply_patch"].run({"patch": patch})

        for name, raw in (
            ("write_file", write_raw),
            ("edit_file", edit_raw),
            ("apply_patch", patch_raw),
        ):
            assert_canary_absent(raw, label=f"{name} payload")
        assert_canary_absent(logs, label="write-tool logs")
        assert _payload(write_raw)["ok"] is True
        assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "tidy content\n"
        assert (tmp_path / "patch.txt").read_text(encoding="utf-8") == "patched clean\n"


# ---------------------------------------------------------------------------
# 4. Protected-path denial: the reason names the protection, never the secret.
# ---------------------------------------------------------------------------


class TestProtectedPathDenial:
    async def test_reading_the_env_file_is_denied_and_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        (tmp_path / ".env").write_text(
            f"{CANARY_VAR}={CANARY}\n", encoding="utf-8"
        )
        tools = build_fs_read_tools(_ws(tmp_path))

        with structlog.testing.capture_logs() as logs:
            read_raw = await tools["read_file"].run({"path": ".env"})
            grep_raw = await tools["grep"].run(
                {"pattern": "CANARY", "path": ".env"}
            )
            glob_raw = await tools["glob"].run({"pattern": ".env*"})

        read = _payload(read_raw)
        assert read.get("reason") == "protected path: env"
        assert_canary_absent(read_raw, label="denied read payload")
        assert_canary_absent(grep_raw, label="denied grep payload")
        assert_canary_absent(glob_raw, label="glob payload")
        assert_canary_absent(logs, label="denial logs")
        # glob never lists the protected entry, and says it skipped one.
        glob = _payload(glob_raw)
        assert ".env" not in glob["paths"]
        assert glob["skipped_protected"] == 1

    async def test_writing_the_env_file_is_denied_and_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CANARY_VAR, CANARY)
        (tmp_path / ".env").write_text(
            f"{CANARY_VAR}={CANARY}\n", encoding="utf-8"
        )
        session = FsWriteSession(_ws(tmp_path))
        tools = build_fs_write_tools(session.workspace, session=session)

        with structlog.testing.capture_logs() as logs:
            raw = await tools["write_file"].run(
                {"path": ".env", "content": "overwritten\n"}
            )

        payload = _payload(raw)
        assert payload["ok"] is False
        assert payload["error"] == "protected path: env"
        assert_canary_absent(raw, label="denied write payload")
        assert_canary_absent(logs, label="denial logs")
        # The canary-bearing file is untouched.
        assert (tmp_path / ".env").read_text(encoding="utf-8") == (
            f"{CANARY_VAR}={CANARY}\n"
        )


# ---------------------------------------------------------------------------
# 5. Ledger + context payloads: redacted at build time, clean on readback.
# ---------------------------------------------------------------------------


class TestLedgerAndContextPayloads:
    async def test_redacted_tool_call_payload_is_clean_on_readback(
        self, ledger: LedgerStore
    ) -> None:
        """A TOOL_CALL_PROPOSED payload built through redact() (the shape the
        N2.2 journal writes, ``args_preview_redacted``) carries no canary
        after a store round-trip."""
        collision_id = new_correlation_id()
        preview = redact(
            f"bash(command=echo {CANARY_VAR}={CANARY})", CANARY
        )
        payload = {
            "v": 1,
            "session_id": "sess-canary",
            "call_id": "call-1",
            "tool": "bash",
            "args_sha256": "a" * 64,
            "args_preview_redacted": preview,
            "isolation_level": "PROCESS",
        }
        event = LedgerEvent(
            correlation_id=collision_id,
            event_type=EventType.TOOL_CALL_PROPOSED,
            state_version=1,
            payload=payload,
        )
        await ledger.append(event)

        events = await ledger.read_correlation(collision_id)
        assert len(events) == 1
        readback = events[0].payload
        assert_canary_absent(readback, label="ledger payload")
        assert_canary_absent(canonical_json(readback), label="canonical ledger bytes")
        assert "***REDACTED***" in str(readback["args_preview_redacted"])

    async def test_control_unredacted_ledger_payload_would_leak(
        self, ledger: LedgerStore
    ) -> None:
        """Control: an UNREDACTED payload read back from the same store DOES
        contain the canary — proving the readback assertion above is
        sensitive, not vacuous. The canary lives only in a throwaway
        ``tmp_path`` database that dies with this test."""
        collision_id = new_correlation_id()
        payload = {
            "v": 1,
            "session_id": "sess-canary",
            "call_id": "call-control",
            "tool": "bash",
            "args_sha256": "b" * 64,
            "args_preview_redacted": f"bash(command=echo {CANARY})",
            "isolation_level": "PROCESS",
        }
        event = LedgerEvent(
            correlation_id=collision_id,
            event_type=EventType.TOOL_CALL_PROPOSED,
            state_version=1,
            payload=payload,
        )
        await ledger.append(event)

        events = await ledger.read_correlation(collision_id)
        assert len(events) == 1
        assert CANARY in str(events[0].payload)

    def test_context_packet_built_from_redacted_content_is_clean(self) -> None:
        element = ContextElement(
            id="fact-1",
            content=redact(f"the deployment secret is {CANARY}", CANARY),
        )
        packet = ContextPacket(
            elements=(element,),
            density=1.0,
            allocated_tokens=12,
            routing_directive="PROCEED_TO_PLANNER",
            pollution_ratio=0.0,
            utility_score=1.0,
            entropy=0.0,
            token_ceiling=100,
        )
        serialized = packet.model_dump_json()
        assert_canary_absent(serialized, label="context packet JSON")
        assert "***REDACTED***" in serialized


# ---------------------------------------------------------------------------
# 6. The shell echo: the headline scenario, against the real sandbox (slow).
# ---------------------------------------------------------------------------


class TestBashEchoCanary:
    @pytest.mark.slow
    async def test_echo_of_the_canary_var_does_not_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_shell()
        monkeypatch.setenv(CANARY_VAR, CANARY)
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())

        with structlog.testing.capture_logs() as logs:
            raw = await tool.run({"command": _print_env_cmd(CANARY_VAR)})

        payload = _payload(raw)
        assert payload["ok"] is True
        assert payload["exit_code"] == 0
        assert_canary_absent(raw, label="bash echo payload")
        assert_canary_absent(logs, label="bash echo logs")
        # The child never saw the variable: echo printed nothing.
        assert str(payload["stdout"]).strip() == ""

    @pytest.mark.slow
    async def test_full_env_dump_has_no_canary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_shell()
        monkeypatch.setenv(CANARY_VAR, CANARY)
        tool = st.BashTool(_ws(tmp_path), sandbox=SubprocessSandbox())
        dump = "Get-ChildItem Env:" if IS_WINDOWS else "env"

        with structlog.testing.capture_logs() as logs:
            raw = await tool.run({"command": dump})

        assert_canary_absent(raw, label="bash env-dump payload")
        assert_canary_absent(logs, label="bash env-dump logs")
        assert CANARY_VAR not in str(_payload(raw)["stdout"])


# ---------------------------------------------------------------------------
# 7. Git tools: fixed argv, real git, outputs clean (slow).
# ---------------------------------------------------------------------------


class TestGitTools:
    @pytest.mark.slow
    async def test_git_tools_emit_no_canary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _require_shell()
        monkeypatch.setenv(CANARY_VAR, CANARY)
        ws = _make_repo(tmp_path)
        tools = build_git_tools(ws)
        (ws.root / "seed.txt").write_text("seed changed\n", encoding="utf-8")

        with structlog.testing.capture_logs() as logs:
            status_raw = await tools["git_status"].run({})
            diff_raw = await tools["git_diff"].run({})
            stage_raw = await tools["git_stage"].run({"paths": ["seed.txt"]})
            commit_raw = await tools["git_commit"].run({"message": "canary run"})

        for name, raw in (
            ("git_status", status_raw),
            ("git_diff", diff_raw),
            ("git_stage", stage_raw),
            ("git_commit", commit_raw),
        ):
            assert_canary_absent(raw, label=f"{name} payload")
        assert_canary_absent(logs, label="git-tool logs")
        assert _payload(status_raw)["exit_code"] == 0
        commit = _payload(commit_raw)
        assert commit["exit_code"] == 0
        assert len(commit["commit"]) == 40


# ---------------------------------------------------------------------------
# 8. The journal preview + PROPOSED payload (N2.2, W2-IMPL-02, parallel).
# ---------------------------------------------------------------------------


class TestJournalRedaction:
    """The journal preview must never carry the canary into the ledger.

    ``args_preview_redacted`` is the helper the loop calls when it builds a
    :class:`~paa.coder.loop.ToolCtx` (``_build_journal_ctx``, loop.py), and
    :class:`~paa.coder.journal.LedgerToolJournal` writes that field verbatim
    into the ``TOOL_CALL_PROPOSED`` payload. These tests therefore go through
    the real helper and a real ``before()`` + readback: a preview leak is a
    ledger leak, by construction.
    """

    def test_preview_masks_secret_keyed_canary(self) -> None:
        preview = args_preview_redacted("bash", {"api_key": CANARY})
        assert_canary_absent(preview, label="journal preview")
        assert "api_key=***" in preview

    def test_preview_redacts_shaped_canary_in_freeform_arg(self) -> None:
        preview = args_preview_redacted(
            "bash", {"command": f"echo {SK_CANARY}"}
        )
        assert_canary_absent(preview, label="journal preview")

    def test_preview_redacts_opaque_canary_in_freeform_arg(self) -> None:
        """An opaque canary in a free-form arg (the hostile route: the model
        copies an env secret into a shell ``command``) must not survive the
        preview either.

        The preview helper's contract (``journal.py`` docstring) is to run
        every value through ``redact`` — literal secrets plus generic
        patterns. The shape of the helper matters: it only gets the args
        mapping, and an opaque value matches no generic pattern, so without
        the literal secret handed over this value survives. The pin below
        is the ``ToolCtx`` field contract — "Never contains a raw secret
        (N2.5 canary test)" — in executable form.
        """
        preview = args_preview_redacted(
            "bash", {"command": f"echo {CANARY}"}
        )
        assert_canary_absent(preview, label="journal preview")

    async def test_journal_before_writes_clean_proposed_payload(
        self, ledger: LedgerStore
    ) -> None:
        """End to end: a canary-bearing bash call journalled through
        ``before()`` leaves no canary in the stored PROPOSED payload."""
        args = {"command": f"echo {CANARY}", "env": {CANARY_VAR: CANARY}}
        ctx = ToolCtx(
            session_id="sess-canary",
            call_id="call-1",
            tool="bash",
            args_sha256=args_sha256_of(args),
            args_preview_redacted=args_preview_redacted("bash", args),
            isolation_level=IsolationLevel.PROCESS,
        )
        correlation_id = new_correlation_id()
        journal = LedgerToolJournal(ledger, correlation_id)

        await journal.before(ctx)

        events = await ledger.read_correlation(correlation_id)
        assert len(events) == 1
        proposed = events[0]
        assert proposed.event_type is EventType.TOOL_CALL_PROPOSED
        assert_canary_absent(proposed.payload, label="PROPOSED payload")
        assert_canary_absent(
            canonical_json(proposed.payload), label="PROPOSED canonical bytes"
        )


def test_reset_settings_cache_does_not_disturb_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache hygiene: the ``.env`` reader and ``PAA_HOME`` settings reset are
    independent caches; resetting one never re-reads the other."""
    (tmp_path / ".env").write_text(f"{CANARY_VAR}={CANARY}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reset_dotenv_cache()
    reset_settings_cache()
    try:
        assert dotenv_value(CANARY_VAR) == CANARY
    finally:
        reset_dotenv_cache()
