"""Container containment, with gVisor when the runtime is present.

This is the backend the RFC actually describes. Docker is **not installed on
the target machine**, so nothing here runs today — but it is implemented in
full rather than stubbed, because the alternative is discovering on the day
Docker appears that the "supported" path was never written.

:meth:`DockerSandbox.healthcheck` returns ``False`` when the CLI is absent, so
the ``auto`` factory skips it cleanly and the runtime degrades to WSL or
subprocess without the caller doing anything.

Isolation levels this backend can report:

``VM``
    ``--runtime=runsc`` is available and selected. gVisor interposes a
    user-space kernel; the container's syscalls are serviced by Sentry, not by
    the host kernel. This is the RFC §13 requirement, actually met.
``NAMESPACE``
    Stock runc. Namespaces plus cgroups plus seccomp-default — strong, and
    still not a syscall boundary.

The level is resolved per-run from what Docker reports, never assumed. A
backend that *claims* VM while silently running runc would be worse than one
that admits NAMESPACE, because the policy layer routes high-risk work on the
strength of that claim.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import posixpath
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from paa.config import SandboxSettings
from paa.core.errors import SandboxError
from paa.sandbox.base import (
    IsolationLevel,
    Sandbox,
    SandboxResult,
    SandboxSpec,
    build_child_env,
    resolve_workspace,
)

__all__ = ["DockerSandbox"]

log = structlog.get_logger(__name__)

_HEALTHCHECK_TIMEOUT_SECONDS = 20.0
_STOP_TIMEOUT_SECONDS = 15.0

#: How long the backend waits for a **killed** container to be recorded as
#: stopped. The kill has already been sent, and the daemon records the exit
#: asynchronously — normally within a few hundred milliseconds.
#:
#: It is deliberately shorter than :data:`_STOP_TIMEOUT_SECONDS`, which is the
#: *client's* patience with a process that will not die. A container that has
#: not stopped by now is a container whose kill did not land — a double
#: fault — and the ``docker rm -f`` in the ``finally`` is what cleans it up.
#: Waiting the full 15 s here would stall the timeout path, which is already a
#: failure, for a fault that has already happened.
_KILL_SETTLE_TIMEOUT_SECONDS = 5.0

#: How long the backend waits for the daemon to answer a *query* it makes
#: after the container has stopped — the final ``inspect`` and the ``logs``
#: read. It is deliberately not the start grace: the grace is the operator's
#: budget for the container's start, and this is the backend's own patience
#: with a daemon that is merely slow to answer a question about a container
#: that has already finished.
_DAEMON_QUERY_TIMEOUT_SECONDS = 15.0

#: How long the "is our image on the daemon?" answer is reused before it is
#: asked again. Short enough that an image built after the first probe shows
#: up without a restart, long enough that a dashboard redraw is free.
_IMAGE_PRESENCE_TTL_SECONDS = 60.0

#: Matches the ledger's ``allocated_worker_image`` default so the recorded
#: image and the executed image cannot drift apart.
DEFAULT_IMAGE = "paa/base_worker:v4.1"

#: Where the workspace is mounted **inside the container**. It is not the path
#: the runtime knows the workspace by — that is a path on the host — so
#: anything the container is told about its own filesystem has to use this.
_CONTAINER_WORKSPACE = Path("/workspace")

#: The uid:gid a container runs as when nothing better is known.
#:
#: It matches every image this repository ships (all of them declare
#: ``USER paa``, uid 10001), so it is the right answer for a workspace the
#: runtime could not stat. It is a *fallback*, not a default: the workspace's
#: own owner wins whenever it is known.
_FALLBACK_CONTAINER_USER = "10001:10001"

#: The shape of a ``--user`` value Docker can resolve: a name or a uid, with
#: an optional gid after a colon. Anything else is refused rather than passed
#: through to the daemon.
_USER_SPEC_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(:[A-Za-z0-9_][A-Za-z0-9_.-]*)?$")


def _is_root_user(user: str) -> bool:
    """Whether an image's declared ``USER`` names root.

    Docker's ``Config.User`` is a string that may be a name (``paa``), a uid
    (``10001``), a ``uid:gid`` pair, or empty. Only the empty string and the
    explicit root spellings mean "this image runs as root"; every other value
    is some non-root identity, and Docker resolves it against the *image's*
    ``/etc/passwd`` when it starts the container.

    The uid is compared **numerically**, because Docker parses it that way:
    ``--user 00`` is uid 0, so a literal string comparison against ``"0"``
    would wave container-root through. The comparison is on the uid part
    only, so ``0:1000`` is root as well — the gid does not make it less
    root.
    """
    value = user.strip()
    if not value:
        return True
    uid = value.split(":", 1)[0].strip()
    if uid.isdigit():
        return int(uid) == 0
    return uid.lower() == "root"


def _is_safe_user_spec(user: str) -> bool:
    """Whether a ``--user`` value is shaped like something Docker can resolve.

    The value comes from the daemon, so it is not attacker-controlled — but
    it is *unvalidated*, and an unexpected string would be handed to
    ``docker run`` verbatim. A conservative shape check turns "the daemon said
    something unexpected" into "use the known-good fallback" instead.
    """
    return bool(_USER_SPEC_PATTERN.match(user.strip()))


def _docker_timestamp(raw: object) -> float | None:
    """A Docker timestamp as epoch seconds, or ``None`` if it is not one.

    The daemon reports RFC 3339 with nanosecond precision
    (``2026-10-21T07:30:00.123456789Z``) and uses the **zero time**
    (``0001-01-01T00:00:00Z``) for "this never happened".

    The zero time is not a moment in history — it is the year 1 — so it is
    refused rather than turned into a number. A deadline computed from it
    would land in the past and kill every container the instant it started,
    and the failure would read like a workload problem.

    ``None`` also means "the daemon did not say", and the caller degrades to
    the client's own clock rather than failing a container that is already
    running.
    """
    if not isinstance(raw, str) or not raw or raw.startswith("0001-01-01"):
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _parse_exit_code(raw: bytes) -> int | None:
    """The exit code ``docker wait`` printed, or ``None`` if it printed none.

    ``docker wait`` writes the container's exit code and nothing else, so the
    payload is one number. A payload that is not a number means the daemon
    said something else entirely, and inventing a code from it would put a
    lie in the ledger.
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True)
class _ContainerRun:
    """What one container lifecycle produced.

    A separate type from :class:`~paa.sandbox.base.SandboxResult` because
    this one is the **backend's** view — the raw bytes, the daemon's exit
    code, the start's duration — and the result is the ledger's view of it.
    Keeping them apart is what lets the lifecycle be written as a linear
    protocol (create, start, wait, collect, remove) without every step
    having to know about the result's shape.
    """

    exit_code: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    killed_reason: str | None = None
    start_duration_ms: float = 0.0


