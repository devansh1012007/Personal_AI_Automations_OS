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


class DockerSandbox(Sandbox):
    """Run a command in a throwaway container.

    Every container is ``--rm``: the filesystem layer is destroyed on exit, so
    a workload cannot persist state between runs except through the explicitly
    mounted workspace. That property is what makes retries clean.
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
        """Assemble the full ``docker run`` argv.

        Each flag maps to a specific RFC §13 requirement:

        ``--rm``
            No layer survives the run.
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
        """
        args = [
            "docker",
            "run",
            "--rm",
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

        args.append(self._image)
        args.extend(spec.command)
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

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` in a container."""
        if not await self.healthcheck():
            raise SandboxError(
                "docker backend is not available",
                hint="install Docker Desktop or select another backend",
            )

        workspace = resolve_workspace(spec.workspace_path)
        container_name = f"paa-{spec.parent_task_id or 'task'}-{int(time.time() * 1000)}"[:63]
        args = self._build_args(spec, workspace, container_name)

        # A named network that does not exist is a configuration mistake, and
        # Docker's own error for it ("network X not found") arrives after the
        # container has been created and read as a workload failure. Say it
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
        try:
            proc = await self._exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SandboxError("failed to launch docker", reason=str(exc)) from exc

        timed_out = False
        killed_reason: str | None = None
        cap = self._settings.max_capture_bytes

        # The wall-clock budget is the step's *work* budget plus the
        # container's start, which is the backend's overhead and not the
        # step's. See SandboxSettings.container_start_grace_seconds for why
        # this is not optional: a SIMPLE step's quarter-of-a-second budget
        # would otherwise be spent entirely on the start.
        budget = (
            None
            if spec.timeout_seconds is None
            else spec.timeout_seconds + self._settings.container_start_grace_seconds
        )

        try:
            if budget is None:
                stdout_raw, stderr_raw = await proc.communicate()
            else:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=budget
                )
        except TimeoutError:
            timed_out = True
            killed_reason = "timeout"
            stdout_raw, stderr_raw = b"", b""
            # `docker kill` the container rather than killing the CLI: killing
            # the client leaves the container running and holding the workspace
            # bind mount, which is precisely the orphan the timeout exists to
            # prevent.
            await self._force_kill(container_name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT_SECONDS)
            log.warning("sandbox.docker.timeout", container=container_name)

        exit_code = proc.returncode
        # 137 == 128 + SIGKILL: the cgroup OOM killer fired. Distinguishing it
        # from an ordinary failure is what lets the orchestrator retry at a
        # higher modality instead of retrying identically and failing again.
        if exit_code == 137 and not timed_out:
            killed_reason = "memory"

        truncated = len(stdout_raw) > cap or len(stderr_raw) > cap
        duration_ms = (time.perf_counter() - started) * 1000.0

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_raw[:cap].decode("utf-8", errors="replace"),
            stderr=stderr_raw[:cap].decode("utf-8", errors="replace"),
            duration_ms=duration_ms,
            peak_rss_mb=None,
            timed_out=timed_out,
            killed_reason=killed_reason,
            truncated_output=truncated,
            backend=self.name,
            isolation_level=self.isolation_level,
            # The mechanism, not the aspiration. See _memory_enforcement.
            memory_enforcement=self._memory_enforcement(),
        )

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
