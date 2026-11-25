"""The recorder core — the harness runs inside PAA's evidence boundary (C7.3).

``paa record run -- <harness-cmd>`` is the flight recorder's front door,
and this module is the sequencing:

1. start the hash-chained flight record (the ``started`` summary is the
   crash marker) and journal the policy that will be in force;
2. snapshot the manifest and git state **before**;
3. open the harness under a :class:`~paa.sandbox.base.SandboxSession` —
   the duplex contract, held open for the whole session: the workspace
   jail, the resource envelope and the whole-tree kill are in force for
   as long as the harness lives, and the harness's stdout streams back
   to the operator's terminal while it runs (``paa record run`` is
   meant to *front* an interactive harness, not to batch it);
4. wait for the harness to exit (or the wall clock to kill it), and
   journal the exit;
5. snapshot the manifest and git state **after**, and classify the
   state the run left behind — :func:`paa.observe.observer.classify_outcome`,
   the same verdict vocabulary the T1 observer uses;
6. write the observe bundle (the run's honest T1 floor: what a watcher
   could see, regardless of what the harness mediated);
7. build the attested change bundle — mandate, diff, the *witnessed*
   command rows from the flight record, the record and the observe
   bundle as evidence blobs, Ed25519-signed — so a reviewer can carry
   the run to a clean machine and ``paa verify`` it;
8. finalise the record, citing the artifacts.

What mediation means here, stated once, where the code enforces it: a
harness that cooperates (points its tool calls at the proxy) gets its
calls policy-checked, contained and journalled — *mediated*. A harness
that does not is still contained, still watched, still classified, but
its effects are visible only as manifest and git deltas — *observed*,
the T1 floor. The record's ``honesty`` block says which is which, and
the attested bundle inherits the distinction: ``observed`` command rows
exist only for calls the record witnessed executing through the proxy.

The recorder never promises a resume: like the observer, it classifies
the state a crash leaves behind and stops there (``resume="none"``).
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import sys
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from paa import __version__
from paa.core.errors import PaaError, SandboxError
from paa.observe.bundle import (
    ObserveBundle,
    capped_stream,
    snapshot_manifest,
)
from paa.observe.observer import (
    classify_outcome,
    git_after,
    git_before,
)
from paa.recorder.record import RunRecord
from paa.sandbox.base import Sandbox, SandboxResult, SandboxSpec

__all__ = [
    "POLICY_MODES",
    "PolicySpec",
    "RecordOutcome",
    "RecorderCore",
    "child_env_for_run",
]

#: The recorder's version, stamped on every artifact it writes.
RECORDER_VERSION = f"paa-recorder/{__version__}"

#: The modes the CLI accepts, mirrored from the permission engine so the
#: refusal can name the whole vocabulary.
POLICY_MODES = ("ask", "accept_edits", "plan", "bypass")

#: The honesty block, verbatim in every record summary. One string, not
#: a per-call decision, because it is a fact about the *format*: what
#: the boundary sees is not a function of what the harness cooperated
#: with.
_HONESTY = {
    "mediated": (
        "tool calls the harness routed through the record proxy are "
        "policy-checked, contained and journalled as tool_call/tool_result "
        "events — the run's only T2 surface"
    ),
    "observed": (
        "everything the harness did outside the proxy is visible only as "
        "manifest and git deltas in the observe bundle — the T1 floor; the "
        "flight record never infers a tool call from a file change"
    ),
    "resume": "none — the recorder classifies the state a crash leaves behind; nothing re-runs",
}


@dataclass(frozen=True, slots=True)
class PolicySpec:
    """The run's policy, as the operator stated it once at spawn time.

    Recorded verbatim in the summary (the proxy rebuilds its engine from
    it) and journalled as a ``policy_config`` event, so the chain covers
    what was in force — a reviewer never has to trust a config file
    that may no longer exist.
    """

    mode: str = "ask"
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "allow": list(self.allow),
            "deny": list(self.deny),
            "ask": list(self.ask),
            "ask_resolution": "denied_unattended",
        }

    def __post_init__(self) -> None:
        if self.mode not in POLICY_MODES:
            raise PaaError(
                f"policy mode {self.mode!r} is not one of {POLICY_MODES}"
            )


def child_env_for_run(run_id: str, run_dir: Path, workspace: Path) -> dict[str, str]:
    """The env the harness receives so it can find the evidence boundary.

    A cooperating harness (or its MCP config) needs exactly one fact:
    where the run record lives. ``PAA_RECORD_PROXY_CMD`` carries the
    ready-to-paste stanza so a human wiring a real harness copies it
    from the record rather than retyping it.
    """
    return {
        "PAA_RECORD_RUN_ID": run_id,
        "PAA_RECORD_RUN_DIR": str(run_dir),
        "PAA_RECORD_WORKSPACE": str(workspace),
        "PAA_RECORD_PROXY_CMD": (
            f"paa record proxy --run-dir {shlex.quote(str(run_dir))}"
        ),
    }


@dataclass(slots=True)
class RecordOutcome:
    """Everything one recorded run produced."""

    run_id: str
    run_dir: Path
    record: RunRecord
    result: SandboxResult
    state: str
    observe_bundle_path: Path
    attest_bundle_dir: Path | None = None
    attest_bundle_id: str | None = None
    attest_error: str | None = None
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def attest_built(self) -> bool:
        return self.attest_bundle_dir is not None


def _manifest_digest(snapshot: Any) -> str:
    """The manifest's own digest — the content identity of the identity."""
    payload = [[f.path, f.size, f.sha256] for f in sorted(snapshot.files, key=lambda f: f.path)]
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