class DockerSandbox(Sandbox):
    """Run a command in a throwaway container.

    Every container is **removed** when its run ends — the filesystem layer
    is destroyed, so a workload cannot persist state between runs except
    through the explicitly mounted workspace. That property is what makes
    retries clean.

    The removal is an explicit ``docker rm -f`` rather than ``--rm``,
    because the container's logs have to still be there after it exits
    (ADR-0031), and because a removal in a ``finally`` cannot be skipped by
    a path that raised.
    """

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        image: str = DEFAULT_IMAGE,
        runtime: str | None = None,
        prefer_gvisor: bool | None = None,
    ) -> None:
        self._settings = settings or SandboxSettings()
        self._image = image
        # The OCI runtime to request via ``--runtime``. Comes from
        # ``SandboxSettings.container_runtime`` (default ``runc``) unless an
        # explicit override is passed. ``runsc`` selects gVisor (RFC §13).
        self._runtime = runtime if runtime is not None else self._settings.container_runtime
        # ``prefer_gvisor`` is retained for backward compatibility; when unset it
        # is derived from the configured runtime, so selecting ``runsc`` is the
        # single source of truth rather than two flags that can disagree.
        self._prefer_gvisor = (
            self._runtime == "runsc" if prefer_gvisor is None else prefer_gvisor
        )
        self._healthy: bool | None = None
        #: Whether the daemon reports the *configured* runtime is registered.
        self._runtime_available: bool | None = None
        #: Why the runtime could not be determined, when it could not be.
        #:
        #: ``_runtime_available`` being ``False`` means "the daemon answered
        #: and does not have it". A probe that *failed* is a different fact,
        #: and reporting it as "not registered" accuses the operator of a
        #: misconfiguration they may not have — so it gets its own field and
        #: the doctor says "could not determine" instead of "not there".
        self._runtime_probe_error: str | None = None
        #: Cached answer to "does the daemon have our image?", and when it was
        #: taken. The TUI re-renders on every command; one ``docker image
        #: inspect`` per keystroke is a cost with no benefit.
        self._image_present_at: float | None = None
        self._image_present_value: bool | None = None
        #: The image's declared ``USER``, as the daemon reports it. Read once
        #: per backend instance; it is the fallback identity when the
        #: workspace's owner cannot be used. ``None`` means "not read yet".
        self._image_user: str | None = None

    async def _exec(self, *args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        """Spawn a subprocess. The single seam for every ``docker`` call.

        Every daemon interaction in this backend goes through here, so there
        is exactly one place to look when asking "what does this talk to the
        daemon about", and exactly one place a test has to intercept. The
        alternative — patching ``asyncio.create_subprocess_exec`` — patches
        the *global* asyncio module, so a test that fakes the daemon also
        silently fakes it for every other coroutine running in the process.
        """
        return await asyncio.create_subprocess_exec(*args, **kwargs)

    async def _capture(
        self,
        *args: str,
        stderr: int = asyncio.subprocess.DEVNULL,
    ) -> tuple[int, bytes, bytes] | None:
        """Run a command, capture its output, and never leave the child running.

        ``None`` means "could not run it, or it overran its timeout" — a
        probe that *failed* and a command that *answered* are different
        facts, and the caller decides which one it is looking at.

        The spawn and the wait are separated deliberately. Combining them in
        one ``try`` is how a spawn failure ends up trying to reap a process
        that was never created — and that is not a hypothetical: it raised
        ``UnboundLocalError`` out of the health check, and because the
        health check's failure is swallowed by the backend factory, a
        containment bug silently downgraded the sandbox from ``NAMESPACE``
        to ``PROCESS``. A bug in the containment path must never look like
        a weaker configuration choice.
        """
        try:
            proc = await self._exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
        except OSError:
            return None
        try:
            stdout, err = await asyncio.wait_for(
                proc.communicate(), timeout=_HEALTHCHECK_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # The child is still running. Kill it: a probe that outlives its
            # timeout leaves a process behind and holds the event loop's
            # transport table open.
            await self._reap(proc)
            return None
        except asyncio.CancelledError:
            # The wait was cancelled from the outside — the boot sequence
            # gave up on the probe. The child is *still* running, and the
            # cancellation is the one path that arrives with no timeout of
            # its own to reap it, so it has to be handled explicitly.
            # Without this, every boot against a wedged daemon leaked a
            # `docker version` process.
            await self._reap(proc)
            raise
        return proc.returncode if proc.returncode is not None else -1, stdout, err

    def host_path(self, path: Path | str) -> Path:
        """Translate a path this process knows into one the *host* knows.

        The Docker daemon resolves a bind mount's source against the host
        filesystem. When the runtime itself is containerised, the path it
        holds is not the path the daemon must be given — and passing the
        wrong one does not fail loudly: ``docker run -v /data/x:/workspace``
        against a host that has no ``/data`` silently mounts a *different*,
        empty, root-owned directory.

        The longest matching prefix in
        :attr:`SandboxSettings.host_path_pairs` wins. No map means the paths
        are the same, which is the correct answer when the runtime is not
        containerised.

        A malformed map raises :class:`SandboxError` rather than quietly
        skipping the entry. Quietly skipping it is the *worse* failure: the
        daemon is then handed the runtime's own path, and answers by creating
        an empty root-owned directory in its place. The user asked for a
        translation and did not get one, and the symptom is a mount that
        looks fine and contains nothing.
        """
        raw = posixpath.normpath(str(path))
        try:
            pairs = self._settings.host_path_pairs
        except ValueError as exc:
            raise SandboxError(
                f"host path translation is misconfigured: {exc}",
                hint="fix PAA_SANDBOX__HOST_PATH_MAP (inside=host, comma-separated)",
            ) from exc

        best_inside: str | None = None
        best_host = ""
        best_suffix = raw
        for inside, host in pairs:
            if inside == "/":
                # A root mapping covers everything, and the suffix is the
                # whole path — there is no prefix to strip.
                suffix = raw
            elif raw == inside or raw.startswith(inside + "/"):
                suffix = raw[len(inside) :]
            else:
                continue
            if best_inside is None or len(inside) > len(best_inside):
                best_inside = inside
                best_host = host
                best_suffix = suffix
        if best_inside is None:
            return Path(raw)
        return Path(best_host + best_suffix)

    @property
    def name(self) -> str:
        return "docker"

    @property
    def isolation_level(self) -> IsolationLevel:
        """VM only when gVisor (``runsc``) is confirmed present, else NAMESPACE.

        Reported optimistically *before* a probe has run (``None`` state) as
        NAMESPACE — understating containment is safe, overstating it is not. A
        non-gVisor custom runtime is not assumed to interpose on syscalls, so it
        stays at NAMESPACE regardless.
        """
        if self._runtime == "runsc" and self._runtime_available:
            return IsolationLevel.VM
        return IsolationLevel.NAMESPACE

    @property
    def runtime(self) -> str:
        return self._runtime

    @property
    def runtime_available(self) -> bool | None:
        """Whether the daemon reports the *configured* runtime as registered.

        ``None`` means **not probed yet**, which is a third state and not a
        "no". A backend that has never asked the daemon cannot claim the
        runtime is missing — it can only say it does not know. Collapsing the
        two would make ``paa doctor`` report a runtime as absent on a host
        where nobody had bothered to look, which is a false accusation of a
        misconfiguration.
        """
        return self._runtime_available

    @property
    def image(self) -> str:
        return self._image

    def describe(self) -> dict[str, Any]:
        """Backend identity, plus the container facts that decide containment.

        The base implementation reports the backend and the isolation level.
        This override adds the three facts an operator cannot otherwise see:
        which OCI runtime was requested, whether the daemon actually has it,
        and which image the container runs. ``runtime_available`` is ``None``
        when the daemon has not been asked yet — see :attr:`runtime_available`.

        A misconfigured host-path translation is reported here too. It cannot
        be reported by *raising*: a diagnostic that throws is a diagnostic
        that lies, and the host with the broken map is exactly the host whose
        operator needs the answer.
        """
        described = super().describe()
        described.update(
            {
                "container_runtime": self._runtime,
                "runtime_available": self._runtime_available,
                "image": self._image,
                # This backend is the one that *has* a start phase, so it is
                # the one that spends the grace. The other backends report
                # zero and say why: a setting only one backend honours must
                # not be silently applied to the others (ADR-0031).
                "container_start_grace_seconds": self._settings.container_start_grace_seconds,
                "container_start_budget": "separate from the work budget",
            }
        )
        map_error = self._settings.host_path_map_error
        if map_error is not None:
            described["host_path_map_error"] = map_error
        return described

    async def diagnose(self) -> dict[str, Any]:
        """Ask the daemon what is actually true, and report it.

        Unlike :meth:`healthcheck`, this is allowed to be slow: it is a
        diagnostic command, and "how long did the daemon take to answer" is
        not the operator's question. "Is the runtime I asked for the runtime
        I am getting, and does the image I named exist" is.

        Never raises. A ``paa doctor`` that crashes on the one host where the
        backend is broken is exactly the run whose output is needed.
        """
        facts = self.describe()
        if shutil.which("docker") is None:
            facts["daemon"] = "no docker CLI on PATH"
            return facts

        try:
            healthy = await self.healthcheck()
        except Exception as exc:  # pragma: no cover - healthcheck never raises
            facts["daemon"] = f"probe failed: {exc}"
            return facts

        # Re-read the facts *after* the probe. ``describe()`` is a snapshot of
        # what the backend currently knows, and the probe is what teaches it
        # whether the requested runtime is registered — a snapshot taken
        # before the probe is the stale one, and it is exactly the snapshot
        # that makes a diagnostic contradict itself.
        facts = self.describe()
        if not healthy:
            facts["daemon"] = "unreachable"
            # The image is NOT probed when the daemon did not answer.
            # `docker image inspect` exits non-zero in that case too, so
            # "absent" and "the daemon is unreachable" are the same exit
            # code — and reporting "NOT present on the daemon" next to
            # "unreachable" would send the operator to rebuild an image
            # that exists, for a failure caused by the socket.
            #
            # Leaving the fact unset means "could not tell", which is what
            # it is. The renderer shows no suffix for that, and the row
            # above it already says the daemon is unreachable.
            return facts
        elif self._runtime_available is False:
            # The daemon answered, but does not have the runtime we asked for.
            # This is the case that matters: the sandbox will run under runc
            # and the isolation level is NAMESPACE, not VM.
            facts["daemon"] = f"reachable, but '{self._runtime}' is not registered"
        elif self._runtime_available is None:
            # The probe itself failed. Saying "not registered" here would
            # accuse the operator of a misconfiguration they may not have.
            facts["daemon"] = (
                f"reachable, but the runtime probe failed "
                f"({self._runtime_probe_error or 'unknown reason'})"
            )
        else:
            facts["daemon"] = "reachable"

        facts["image_present"] = await self._image_present()
        return facts

    async def _exists(self, kind: str, name: str) -> bool | None:
        """Ask the daemon whether a named object exists.

        ``None`` means the probe itself failed — the daemon is unreachable,
        or the query did not finish in time. That is a different answer from
        "absent", and the two demand different responses from a caller, so
        they are not collapsed here.
        """
        result = await self._capture("docker", kind, "inspect", name)
        if result is None:
            return None
        return result[0] == 0

    async def _network_exists(self, name: str) -> bool:
        """Whether the daemon has a network with this name.

        A container asked to join a network that does not exist fails with
        Docker's own "network X not found" — an error that arrives after the
        container is created and reads like a workload failure rather than a
        configuration mistake. The check is one call and turns the second
        into the first.
        """
        exists = await self._exists("network", name)
        # Cannot tell: let Docker produce the real error.
        return True if exists is None else exists

    async def _image_present(self) -> bool | None:
        """Whether the daemon has the image the sandbox would run.

        The image name is a *string* in four places — the backend's default,
        the ledger's ``allocated_worker_image`` default, and the two SQL
        schemas — and until Phase 0 the string named an image nobody had
        built, so every run failed with "Unable to find image". Asking the
        daemon is one call and turns a silent lie into a visible fact.

        ``None`` means "could not tell" (the daemon is unreachable, or the
        query failed), which is different from "the image is absent".

        The answer is cached briefly: the TUI re-renders its dashboard on
        every command, and one ``docker image inspect`` per keystroke is a
        cost with no benefit. The TTL is short enough that an image built
        after the first probe still shows up.
        """
        now = time.monotonic()
        if (
            self._image_present_at is not None
            and now - self._image_present_at < _IMAGE_PRESENCE_TTL_SECONDS
        ):
            return self._image_present_value
        value = await self._exists("image", self._image)
        self._image_present_at = now
        self._image_present_value = value
        return value

    async def healthcheck(self) -> bool:
        """Whether the Docker CLI exists and a daemon answers. Never raises.

        Checks the *daemon*, not just the CLI: ``docker`` on PATH with Docker
        Desktop stopped is the single most common state on a Windows dev
        machine, and a CLI-only check would route work to a backend that
        cannot start a container.
        """
        if self._healthy is not None:
            return self._healthy
        self._healthy = await self._probe()
        return self._healthy

    async def _probe(self) -> bool:
        if shutil.which("docker") is None:
            log.debug("sandbox.docker.unavailable", reason="docker CLI not on PATH")
            return False
        result = await self._capture(
            "docker",
            "version",
            "--format",
            "{{.Server.Version}}",
            stderr=asyncio.subprocess.PIPE,
        )
        if result is None:
            log.debug("sandbox.docker.unavailable", reason="the daemon probe failed")
            return False
        returncode, stdout, stderr = result
        if returncode != 0:
            log.debug(
                "sandbox.docker.daemon_unreachable",
                stderr=stderr.decode("utf-8", errors="replace").strip()[:200],
            )
            return False

        log.info(
            "sandbox.docker.available",
            server_version=stdout.decode("utf-8", errors="replace").strip(),
        )
        await self._detect_runtime()
        await self._detect_image_user()
        return True

    async def _detect_image_user(self) -> None:
        """Read the image's declared ``USER`` from the daemon.

        It is the fallback identity for a container whose workspace owner
        cannot be used — a root-owned workspace, or one that could not be
        stat'ed. Docker resolves the value against the *image's*
        ``/etc/passwd``, so a name like ``paa`` is passed through unchanged.

        A failure is not an error: the daemon may not have the image yet, and
        the fallback chain already ends at a safe non-root uid.
        """
        if self._image_user is not None:
            return
        result = await self._capture(
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Config.User}}",
            self._image,
        )
        if result is None:
            return
        returncode, stdout, _ = result
        if returncode != 0:
            return
        declared = stdout.decode("utf-8", errors="replace").strip()
        if declared:
            self._image_user = declared
            log.info("sandbox.docker.image_user", user=declared, image=self._image)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process) -> None:
        """Kill a subprocess that overran its timeout, and wait for it."""
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT_SECONDS)

    async def _detect_runtime(self) -> bool | None:
        """Ask the daemon whether the *configured* runtime is registered.

        Stock ``runc`` is always present, so no probe is needed for it. For any
        other runtime (``runsc``, or a bespoke one) the daemon's registered
        runtimes are queried; the result gates both ``--runtime`` selection and
        the honest :attr:`isolation_level`. A configured runtime the daemon does
        not know about is a loud warning, never a silent overstatement of
        containment.

        The three outcomes are kept apart, because they demand different
        responses from an operator:

        * the daemon answered and **has** the runtime → ``True``;
        * the daemon answered and does **not** have it → ``False``, and the
          sandbox will run under runc with NAMESPACE isolation;
        * the probe **failed** → ``None`` and
          :attr:`_runtime_probe_error` says why. Reporting a failed probe as
          "not registered" would send an operator to reinstall a runtime that
          is installed and working.

        The return value is that same tri-state, so a caller that reads it
        cannot mistake "could not determine" for "not there".
        """
        if self._runtime_available is not None:
            return self._runtime_available
        if self._runtime == "runc":
            self._runtime_available = True
            return True

        self._runtime_available = False
        self._runtime_probe_error = None
        result = await self._capture("docker", "info", "--format", "{{json .Runtimes}}")
        if result is None:
            self._runtime_available = None
            self._runtime_probe_error = "`docker info` did not answer in time"
        else:
            returncode, stdout, _ = result
            if returncode == 0:
                try:
                    runtimes = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
                except json.JSONDecodeError as exc:
                    self._runtime_available = None
                    self._runtime_probe_error = str(exc)
                else:
                    self._runtime_available = self._runtime in runtimes
            else:
                # The daemon answered, but not usefully. That is a failed
                # probe, not an answer.
                self._runtime_available = None
                self._runtime_probe_error = f"`docker info` exited {returncode}"

        if self._runtime_available is None:
            log.warning(
                "sandbox.docker.runtime_probe_failed",
                requested=self._runtime,
                detail=self._runtime_probe_error,
            )
        elif not self._runtime_available:
            log.warning(
                "sandbox.docker.runtime_unavailable",
                requested=self._runtime,
                detail="daemon does not report this runtime; falling back to runc "
                "with NAMESPACE isolation. See docs/deployment/docker.md.",
            )
        log.info(
            "sandbox.docker.runtime", requested=self._runtime, available=self._runtime_available
        )
        return self._runtime_available

    def _build_args(self, spec: SandboxSpec, workspace: Any, container_name: str) -> list[str]:
        """Assemble the full ``docker create`` argv.

        ``create`` and not ``run``. ``docker run`` creates, starts, attaches,
        waits and tears down inside **one** opaque call, so the backend
        cannot see where the start ends and the work begins — and a budget it
        cannot see a boundary in, it cannot separate. That opacity is the
        whole reason the start used to be charged to the work (ADR-0031).

        ``--rm`` is deliberately **absent**. The container's logs have to
        still be there after it exits, and ``--rm`` destroys them with the
        container. The removal is an explicit ``docker rm -f`` in a
        ``finally`` instead, which is also the only version of it that
        cannot leave an orphan behind: a container that is still running
        holds the workspace bind mount, which is precisely the orphan the
        timeout exists to prevent.

        The containment flags live in :meth:`_container_flags`, so they are
        the same flags whichever way the container is created, and they
        cannot drift apart from the ones the tests assert on.
        """
        args = ["docker", "create"]
        args += self._container_flags(spec, workspace, container_name)
        args.append(self._image)
        args.extend(spec.command)
        return args

    def _container_flags(
        self, spec: SandboxSpec, workspace: Any, container_name: str
    ) -> list[str]:
        """The flags every container is created with.

        Each flag maps to a specific RFC §13 requirement:

        ``--network=none``
            Real egress severance — no interfaces but loopback exist in the
            container's netns. This is the guarantee the subprocess backend
            documents that it *cannot* make.
        ``--read-only``
            Root filesystem is immutable; only the workspace bind mount and
            the tmpfs are writable, so a workload cannot tamper with its own
            interpreter or libraries mid-run.
        ``--tmpfs /tmp``
            A writable scratch area that is RAM-backed and size-capped, so
            ``--read-only`` does not break every well-behaved program while
            still bounding scratch growth.
        ``--memory`` / ``--memory-swap``
            cgroup memory cap. ``--memory-swap`` is pinned equal to
            ``--memory`` deliberately: left unset Docker grants swap equal to
            memory, silently doubling the effective ceiling and making the
            limit a suggestion.
        ``--cpus``
            CFS quota.
        ``--pids-limit``
            Fork-bomb containment. Without it a workload can exhaust host PIDs
            while staying comfortably inside its memory budget.
        ``--cap-drop=ALL`` / ``--security-opt=no-new-privileges``
            No capabilities, and no way to regain any via setuid binaries.
        ``--user``
            The container runs as the uid/gid that *owns the workspace*, not
            as root. A container-root process can create root-owned files in
            the user's workspace, which the user then cannot delete without
            sudo. Running as the owner also means the container can actually
            write the one directory it was given. The flag is always passed
            and the uid is never 0 — see :meth:`_container_user`.
        ``--network``
            Explicit, never implicit. With the network denied it is ``none``;
            with it allowed it is the configured network, or — if none is
            configured — the daemon's default bridge, which has full host and
            internet reachability. That fallback is logged, because it is the
            difference between "no egress" and "all egress".
        ``--runtime``
            The OCI runtime, and only when the daemon actually has it: the
            isolation level follows the **effective** runtime, so a host that
            asked for gVisor and fell back to runc records NAMESPACE rather
            than claiming a syscall boundary it does not have.
        """
        args = [
            "--name",
            container_name,
            "--workdir",
            "/workspace",
            "--read-only",
            "--tmpfs",
            # A container-internal path, not a host one: this string is
            # interpreted by the container runtime inside the mount
            # namespace, so the host /tmp is never touched.
            "/tmp:rw,noexec,nosuid,size=64m",  # noqa: S108
            "--memory",
            f"{spec.memory_mb}m",
            "--memory-swap",
            f"{spec.memory_mb}m",
            "--cpus",
            f"{spec.cpu_cores}",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
        ]

        # Exec-capable scratch, for runtimes that must execute what they
        # unpack (npm's cache holds the shims `npx` installs). Separate from
        # /tmp so the noexec property there survives.
        #
        # mode=1777 because the tmpfs is created by the daemon and therefore
        # owned by root: without it the non-root container user cannot create
        # its cache directory inside the mount, and npm fails with EACCES
        # rather than with anything that names the real problem.
        for path in spec.exec_tmpfs:
            args += ["--tmpfs", f"{path}:rw,exec,nosuid,mode=1777,size=64m"]

        # Request a non-default runtime only when the daemon actually has it.
        # ``--runtime=runsc`` is the RFC §13 containment; a bespoke runtime name
        # is honoured identically. Stock runc needs no flag.
        if self._runtime != "runc" and self._runtime_available:
            args += ["--runtime", self._runtime]

        network = self._effective_network(spec)
        if network == "none":
            args += ["--network", "none"]
        elif network:
            args += ["--network", network]
        else:
            # The default bridge reaches the host and the internet. Say so
            # rather than let "allow_network" imply more than it delivers.
            log.warning(
                "sandbox.docker.default_bridge_network",
                detail=(
                    "allow_network=True with no configured network puts the "
                    "container on the Docker default bridge, which has full "
                    "host and internet reachability. Set "
                    "SandboxSettings.default_container_network or "
                    "SandboxSpec.network to a narrower one."
                ),
            )
        # The container runs as the workspace's owner, so it can write the one
        # directory it was given and cannot leave root-owned files behind.
        # The flag is always present: omitting it means "whatever the image
        # says", and an image that says nothing runs as root.
        args += ["--user", self._container_user(workspace)]

        # The daemon resolves bind-mount sources against the HOST filesystem,
        # so a workspace path the runtime knows is not necessarily the path the
        # daemon must be given.
        args += ["--volume", f"{self.host_path(workspace)}:/workspace:rw"]
        for index, mount in enumerate(spec.read_only_mounts):
            args += ["--volume", f"{self.host_path(mount.resolve())}:/mnt/ro{index}:ro"]

        # Env is passed as explicit -e pairs built from the allowlist. Docker
        # does not inherit the host environment, so this is the only channel —
        # which makes the allowlist airtight on this backend.
        #
        # The workspace is at /workspace *inside the container*, which is not
        # the path the runtime knows it by. HOME and TMPDIR have to name the
        # container's path or they point at a directory that does not exist
        # there, and a tool that writes to "its home" writes nowhere.
        for key, value in sorted(
            build_child_env(spec, workspace=_CONTAINER_WORKSPACE).items()
        ):
            if key in {"SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP"}:
                continue  # Windows-only bootstrap keys are meaningless in Linux
            args += ["--env", f"{key}={value}"]

        return args

    @staticmethod
    def _workspace_owner(workspace: Path) -> tuple[int, int] | None:
        """The uid/gid that owns the workspace, or ``None`` if it cannot be read.

        This is the *filesystem's* answer, and nothing more: the decision about
        what the container should run as belongs to :meth:`_container_user`,
        which is where the "never root" rule lives.
        """
        try:
            stat = workspace.stat()
        except OSError:
            return None
        return stat.st_uid, stat.st_gid

    def _container_user(self, workspace: Path) -> str:
        """The ``--user`` the container runs as. **Never uid 0.**

        The rule is an invariant, not a preference: a container that runs as
        root inside a gVisor sandbox is still a root process with a writable
        workspace, and the whole point of ``--user`` is that the workload
        cannot leave root-owned files behind or gain capabilities the daemon
        did not intend to hand it. So the flag is **always** passed — never
        omitted — because omitting it means "whatever the image says", and an
        image that says nothing runs as root.

        The order of preference:

        1. the workspace's owner, when it is known and not root — the
           directory the container must be able to write;
        2. the image's own declared user, when the image declares one that
           is not root — Docker resolves names against the image's
           ``/etc/passwd``, so ``paa`` works;
        3. :data:`_FALLBACK_CONTAINER_USER`, the uid every image this
           repository ships uses.

        A **root-owned workspace** is refused at step 1. It is the case that
        looks like a shortcut and is not: ``--user 0:0`` would put the
        workload back in container-root, and the exact harm the flag exists to
        prevent — root-owned files in a directory the operator owns — is what
        would happen. A workload that cannot write a root-owned directory
        fails with a legible error; a workload running as root does not fail
        at all, and leaves the mess behind.
        """
        owner = self._workspace_owner(workspace)
        if owner is not None and owner[0] != 0:
            return f"{owner[0]}:{owner[1]}"
        if owner is not None:
            log.warning(
                "sandbox.docker.root_owned_workspace",
                detail=(
                    "the workspace is owned by root; the container will not run "
                    "as root, so it falls back to the image's own user. chown "
                    "the workspace to the account that runs the agent if it "
                    "must be writable."
                ),
            )
        image_user = self._image_user
        if image_user and not _is_root_user(image_user) and _is_safe_user_spec(image_user):
            return image_user
        return _FALLBACK_CONTAINER_USER

    def _memory_enforcement(self) -> str:
        """How the memory ceiling is actually enforced by this backend.

        **By the kernel's cgroup, under either runtime.** ``--memory`` is a
        *daemon*-level limit: the daemon puts the container in a cgroup and
        caps that cgroup, and it refuses to start a container whose limit it
        cannot apply. The OCI runtime in charge does not change that — and
        gVisor does not get an exception to it.

        That was measured, not assumed. On this host, with ``--memory 256m``
        and a process that touches 512 MiB:

        ==================  ==========  ========  =========================
        runtime            exit code   OOMKilled  cgroup ``memory.events``
        ==================  ==========  ========  =========================
        ``runc``           137         true      ``oom 1 oom_kill 1``
        ``runsc`` (gVisor) 137         true      ``oom 1 oom_kill 1``
        ==================  ==========  ========  =========================

        and in both cases ``memory.peak`` reached ``memory.max`` exactly. The
        kernel's own ``oom_kill`` counter is what fired, under gVisor as much
        as under runc — so the cap was a hard ceiling in both, and recording
        anything weaker would put a claim in the ledger that the host's own
        accounting contradicts.

        Which *runtime* ran the container is recorded separately, in
        ``isolation_level`` (``VM`` under gVisor, ``NAMESPACE`` under runc),
        so nothing is lost by naming the mechanism here.

        The mechanism follows the **effective** runtime, not the requested
        one: a host that asks for gVisor but has not registered it runs the
        container under runc — with the same cgroup cap, and with the
        isolation level to say so. Keying the *ceiling* on the request would
        put a weaker claim in the ledger on every fallback host, and the
        ledger's isolation row already says which runtime was in charge.
        """
        return "cgroup"

    def _effective_network(self, spec: SandboxSpec) -> str | None:
        """The Docker network the container will actually join.

        Three distinct answers, and the difference between them is the
        difference between "no network" and "every network":

        ``"none"``
            No network at all.
        ``None``
            The Docker daemon's *default bridge*, because a network was
            allowed but none was named. The bridge reaches the host's
            gateway address and the internet, which is more than
            "networked" implies — so it is logged whenever it is chosen.
        a name
            That network.

        Computed in one place, because two callers need the same answer and
        they must not disagree: the argv builder and the existence check. A
        check that looked at ``spec.network`` while the argv builder used
        ``--network none`` would refuse to start a container over a network
        it was never going to join.
        """
        if not spec.allow_network:
            return "none"
        return spec.network or self._settings.default_container_network or None

    @staticmethod
    def _container_name(spec: SandboxSpec) -> str:
        """A name for this run's container: unique, and short enough for Docker.

        The name is a *handle*, and the daemon refuses to create a container
        whose name is taken — so two runs that pick the same name collide, and
        the second one fails to start for a reason that has nothing to do with
        the workload.

        The name used to be ``paa-{task}-{milliseconds}``: a millisecond
        clock, so two runs that began in the same millisecond collided.
        Dropping ``--rm`` (ADR-0031) made the window *wider*, because the
        name is held until the ``docker rm -f`` in the ``finally`` — the
        container outlives the process that created it, so a second run can
        reach the daemon while the first run's name is still taken. A short
        random suffix removes the collision without making the name long
        enough to reach Docker's 63-character limit.
        """
        suffix = uuid.uuid4().hex[:8]
        stem = f"paa-{spec.parent_task_id or 'task'}"
        return f"{stem[: 63 - len(suffix) - 1]}-{suffix}"

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` in a container.

        **Two budgets, enforced independently** (ADR-0031):

        =========================  ===========================================
        ``container_start_grace``  the ``docker create`` and ``docker start``
        ``timeout_seconds``        the container's **lifetime**
        =========================  ===========================================

        The old implementation could not do that, because ``docker run`` is
        one opaque call that creates, starts, attaches, waits and tears down
        inside a single ``communicate()``. The backend could not see where the
        start ended and the work began, so it added the two budgets together
        and gave the sum to one timer. The consequence was measured on this
        host: a step with ``timeout_seconds=0.25`` and a three-second
        workload ran for **3.6 seconds** and was reported as a success with
        ``timed_out=False`` — a quarter-second budget became five and a
        quarter seconds of *work*.

        Now the boundary is observable, because the daemon's own accounting
        is used instead of one opaque call:

        ``docker create`` / ``docker start``
            the start phase, on the start grace. ``docker start`` returns
            once the container's process is running, so the boundary is real
            rather than assumed.
        ``docker inspect``
            ``State.StartedAt`` — the **daemon's** clock, which on this host
            is the host's clock. The work deadline is ``StartedAt +
            timeout_seconds`` in that clock, so the budget does not drift with
            how late the client was.
        ``docker wait``
            the work phase, bounded by that deadline.
        ``docker logs``
            the output, with the two streams kept apart — ``docker logs``
            demultiplexes, exactly as ``docker run`` did.
        ``docker rm -f``
            the teardown, in a ``finally``. ``--rm`` is gone: the logs have
            to still be there after the container exits, and a container that
            is never removed is precisely the orphan the timeout exists to
            prevent.

        **The container's fixed cost is charged to the caller, and that is
        deliberate.** The lifetime includes the runtime's setup and teardown
        — **~200 ms, measured** (a ``sh -c true`` container's lifetime is
        186-225 ms; 114-130 ms on the host this was re-measured on) — which
        is *before* and *after* the workload's own execution. The subprocess
        backend charges its own ~5 ms fork to the caller today; the container
        backend charging its ~200 ms setup is the same rule at a different
        scale. The workload's own duration is not separately observable: the
        daemon reports the lifetime, and the budget bounds that.

        ``duration_ms`` keeps its meaning — the **total** wall clock. It is
        the sandbox's own measurement of the two phases together, and a
        total that silently stops counting the start is not a total; the
        skill-ranking signal is a different field of the same name (the run
        record's own wall clock). The start is reported separately, in
        ``start_duration_ms``.
        """
        if not await self.healthcheck():
            raise SandboxError(
                "docker backend is not available",
                hint="install Docker Desktop or select another backend",
            )

        workspace = resolve_workspace(spec.workspace_path)
        container_name = self._container_name(spec)

        # A named network that does not exist is a configuration mistake, and
        # Docker's own error for it ("network X not found") arrives after the
        # container has been created and read like a workload failure. Say it
        # plainly instead, and before anything runs.
        #
        # The network checked is the one the container will actually join.
        # "none" is not a network that can be missing, the default bridge
        # always exists, and a spec that names a network while the network is
        # denied never joins it.
        named_network = self._effective_network(spec)
        if named_network and named_network != "none" and not await self._network_exists(
            named_network
        ):
            raise SandboxError(
                f"docker network {named_network!r} does not exist",
                hint=(
                    "create it with `docker network create "
                    f"{named_network}` or unset the network setting"
                ),
            )

        started = time.perf_counter()
        outcome = await self._execute(spec, workspace, container_name)
        duration_ms = (time.perf_counter() - started) * 1000.0

        cap = self._settings.max_capture_bytes
        truncated = len(outcome.stdout) > cap or len(outcome.stderr) > cap

        return SandboxResult(
            exit_code=outcome.exit_code,
            stdout=outcome.stdout[:cap].decode("utf-8", errors="replace"),
            stderr=outcome.stderr[:cap].decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            start_duration_ms=outcome.start_duration_ms,
            peak_rss_mb=None,
            timed_out=outcome.timed_out,
            killed_reason=outcome.killed_reason,
            truncated_output=truncated,
            backend=self.name,
            isolation_level=self.isolation_level,
            # The mechanism, not the aspiration. See _memory_enforcement.
            memory_enforcement=self._memory_enforcement(),
        )

    async def _execute(
        self, spec: SandboxSpec, workspace: Path, container_name: str
    ) -> _ContainerRun:
        """The container lifecycle: create, start, wait, collect, remove.

        Split out of :meth:`run` so the protocol reads as a protocol. The
        removal is in a ``finally``, so **every** path out of here — the
        success, the work timeout, the start breach, and an exception raised
        in the middle — removes the container. A container that outlives its
        run holds the workspace bind mount, and the next run cannot mount it
        again.
        """
        grace = self._settings.container_start_grace_seconds
        started = time.perf_counter()

        stdout = b""
        stderr = b""
        exit_code: int | None = None
        timed_out = False
        killed_reason: str | None = None
        start_duration_ms = 0.0

        try:
            # -- the start phase -------------------------------------------------
            # The daemon's work, on the daemon's budget. The create and the
            # start each get the whole grace: they are two calls to a daemon
            # that may be slow, and the grace is the operator's statement
            # about how slow is too slow.
            try:
                create_rc, _, create_err = await self._docker_call(
                    self._build_args(spec, workspace, container_name), grace
                )
            except TimeoutError:
                # The daemon did not answer the create inside the grace. That
                # is a **host** problem, and it is reported as one:
                # "start_timeout", not "timeout". The two demand opposite
                # responses, and a caller that cannot tell them apart retries
                # the workload on a host that cannot start it.
                start_duration_ms = (time.perf_counter() - started) * 1000.0
                killed_reason = "start_timeout"
                log.warning(
                    "sandbox.docker.start_timeout",
                    phase="create",
                    container=container_name,
                    grace_seconds=grace,
                )
                await self._force_kill(container_name)
                exit_code = await self._wait_for_exit(container_name)
            else:
                if create_rc != 0:
                    # The daemon refused to create the container — a missing
                    # image, a bad mount, a runtime that is not registered.
                    # That is the daemon's own error and it is passed on
                    # verbatim, because "Unable to find image" names the
                    # problem and "the step failed" does not.
                    return _ContainerRun(
                        exit_code=create_rc,
                        stderr=create_err,
                        start_duration_ms=(time.perf_counter() - started) * 1000.0,
                    )

                try:
                    start_rc, _, start_err = await self._docker_call(
                        ["docker", "start", container_name], grace
                    )
                except TimeoutError:
                    start_duration_ms = (time.perf_counter() - started) * 1000.0
                    killed_reason = "start_timeout"
                    log.warning(
                        "sandbox.docker.start_timeout",
                        phase="start",
                        container=container_name,
                        grace_seconds=grace,
                    )
                    # The container may be running: the *client* gave up, not
                    # the daemon. It has to die, or it holds the workspace
                    # mount.
                    await self._force_kill(container_name)
                    exit_code = await self._wait_for_exit(container_name)
                else:
                    start_duration_ms = (time.perf_counter() - started) * 1000.0
                    # The client's own observation of the container's start:
                    # ``docker start`` returns once the container's process is
                    # running. It is the anchor the deadline falls back to
                    # when the daemon does not say when it started — and it
                    # is a *client* clock, so it is only ever the fallback.
                    work_started_at = time.time()
                    if start_rc != 0:
                        return _ContainerRun(
                            exit_code=start_rc,
                            stderr=start_err,
                            start_duration_ms=start_duration_ms,
                        )

                    # -- the work phase ------------------------------------------
                    # The daemon's clock decides the deadline, so the budget
                    # does not depend on how punctually the client got here.
                    state = await self._inspect_state(container_name, grace)
                    started_at = _docker_timestamp(state.get("StartedAt"))
                    if started_at is None:
                        log.warning(
                            "sandbox.docker.no_started_at",
                            container=container_name,
                            detail="the daemon did not report a start time; "
                            "the client's own start-of-work clock is used for "
                            "the deadline instead",
                        )
                    budget = self._work_budget(spec, started_at, work_started_at)
                    if budget == 0.0:
                        # The budget is spent before the wait even begins.
                        # The usual cause is a **clock**: the deadline is
                        # ``StartedAt + timeout`` in the daemon's clock, and a
                        # daemon whose clock runs behind the host's puts the
                        # deadline in the past. It errs the safe way — the
                        # container is killed rather than overrunning — but a
                        # work timeout with no hint about the clock sends
                        # the operator looking at the workload when the
                        # workload is innocent.
                        log.warning(
                            "sandbox.docker.work_budget_exhausted",
                            container=container_name,
                            timeout_seconds=spec.timeout_seconds,
                            container_running=bool(state.get("Running")),
                            daemon_started_at=started_at,
                            client_started_at=round(work_started_at, 3),
                            # Positive when the daemon's clock is *behind* the
                            # host's — the lag between the two.
                            daemon_clock_lag_seconds=(
                                round(work_started_at - started_at, 3)
                                if started_at is not None
                                else None
                            ),
                            detail=(
                                "the work budget was already spent when the wait "
                                "began; the deadline is StartedAt + timeout in the "
                                "daemon's clock, and a daemon whose clock runs "
                                "behind the host's shortens the budget"
                            ),
                        )

                    try:
                        wait_rc, wait_out, _ = await self._docker_call(
                            ["docker", "wait", container_name], budget
                        )
                    except TimeoutError:
                        # The work overran its budget. The container is
                        # killed, not the client: killing the client leaves
                        # the container running and holding the workspace
                        # bind mount, which is precisely the orphan the
                        # timeout exists to prevent.
                        timed_out = True
                        killed_reason = "timeout"
                        await self._force_kill(container_name)
                        exit_code = await self._wait_for_exit(container_name)
                        log.warning(
                            "sandbox.docker.timeout",
                            container=container_name,
                            timeout_seconds=spec.timeout_seconds,
                        )
                    else:
                        if wait_rc == 0:
                            exit_code = _parse_exit_code(wait_out)

            # -- the collection --------------------------------------------------
            # The logs are read after the container has stopped, because the
            # removal that follows destroys the container's filesystem layer
            # and the logs are the only record of what it did.
            stdout, stderr = await self._collect_logs(container_name)
            final_state = await self._inspect_state(
                container_name, _DAEMON_QUERY_TIMEOUT_SECONDS
            )
            if exit_code is None:
                exit_code = self._state_exit_code(final_state)

            # The **start** timeout is checked first. A container killed by
            # the start grace also exits 137 — the same code the cgroup OOM
            # killer produces — and reporting that as "memory" would send
            # the orchestrator to retry at a higher modality when the real
            # problem is a host that cannot start a container in time.
            if killed_reason != "start_timeout" and exit_code == 137 and not timed_out:
                killed_reason = "memory"

            log.info(
                "sandbox.docker.completed",
                container=container_name,
                exit_code=exit_code,
                timed_out=timed_out,
                killed_reason=killed_reason,
                start_duration_ms=round(start_duration_ms, 3),
                # The container's lifetime as the *daemon* measured it — the
                # span the work budget bounds, and not the same as the wall
                # clock: it includes the runtime's setup and teardown.
                lifetime_ms=round(self._lifetime_ms(final_state) or 0.0, 3),
            )

            return _ContainerRun(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                killed_reason=killed_reason,
                start_duration_ms=start_duration_ms,
            )
        finally:
            await self._remove_container(container_name)

    # -- the daemon, one call at a time ---------------------------------------

    async def _docker_call(
        self, args: list[str], budget_seconds: float | None
    ) -> tuple[int, bytes, bytes]:
        """Run one ``docker`` command and capture its output.

        The single place the run path talks to the daemon, so the budget
        each call gets is visible in one place — and the budget is the whole
        point of this backend (ADR-0031): the start calls get the start
        grace, the wait gets the work budget, and **nothing** gets the sum.

        A timeout raises :class:`TimeoutError` *after* the client process has
        been reaped, so a call that overruns leaves no ``docker`` client
        behind holding a pipe open. The container itself is not touched
        here: the client going away is not the container going away, and the
        two are killed in different ways.
        """
        try:
            proc = await self._exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SandboxError("failed to launch docker", reason=str(exc)) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=budget_seconds
            )
        except TimeoutError:
            await self._reap(proc)
            raise
        except asyncio.CancelledError:
            # The wait was cancelled from the outside. The child is still
            # running and the cancellation is the one path that arrives with
            # no timeout of its own to reap it.
            await self._reap(proc)
            raise
        return (proc.returncode if proc.returncode is not None else -1), stdout, stderr

    async def _inspect_state(
        self, container_name: str, budget_seconds: float | None
    ) -> dict[str, Any]:
        """The daemon's own view of the container's state.

        ``State.StartedAt`` is the boundary between the start and the work,
        and it is in the **daemon's** clock. The daemon runs on the same
        host, so that clock is the host's clock, and the work deadline can
        be computed from it — a deadline computed from the client's clock
        would drift by however late the client was, and a deadline that
        drifts is a deadline the caller cannot rely on.

        An empty mapping means "the daemon did not answer, or answered
        something unreadable". The caller degrades to the client's clock
        rather than failing the run: a container that is already running is
        better than no container at all.

        A daemon that cannot be reached at all degrades the same way. The
        query is *reporting*, and a report that cannot be made must not
        destroy the work it was reporting on.

        A daemon that does not answer **in time** degrades the same way
        too, and this is the case that is easy to forget: ``_docker_call``
        raises :class:`TimeoutError` whenever the daemon does not answer
        inside the call's budget, and the pre-work inspect's budget *is* the
        start grace — so an operator who tightens the grace to fail fast on
        a slow host **raises** the probability of hitting it. Letting that
        escape would kill a container that was running perfectly well, on
        the grounds that the backend could not report on it, and would raise
        a bare :class:`TimeoutError` — which is not a
        :class:`~paa.core.errors.SandboxError`, and so is not on the
        failure surface the callers are documented to handle. A run that was
        killed still produces a result, because the ledger needs to record
        *how* it died.
        """
        try:
            returncode, stdout, _ = await self._docker_call(
                ["docker", "inspect", "--format", "{{json .State}}", container_name],
                budget_seconds,
            )
        except (SandboxError, TimeoutError):
            log.warning(
                "sandbox.docker.state_unavailable",
                container=container_name,
                detail="the daemon could not be asked about the container's state",
            )
            return {}
        if returncode != 0:
            return {}
        try:
            state = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
        except json.JSONDecodeError:
            log.warning("sandbox.docker.unreadable_state", container=container_name)
            return {}
        return state if isinstance(state, dict) else {}

    @staticmethod
    def _state_exit_code(state: dict[str, Any]) -> int | None:
        """The container's exit code as the daemon recorded it, if it did."""
        raw = state.get("ExitCode")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw

    @staticmethod
    def _lifetime_ms(state: dict[str, Any]) -> float | None:
        """The container's lifetime, in the daemon's clock, in milliseconds.

        ``StartedAt`` to ``FinishedAt`` — the span the work budget bounds.
        It is not the wall clock and it is not the workload's own duration:
        it includes the runtime's setup and teardown, which is why the
        budget has to accommodate them (ADR-0031).
        """
        started = _docker_timestamp(state.get("StartedAt"))
        finished = _docker_timestamp(state.get("FinishedAt"))
        if started is None or finished is None:
            return None
        return (finished - started) * 1000.0

    def _work_budget(
        self, spec: SandboxSpec, started_at: float | None, client_started_at: float
    ) -> float | None:
        """How long the **work** may still run, in seconds.

        The deadline is ``StartedAt + timeout_seconds`` in the daemon's
        clock: the budget is on the container's *lifetime*, which begins when
        the daemon started the container's process and ends when it stops.

        The client's own clock is a fallback, and it is a fallback because it
        is the only clock that can be wrong in the dangerous direction. A
        daemon whose clock runs *ahead* of the host's would push the deadline
        away, and the caller would get more than it asked for. So the budget
        is capped at the caller's own ``timeout_seconds``: the deadline is
        honoured when the clocks agree, and the caller's ask is the ceiling
        when they do not. A daemon whose clock runs *behind* the host's
        shortens the budget instead, which errs the safe way.

        **Where the fallback is anchored.** When the daemon does not say when
        the container started, the deadline is anchored on the **client's own
        start-of-work clock** — the moment ``docker start`` returned, which
        is the client's closest observation of the container's start. It is
        deliberately *not* anchored on the moment of the inspect, which is
        later: an anchor that late would measure the caller's budget from the
        *report* rather than from the work, and the container's lifetime
        would be ``timeout_seconds`` plus the whole of the start and the
        inspect — more than the caller asked for, which is exactly what the
        fallback exists to avoid. The fallback fires only when the daemon
        does not answer or reports the zero time, so the excess is at most
        one round trip; the anchor makes it no more than that.
        """
        if spec.timeout_seconds is None:
            return None
        if started_at is None:
            deadline = client_started_at + spec.timeout_seconds
        else:
            deadline = started_at + spec.timeout_seconds
        return max(0.0, min(spec.timeout_seconds, deadline - time.time()))

    async def _collect_logs(self, container_name: str) -> tuple[bytes, bytes]:
        """The container's output, with the two streams kept apart.

        ``docker logs`` **demultiplexes**: the container's stdout comes back
        on the client's stdout and its stderr on the client's stderr, exactly
        as ``docker run`` did (measured on this host — a container writing to
        both streams comes back with ``MARKER_OUT`` on stdout and
        ``MARKER_ERR`` on stderr).

        The logs are read *after* the container has stopped, because the
        removal that follows destroys the container's filesystem layer and
        the logs are the only record of what it did.

        A daemon that cannot be reached yields no output rather than an
        exception: the work has already happened, and losing the *report* of
        it is not a reason to lose the run.
        """
        try:
            _, stdout, stderr = await self._docker_call(
                ["docker", "logs", container_name], _DAEMON_QUERY_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return b"", b""
        except SandboxError:
            return b"", b""
        return stdout, stderr

    async def _wait_for_exit(self, container_name: str) -> int | None:
        """The container's exit code, once it has stopped.

        Used after a kill: the daemon records the exit asynchronously, so
        asking for the state immediately after ``docker kill`` can find the
        container still running. ``docker wait`` blocks until it is not, and
        then it is the daemon's own number — 137 for a killed container.

        The wait is bounded by :data:`_KILL_SETTLE_TIMEOUT_SECONDS`, and not
        by the client's own patience with a process that will not die. The
        kill has already been sent, so a container that is still running by
        then is a container whose kill did not land — a double fault — and
        the ``docker rm -f`` in the ``finally`` is what cleans it up. The
        long patience belongs in the *client's* reaping, not in the timeout
        path, where it is a stall on a path that has already failed.

        ``None`` means the daemon never said, and the caller reports the
        exit code it has (or none) rather than inventing one.
        """
        try:
            returncode, stdout, _ = await self._docker_call(
                ["docker", "wait", container_name], _KILL_SETTLE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return None
        except SandboxError:
            return None
        if returncode != 0:
            return None
        return _parse_exit_code(stdout)

    async def _remove_container(self, container_name: str) -> None:
        """Remove the container, forcibly.

        ``--rm`` is gone (the logs have to outlive the container), so the
        removal is explicit — and it is in a ``finally``, so it happens on
        every path out of the lifecycle.

        ``-f`` because a container that is still running has to die first: a
        container left running holds the workspace bind mount, which is
        precisely the orphan the timeout exists to prevent. A failure here
        is swallowed on purpose — the container is already gone as far as the
        caller is concerned, and a ``docker rm`` that cannot reach the
        daemon must not turn a finished run into an exception.

        The *client* is reaped if the removal overruns: a ``docker rm`` that
        is still running is a process the backend left behind, holding the
        event loop's transport table open. The container is the daemon's
        business; the client is ours.
        """
        with contextlib.suppress(Exception):
            remover = await self._exec(
                "docker",
                "rm",
                "-f",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(remover.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            except TimeoutError:
                await self._reap(remover)

    async def _force_kill(self, container_name: str) -> None:
        with contextlib.suppress(Exception):
            killer = await self._exec(
                "docker",
                "kill",
                container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=_STOP_TIMEOUT_SECONDS)