class RecorderCore:
    """The sequencing: record, snapshots, session, snapshots, artifacts."""

    def __init__(
        self,
        sandbox: Sandbox,
        *,
        home: Path,
    ) -> None:
        self._sandbox = sandbox
        self._home = Path(home)

    def run_dir_for(self, run_id: str) -> Path:
        return self._home / "record" / run_id

    # -- the one public action ------------------------------------------------

    async def record(
        self,
        command: Sequence[str],
        *,
        workspace: Path,
        timeout_s: float = 3600.0,
        memory_mb: int = 1024,
        transcript_cap_kb: int = 2048,
        policy: PolicySpec | None = None,
        child_env: Mapping[str, str] | None = None,
        run_id: str | None = None,
        goal: str | None = None,
        attest: bool = True,
        quiet: bool = False,
    ) -> RecordOutcome:
        """Run ``command`` under the session contract and record everything.

        The harness runs inside a :class:`~paa.sandbox.base.SandboxSession`
        for its whole life — the duplex contract C5.2 built for exactly
        this shape of consumer — with the wall clock as the session's
        lifetime budget. Its stdout streams to the operator's stdout as
        it arrives (unless ``quiet``), because ``paa record run`` fronts
        an interactive harness; the transcripts in the artifacts come
        from the session's own capture, the same bytes either way.
        """
        if not command:
            raise SandboxError("record needs a non-empty command")
        rid = run_id or uuid.uuid4().hex
        spec = policy or PolicySpec()
        run_dir = self.run_dir_for(rid)

        record = RunRecord.create(
            run_dir,
            run_id=rid,
            harness=tuple(command),
            workspace=workspace,
            policy=spec.to_json(),
            honesty=dict(_HONESTY),
            recorder_version=RECORDER_VERSION,
        )
        record.append("policy_config", spec.to_json())

        manifest_before = snapshot_manifest(workspace)
        gitb = await git_before(self._sandbox, workspace)
        record.append(
            "snapshot_before",
            {
                "files": manifest_before.count,
                "manifest_sha256": _manifest_digest(manifest_before),
                "git_available": gitb.available,
                "git_head": gitb.head,
            },
        )

        env = dict(child_env_for_run(rid, run_dir, workspace))
        env.update(dict(child_env) if child_env else {})
        session = await self._sandbox.open(
            SandboxSpec(
                command=tuple(command),
                workspace_path=workspace,
                env=env,
                timeout_seconds=timeout_s,
                memory_mb=memory_mb,
            )
        )
        record.append(
            "child_spawned",
            {
                "pid": session.pid,
                "backend": self._sandbox.name,
                "isolation_level": self._sandbox.isolation_level.name,
                "timeout_s": timeout_s,
                "memory_mb": memory_mb,
                "env": "PAA_RECORD_* + caller allowlist",
            },
        )

        # The relay: harness stdout -> operator stdout, live. The
        # artifacts' transcripts come from the session's capture at
        # close; this loop is the operator's window, not the record's.
        stdin_task = self._relay_stdin_if_tty(session, asyncio.get_running_loop())
        try:
            while True:
                chunk = await session.read()
                if not chunk:
                    break
                if not quiet:
                    self._write_stdout(chunk)
        finally:
            if stdin_task is not None:
                stdin_task.stop()
        result = await session.close()

        record.append(
            "child_exit",
            {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "killed_reason": result.killed_reason,
                "duration_ms": round(result.duration_ms, 3),
                "peak_rss_mb": result.peak_rss_mb,
                "backend": result.backend,
                "isolation_level": result.isolation_level.name,
                "memory_enforcement": result.memory_enforcement,
            },
        )

        manifest_after = snapshot_manifest(workspace)
        from paa.observe.bundle import ManifestDiff

        diff = ManifestDiff.between(manifest_before, manifest_after)
        gita = await git_after(self._sandbox, workspace, gitb)
        classification = classify_outcome(result, diff, gita)
        record.append(
            "snapshot_after",
            {
                "files": manifest_after.count,
                "manifest_sha256": _manifest_digest(manifest_after),
                "created": len(diff.created),
                "modified": len(diff.modified),
                "deleted": len(diff.deleted),
                "git_available": gita.available,
                "git_head_after": gita.head_after,
                "git_dirty": gita.dirty,
                "commits": len(gita.commits),
                "state": classification.state.value,
            },
        )

        # The observe bundle: the T1 floor, identical in shape to what
        # `paa observe` would have produced for the same child.
        bundle = ObserveBundle(
            run_id=rid,
            command=tuple(command),
            workspace=str(workspace),
            observer_version=RECORDER_VERSION,
        )
        cap = transcript_cap_kb * 1024
        bundle.child = {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "killed_reason": result.killed_reason,
            "duration_ms": round(result.duration_ms, 3),
            "peak_rss_mb": result.peak_rss_mb,
            "backend": result.backend,
            "isolation_level": result.isolation_level.name,
            "truncated_output": result.truncated_output,
        }
        bundle.transcript_stdout = capped_stream("stdout", result.stdout, cap_bytes=cap)
        bundle.transcript_stderr = capped_stream("stderr", result.stderr, cap_bytes=cap)
        bundle.manifest = {
            "before_count": manifest_before.count,
            "after_count": manifest_after.count,
            **diff.to_json(),
        }
        bundle.git = gita
        bundle.classification = classification.to_json()
        bundle.finalise()
        observe_path = run_dir / "observe-bundle.json"
        bundle.write(observe_path)
        record.note_artifacts({"observe_bundle": str(observe_path)})
        record.note_containment(
            {
                "backend": result.backend,
                "isolation_level": result.isolation_level.name,
                "memory_enforcement": result.memory_enforcement,
                "mediated": "tool calls through the record proxy only",
                "unmediated": (
                    "harness effects outside the proxy are observed as "
                    "manifest/git deltas, never inferred as tool calls"
                ),
            }
        )

        # The attested bundle — the run's reviewer-facing artifact. Built
        # BEFORE the finalise event on purpose: the record blob it embeds
        # is the flight record up to the attestation point, and the
        # finalise event then cites the blob's digest, so the chain's
        # last word covers the exact bytes the reviewer will verify.
        attest_dir: Path | None = None
        attest_id: str | None = None
        attest_error: str | None = None
        if attest:
            try:
                built = self._build_attested(
                    run_dir=run_dir,
                    record=record,
                    workspace=workspace,
                    command=tuple(command),
                    goal=goal,
                    observe_path=observe_path,
                )
                attest_dir, attest_id = built
            except Exception as exc:  # the run still finalises honestly
                attest_error = f"{type(exc).__name__}: {exc}"

        record.finalise(
            extra={
                "classification": classification.state.value,
                "observe_bundle": str(observe_path),
                **(
                    {
                        "attest": {
                            "bundle_id": attest_id,
                            "path": str(attest_dir),
                        }
                    }
                    if attest_dir is not None
                    else {"attest": {"error": attest_error or "not built"}}
                ),
            }
        )

        return RecordOutcome(
            run_id=rid,
            run_dir=run_dir,
            record=record,
            result=result,
            state=classification.state.value,
            observe_bundle_path=observe_path,
            attest_bundle_dir=attest_dir,
            attest_bundle_id=attest_id,
            attest_error=attest_error,
            counts=record.counts,
        )

    # -- the attested bundle ---------------------------------------------------

    def _build_attested(
        self,
        *,
        run_dir: Path,
        record: RunRecord,
        workspace: Path,
        command: tuple[str, ...],
        goal: str | None,
        observe_path: Path,
    ) -> tuple[Path, str]:
        """Build the run's attested bundle; returns ``(dir, bundle_id)``.

        The command rows are the calls the record *witnessed executing*
        through the proxy — ``observed``, each citing the record blob's
        digest, never ``asserted``: the whole difference between the
        recorder and a harness typing its own receipt is that these
        rows are backed by hash-chained events.
        """
        from paa.attest.bundle import build_bundle, load_or_generate_key

        key = load_or_generate_key(self._home / "attest" / "attest-key.json")
        record_bytes = record.events_path.read_bytes()
        record_digest = hashlib.sha256(record_bytes).hexdigest()
        run_id = str(record.summary().get("run_id") or "run")

        command_rows: list[dict[str, Any]] = []
        for event in record.events():
            if event["kind"] != "tool_result":
                continue
            data = event["data"]
            if data.get("tool") != "bash":
                continue
            command_rows.append(
                {
                    "command": list(data.get("argv") or []),
                    "evidence_sha256": record_digest,
                    "exit_code": data.get("exit_code"),
                    "state": "tool_executed" if data.get("ok") else "tool_failed",
                }
            )

        attest_dir = self._home / "attest" / f"record-{run_id[:16]}"
        built = build_bundle(
            out_dir=attest_dir,
            workspace=workspace,
            goal=goal or f"recorded harness session: {shlex.join(command)}",
            requester="operator",
            requester_kind="cli",
            evidence_bundles=[observe_path],
            extra_evidence=[
                (
                    "record",
                    record.events_path,
                    {
                        "kind": "paa-record/1",
                        "run_id": run_id,
                        "events": len(record.events()),
                        "chain_root": record.chain_root,
                        "note": (
                            "the flight record as of the attestation point; "
                            "the record_finalised event cites this blob's "
                            "digest, so the live record extends it visibly"
                        ),
                    },
                )
            ],
            observed_command_rows=command_rows,
            key=key,
        )
        return built.bundle_dir, built.bundle_id

    # -- the operator's window ---------------------------------------------------

    @staticmethod
    def _write_stdout(chunk: bytes) -> None:
        import contextlib

        stream = getattr(sys.stdout, "buffer", None)
        if stream is None:
            with contextlib.suppress(ValueError, OSError):
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
            return
        # A closed pipe (``paa record run … | head``) must not take the
        # run down: the artifacts carry the transcript either way.
        with contextlib.suppress(ValueError, OSError):
            stream.write(chunk)
            stream.flush()

    @staticmethod
    def _relay_stdin_if_tty(session: Any, loop: Any) -> _StdinRelay | None:
        """Pump the operator's stdin into the session, iff it is a TTY.

        A recorded harness is an interactive program fronted by the
        recorder; when stdin is not a TTY (tests, pipes, CI) there is
        nothing to relay and the harness sees EOF, which is the honest
        experience of being run non-interactively.
        """
        try:
            if not sys.stdin.isatty():
                return None
        except (ValueError, OSError):
            return None
        relay = _StdinRelay(session, loop)
        relay.start()
        return relay


class _StdinRelay:
    """A daemon thread pumping the TTY's stdin into the session.

    The relay exists only for the interactive door; it stops when stdin
    ends, when the session dies (writes raise), or when the run ends
    (``stop``). It is deliberately not part of the record: keystrokes
    are the operator's, not the harness's, and the artifacts speak about
    the harness.
    """

    def __init__(self, session: Any, loop: Any) -> None:
        self._session = session
        self._loop = loop
        self._stopped = threading.Event()

    def start(self) -> None:
        thread = threading.Thread(target=self._pump, name="paa-record-stdin", daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _pump(self) -> None:
        import asyncio

        stdin = getattr(sys.stdin, "buffer", None)
        if stdin is None or not hasattr(stdin, "read1"):
            return
        while not self._stopped.is_set():
            try:
                data = stdin.read1(4096)
            except (ValueError, OSError, EOFError):
                return
            if not data:
                return
            future = asyncio.run_coroutine_threadsafe(self._session.write(data), self._loop)
            try:
                future.result(timeout=5)
            except Exception:  # the session died; stop pumping
                return
