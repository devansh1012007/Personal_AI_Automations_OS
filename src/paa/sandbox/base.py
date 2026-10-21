"""The containment contract every sandbox backend implements.

SPEC DEVIATION (docs/adr/0006): RFC §13/§14 specify gVisor (``runsc``) as *the*
containment substrate. gVisor is a Linux user-space kernel implemented against
the Linux syscall ABI; it does not exist on Windows and cannot be ported to it.
The target machine is Windows 11 with no Docker daemon.

Rather than pretend, this package defines a **pluggable** contract with an
:class:`IsolationLevel` that every backend must declare honestly, so a caller
can ask "what containment am I actually getting?" and branch on the answer
instead of assuming. The policy layer is expected to refuse high-risk skills
when the available level is below what the skill's risk profile demands.

The honest ranking on this hardware is::

    DryRunSandbox      NONE       nothing executes at all
    SubprocessSandbox  PROCESS    same kernel, same user, same network stack
    WslSandbox         NAMESPACE  separate kernel + PID/mount namespaces
    DockerSandbox      NAMESPACE  (VM when --runtime=runsc is present)

Nothing here is equivalent to gVisor's syscall interception. ``PROCESS`` in
particular means the workload runs as the *same OS user* with the *same*
filesystem and network reachability as the host runtime; its containment is a
workspace jail, a memory cap, a wall-clock kill switch and a mandatory AST
pre-scan (:mod:`paa.validation.ast_scanner`). A determined adversary with
arbitrary code execution defeats it. It is a guard against a *confused* agent,
not a *hostile* one.
"""

from __future__ import annotations

import abc
import enum
import os
import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from paa.core.errors import SandboxError
from paa.core.types import ComplexityModality, ModalityProfile
from paa.sandbox._win32 import IS_WINDOWS

__all__ = [
    "IsolationLevel",
    "Sandbox",
    "SandboxResult",
    "SandboxSpec",
    "build_child_env",
    "collect_sandbox_facts",
    "resolve_workspace",
    "sandbox_report_rows",
]


class IsolationLevel(enum.IntEnum):
    """What containment a backend actually delivers.

    ``IntEnum`` so callers can write ``level >= IsolationLevel.NAMESPACE`` —
    the ordering is the whole point. Each member documents the *escape* it does
    NOT stop, because a security level is only meaningful in terms of what gets
    through it.
    """

    NONE = 0
    """Nothing executes. Planning-only / dry-run. No escape to prevent."""

    PROCESS = 1
    """A child process on the host kernel, same OS user, same network stack.

    Stops: runaway CPU/wall-clock, runaway memory (Job Object or watchdog),
    accidental writes outside the workspace cwd.
    Does NOT stop: reading any file the host user can read, opening sockets,
    ptrace-class tricks, or any syscall at all. There is no syscall boundary.
    """

    NAMESPACE = 2
    """Separate PID/mount/network namespaces, shared or peer kernel.

    Stops: everything PROCESS stops, plus filesystem visibility outside the
    mounted workspace and (with ``--network=none``) network reachability.
    Does NOT stop: kernel-level exploits — the host kernel is still reachable
    through the full syscall surface.
    """

    VM = 3
    """Syscall interception or hardware virtualisation (gVisor, Firecracker).

    The only level that interposes on the syscall boundary itself. This is what
    the RFC assumes everywhere it says "sandbox", and it is NOT available on
    the target hardware.
    """

    @property
    def is_real_containment(self) -> bool:
        """Whether this level survives deliberately hostile code.

        ``PROCESS`` does not, and saying so in one place stops every caller
        from having to re-derive it.
        """
        return self >= IsolationLevel.NAMESPACE


#: Container paths an ``exec_tmpfs`` mount must not shadow, **and everything
#: under them**.
#:
#: A tmpfs mount *replaces* whatever was at the path it is mounted on. Mounting
#: one over ``/usr`` does not give the workload a scratch directory — it gives
#: it an empty filesystem where the image's own binaries were, and the failure
#: that follows names the wrong cause entirely.
#:
#: The list is the set of paths a container cannot start without: the
#: kernel's pseudo-filesystems, the loader and the binary directories, the
#: configuration directory, and the workspace mount itself.
#:
#: The rule is **by prefix**, not by exact match. ``/usr/bin`` is just as
#: fatal as ``/usr`` — it holds the shell — and an exact-match list would
#: wave it through. On Debian (usrmerge) ``/bin`` is a symlink to
#: ``/usr/bin``, so refusing ``/bin`` protects nothing on its own; refusing
#: ``/usr`` covers the real location.
#:
#: ``/tmp`` is here for a different reason, and it matters: the backend
#: mounts ``/tmp`` ``noexec`` on purpose. A second ``--tmpfs /tmp`` with
#: ``exec`` would *win* — Docker takes the last one — and the ``noexec``
#: property would silently disappear. The invariant is stated in ADR-0024 and
#: in the deployment guide, so it is enforced here rather than assumed.
#:
#: ``/run`` and ``/var`` are deliberately *not* here: they are ordinary
#: scratch locations, and ``/run`` is exactly where the toolbox image puts
#: npm's cache — a path that has to be exec-capable.
_SHADOWED_CONTAINER_PATHS = frozenset(
    {
        "/",
        "/bin",
        "/sbin",
        "/lib",
        "/lib32",
        "/lib64",
        "/usr",
        "/etc",
        "/proc",
        "/sys",
        "/boot",
        "/root",
        "/tmp",  # noqa: S108 - a container path in a denylist, not a temp file
        "/workspace",
    }
)

#: Paths that are protected **exactly**, but whose subdirectories are not.
#:
#: ``/dev`` holds the device nodes a container cannot start without — no
#: ``/dev/null``, no ``/dev/urandom``. But everything *under* it is a mount
#: point the runtime already manages (``/dev/shm``, ``/dev/pts``), and
#: replacing one of those with a scratch tmpfs is a normal thing to do.
_SHADOWED_CONTAINER_EXACT = frozenset({"/dev"})


def _shadows_a_container_path(path: str) -> str | None:
    """The protected root ``path`` sits under, or ``None`` if it is safe.

    ``/usr/bin`` sits under ``/usr``; ``/usr`` *is* ``/usr``; ``/usrlocal``
    sits under neither, because the match is on a path boundary.
    """
    for protected in _SHADOWED_CONTAINER_PATHS:
        if path == protected or path.startswith(protected + "/"):
            return protected
    for protected in _SHADOWED_CONTAINER_EXACT:
        if path == protected:
            return protected
    return None


def _normalise_container_path(raw: str) -> str:
    """Collapse a container path to the form the container will see.

    ``posixpath.normpath`` is lexical — it does not touch the filesystem, so
    it works for paths that do not exist yet, which is the normal case for a
    scratch directory. The one thing it does not do on its own is collapse a
    leading ``//``, which POSIX keeps as a distinct, implementation-defined
    root; a container has one root, so it is collapsed here.
    """
    path = posixpath.normpath(raw.strip())
    while path.startswith("//"):
        path = path[1:]
    return path


class SandboxSpec(BaseModel):
    """One unit of contained work.

    ``env`` is the security-critical field. It is an **explicit allowlist**: the
    child receives exactly these variables plus the minimal OS bootstrap set
    (``PATH``/``SystemRoot``) that a process needs to start at all. The host
    environment is *never* inherited.

    That is not paranoia about a hypothetical. A developer machine's
    environment routinely holds ``ANTHROPIC_API_KEY``, ``AWS_SECRET_ACCESS_KEY``,
    ``GITHUB_TOKEN`` and database DSNs. ``subprocess`` inherits ``os.environ``
    *by default*, so the safe behaviour has to be the one we write down —
    ambient secret leakage into agent-authored code is the threat here, and it
    is silent when it happens.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: tuple[str, ...]
    """argv. Never a shell string — no backend passes this through a shell, so
    shell metacharacters in agent-authored arguments are inert."""

    workspace_path: Path
    """The one writable directory. Resolved to a real absolute path and used as
    the child's cwd; :func:`resolve_workspace` enforces the jail."""

    read_only_mounts: tuple[Path, ...] = ()
    """Extra paths exposed read-only. Honoured by the container backends; the
    subprocess backend cannot enforce this and says so in its docstring."""

    env: dict[str, str] = Field(default_factory=dict)
    """Allowlisted child environment. See the class docstring."""

    memory_mb: int = Field(default=256, gt=0)
    cpu_cores: float = Field(default=0.5, gt=0)

    timeout_seconds: float | None = Field(default=30.0)
    """``None`` means "no wall-clock kill" — only MAX modality, which blocks on
    human attestation instead. Backends must treat ``None`` as "wait forever"
    rather than as zero."""

    allow_network: bool = False
    """Default-deny. Read :meth:`SubprocessSandbox.run` before trusting this on
    the subprocess backend — it cannot enforce it and does not claim to."""

    network: str | None = None
    """An explicit Docker network to attach the container to.

    ``None`` means the backend's default: ``--network none`` when the network
    is denied, and the Docker daemon's *default bridge* when it is allowed.
    The default bridge has full host and internet reachability — including
    the host's gateway address — so a caller that wants a networked container
    to be reachable from the runtime but not from the substrate services must
    name the network explicitly. The tool layer pins ``paa_tools`` (no
    external routing at all) or ``paa_tools_egress`` here.
    """

    exec_tmpfs: tuple[str, ...] = ()
    """Extra tmpfs mounts that are **exec**-capable, by container path.

    ``/tmp`` is mounted ``noexec`` on purpose: a binary dropped there by a
    workload should not be executable. Some runtimes need the opposite — npm's
    cache holds the executable shims that ``npx`` installs, and a server
    started through ``npx`` fails with ``Permission denied`` (exit 126) when
    the cache is on a ``noexec`` mount. Those workloads get their own
    exec-capable scratch path here rather than the whole image losing the
    ``noexec`` property.

    The paths are validated: they must be absolute, must not be ``/``, and
    must not shadow a path the container's own filesystem needs. A tmpfs
    mount replaces whatever was at that path, so ``("/etc",)`` would hand the
    workload an empty ``/etc`` — not a scratch directory, a broken image.
    """

    recursion_depth: int = Field(default=0, ge=0)
    """Delegation depth that produced this spec. Enforced by
    :class:`paa.sandbox.recursion.RecursionGuard`, carried here so the ledger
    and the watchdog can attribute resource use to a nesting level."""

    parent_task_id: str | None = None

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("command must have at least one element (the executable)")
        if any(not part for part in v):
            raise ValueError("command must not contain empty arguments")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_timeout(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError(f"timeout_seconds must be positive or None, got {v}")
        return v

    @field_validator("exec_tmpfs")
    @classmethod
    def _safe_exec_tmpfs(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse exec tmpfs paths that would break the container.

        A tmpfs mount *replaces* the path it is mounted on. ``/etc`` or
        ``/usr`` would not be a scratch directory — they would be an empty
        filesystem where the image's own configuration lived, and the
        workload would fail with errors that name the wrong cause. The
        container's root is worse than useless for the same reason.

        The paths are **normalised before they are judged**, because the
        comparison is against the path the container will actually see.
        ``/run/../etc`` resolves to ``/etc``, and a validator that compared
        the literal string would wave it through — and the tmpfs would then
        replace the container's ``/etc`` with an empty, exec-capable
        filesystem, which is the exact failure this check exists to prevent.
        """
        normalised: list[str] = []
        for raw in v:
            path = _normalise_container_path(raw)
            if not path.startswith("/"):
                raise ValueError(
                    f"exec_tmpfs paths must be absolute container paths, got {raw!r}"
                )
            if path == "/":
                raise ValueError("exec_tmpfs must not be the container root")
            protected = _shadows_a_container_path(path)
            if protected is not None:
                raise ValueError(
                    f"exec_tmpfs must not shadow {path!r}: a tmpfs mount replaces "
                    f"the path, so the workload would lose the image's own files "
                    f"under {protected!r}"
                )
            normalised.append(path)
        return tuple(normalised)

    @model_validator(mode="after")
    def _reject_ambient_env_smell(self) -> Self:
        """Catch the obvious ``env=dict(os.environ)`` mistake at construction.

        This cannot be airtight — a caller can always copy secrets in by hand,
        and sometimes should. It is a tripwire for the *accidental* whole-
        environment splat, which is the failure mode that actually happens in
        review-passed code.
        """
        upper = {key.upper() for key in self.env}
        if len(self.env) > 40 and "SYSTEMROOT" in upper and "USERPROFILE" in upper:
            raise ValueError(
                "env looks like a copy of the host environment "
                f"({len(self.env)} vars incl. USERPROFILE/SystemRoot). "
                "SandboxSpec.env is an explicit allowlist — pass only what the "
                "workload needs. See docs/adr/0006."
            )
        return self

    @classmethod
    def from_profile(
        cls,
        command: tuple[str, ...] | list[str],
        workspace_path: Path | str,
        profile: ModalityProfile | ComplexityModality,
        **overrides: Any,
    ) -> SandboxSpec:
        """Build a spec whose limits come from the modality matrix (RFC §9.2).

        Keeps the resource envelope in one place — a caller that hand-rolls
        ``memory_mb`` will eventually disagree with ``MODALITY_PROFILES`` and
        the disagreement will be invisible.
        """
        from paa.core.types import MODALITY_PROFILES

        if isinstance(profile, ComplexityModality):
            profile = MODALITY_PROFILES[profile]
        return cls(
            command=tuple(command),
            workspace_path=Path(workspace_path),
            memory_mb=profile.memory_mb,
            cpu_cores=profile.cpu_cores,
            timeout_seconds=profile.timeout_seconds,
            **overrides,
        )


class SandboxResult(BaseModel):
    """Outcome of one contained run.

    Deliberately total: a run that was killed still produces a result rather
    than an exception, because the ledger needs to record *how* it died. Only
    failures to *start* a sandbox raise :class:`~paa.core.errors.SandboxError`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    """``None`` when the process was killed before an exit status existed."""

    stdout: str = ""
    stderr: str = ""
    duration_ms: float = 0.0

    peak_rss_mb: float | None = None
    """``None`` means "not measured" — NOT "used no memory". The distinction
    matters: the subprocess backend degrades to no measurement when neither
    psutil nor the native API is reachable, and a caller must not read that as
    a clean bill of health."""

    timed_out: bool = False
    killed_reason: str | None = None
    """``"timeout"`` | ``"memory"`` | ``"heartbeat"`` | backend-specific."""

    truncated_output: bool = False
    """Output exceeded the capture cap; ``stdout``/``stderr`` are prefixes."""

    backend: str = "unknown"
    isolation_level: IsolationLevel = IsolationLevel.NONE
    """Recorded per-run so the ledger shows what containment was in force when
    the work happened, not what is configured now."""

    memory_enforcement: str = "none"
    """How ``memory_mb`` was actually enforced: ``"cgroup"`` (the kernel's own
    limit on the container's cgroup — which is what the Docker backend's
    ``--memory`` is, under *either* OCI runtime, measured), ``"job_object"``
    (the Windows kernel), ``"watchdog"`` (sampled, best-effort), or
    ``"none"``.

    Which runtime ran the container is a separate fact, and it is recorded
    separately: ``isolation_level`` says ``VM`` under gVisor and
    ``NAMESPACE`` under runc. The two are not the same question — the
    isolation level is about the syscall boundary, this is about the
    ceiling."""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.killed_reason is None

    def to_payload(self) -> dict[str, Any]:
        """Ledger-safe summary. Output is capped — full text lives in the lake."""
        return {
            "exit_code": self.exit_code,
            "duration_ms": round(self.duration_ms, 3),
            "peak_rss_mb": self.peak_rss_mb,
            "timed_out": self.timed_out,
            "killed_reason": self.killed_reason,
            "truncated_output": self.truncated_output,
            "backend": self.backend,
            "isolation_level": self.isolation_level.name,
            "memory_enforcement": self.memory_enforcement,
            "stdout_tail": self.stdout[-2000:],
            "stderr_tail": self.stderr[-2000:],
        }


# ---------------------------------------------------------------------------
# Shared helpers — every backend needs these and they must not drift apart.
# ---------------------------------------------------------------------------


def resolve_workspace(workspace_path: Path | str) -> Path:
    """Resolve a workspace to a real absolute path, or refuse.

    ``Path.resolve()`` is what makes this a jail rather than a suggestion: it
    collapses ``..`` segments *and* follows symlinks, so a workspace handed in
    as ``C:/ws/../../Windows`` or as a symlink pointing at ``/etc`` becomes its
    true target before anything is compared against it. Comparing unresolved
    paths is the classic way a path check passes while the write lands
    somewhere else entirely.

    Raises :class:`~paa.core.errors.SandboxError` when the directory does not
    exist — booting a sandbox against a missing workspace means the caller's
    model of the filesystem is already wrong, and inventing the directory here
    would hide that.
    """
    resolved = Path(workspace_path).expanduser().resolve()
    if not resolved.exists():
        raise SandboxError("workspace does not exist", workspace_path=str(resolved))
    if not resolved.is_dir():
        raise SandboxError("workspace is not a directory", workspace_path=str(resolved))
    return resolved


def assert_inside_workspace(candidate: Path | str, workspace: Path) -> Path:
    """Assert ``candidate`` resolves to somewhere inside ``workspace``.

    The workspace jail. Both sides are fully resolved first — see
    :func:`resolve_workspace` for why that ordering is load-bearing.
    """
    resolved_ws = workspace.resolve()
    resolved = Path(candidate).expanduser().resolve()
    if resolved != resolved_ws and not resolved.is_relative_to(resolved_ws):
        raise SandboxError(
            "path escapes the workspace jail",
            path=str(resolved),
            workspace_path=str(resolved_ws),
        )
    return resolved


#: Host variables the child may inherit *by value* when present, because a
#: process cannot reliably start without them. Everything else is dropped.
#:
#: ``SystemRoot`` is not optional on Windows: without it the CRT cannot locate
#: ``system32`` and socket/crypto initialisation fails with errors that look
#: nothing like "missing environment variable", which makes this a genuinely
#: expensive thing to omit.
#:
#: Spelled uppercase because ``os.environ`` on Windows upper-cases every key at
#: import; looking up ``"SystemRoot"`` happens to work only because the mapping
#: also upper-cases the lookup. Windows itself treats variable names
#: case-insensitively, so the child sees the same variable either way.
_WINDOWS_BOOTSTRAP_KEYS = ("SYSTEMROOT", "WINDIR", "PATHEXT", "NUMBER_OF_PROCESSORS")


def build_child_env(spec: SandboxSpec, *, workspace: Path | None = None) -> dict[str, str]:
    """Construct the child environment from scratch.

    Never derived from ``os.environ`` by copy-and-delete — that pattern fails
    open, because a secret whose name nobody thought to add to the deny list
    survives. This builds up from empty instead, so an unlisted variable is
    absent by construction rather than by vigilance.

    ``TEMP``/``TMPDIR`` are pointed at the workspace so a workload that writes
    scratch files keeps them inside the jail instead of scattering them through
    the host temp directory, where they outlive the sandbox.
    """
    env: dict[str, str] = {}
    ws = str(workspace) if workspace is not None else str(spec.workspace_path)

    # ``IS_WINDOWS`` rather than a bare ``sys.platform == "win32"``: a direct
    # comparison is folded against the platform the checker runs on, so the
    # Windows branch below would read as dead code and never be type-checked
    # at all. The constant keeps the branch live — see ``paa.sandbox._win32``.
    if IS_WINDOWS:
        system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        for key in _WINDOWS_BOOTSTRAP_KEYS:
            if (value := os.environ.get(key)) is not None:
                env[key] = value
        env.setdefault("SYSTEMROOT", system_root)
        # A minimal PATH derived from SystemRoot rather than the host PATH.
        # The host PATH leaks the shape of the developer's toolchain and can
        # place attacker-writable directories ahead of system ones.
        env["PATH"] = os.pathsep.join(
            [
                f"{system_root}\\system32",
                system_root,
                f"{system_root}\\System32\\Wbem",
            ]
        )
        env["TEMP"] = ws
        env["TMP"] = ws
    else:
        env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
        env["HOME"] = ws
        env["TMPDIR"] = ws
        env["LANG"] = "C.UTF-8"

    # Deterministic, quiet Python children. Unbuffered output matters for the
    # capture cap: a buffered child that is killed on timeout would otherwise
    # lose everything it had printed.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    # Caller allowlist wins — passing a secret deliberately is the supported
    # way to give a workload a credential it genuinely needs.
    env.update(spec.env)
    return env


class Sandbox(abc.ABC):
    """Contract for a containment backend.

    Implementations must be safe to reuse across runs and safe to call
    concurrently; nothing here holds per-run state on the instance.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Stable identifier recorded in the ledger (``"subprocess"``, ...)."""

    @property
    @abc.abstractmethod
    def isolation_level(self) -> IsolationLevel:
        """What this backend actually delivers. Must not be aspirational."""

    @abc.abstractmethod
    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` under containment.

        Returns a result for *any* completed execution including failures,
        timeouts and kills. Raises :class:`~paa.core.errors.SandboxError` only
        when the sandbox could not be established — a distinction the caller
        needs, because "the workload failed" and "containment failed" demand
        opposite responses.
        """

    @abc.abstractmethod
    async def healthcheck(self) -> bool:
        """Whether this backend can run work *right now*.

        Must never raise: the factory calls it to choose a backend, and a
        healthcheck that throws would take down backend selection itself.
        """

    async def diagnose(self) -> dict[str, Any]:
        """What the backend can say about itself, for ``paa doctor``.

        The base implementation returns :meth:`describe` — the backend's
        identity and its isolation level. A backend with nothing *extra* to
        report says so by saying nothing extra, and every caller can render
        the result unconditionally.

        Returning the identity rather than an empty dict matters: the
        operator's question is "what is my sandbox, and what does it actually
        contain me?", and a doctor that answers "none (dry-run)" for a
        subprocess backend is not merely unhelpful — it is false, because the
        workload *does* run, on the host, at ``PROCESS`` isolation.

        Implementations **must not raise**. This is a diagnostic, and a
        diagnostic that throws is a diagnostic that lies — a `paa doctor` that
        crashes on the one host where the backend is broken is exactly the run
        whose output the operator needs. Swallow the failure and report it.
        """
        return self.describe()

    def describe(self) -> dict[str, Any]:
        """Backend identity for the ledger and the ``paa doctor`` command."""
        return {
            "backend": self.name,
            "isolation_level": self.isolation_level.name,
            "isolation_rank": int(self.isolation_level),
            "real_containment": self.isolation_level.is_real_containment,
        }

    def __repr__(self) -> str:
        return f"{type(self).__name__}(isolation={self.isolation_level.name})"


async def collect_sandbox_facts(backend: Sandbox) -> dict[str, Any]:
    """Collect a backend's self-report, normalising every failure mode.

    One collector, so every surface that shows a sandbox's facts handles the
    failures the same way. There are three ways this can go wrong and all
    three have to be *reported* rather than crashed on:

    * ``diagnose()`` raises — a diagnostic that throws is a diagnostic that
      lies, and the doctor is exactly the run whose output the operator needs
      on the one host where the backend is broken;
    * ``diagnose()`` returns something that is not a mapping — a backend
      that breaks its own contract should not take the doctor down with it;
    * it returns a mapping that is missing the identity — the collector
      fills in what it can, so the row is never blank.

    The failure becomes a ``diagnostic_error`` fact, which
    :func:`sandbox_report_rows` renders as its own row. A failure that is
    swallowed here is a failure the operator never hears about.
    """
    name = "sandbox"
    try:
        # Inside the try on purpose: a backend whose ``name`` property
        # raises must not take the doctor down either. The function's whole
        # contract is that it reports the failure rather than propagating
        # it, and that has to hold for *every* step of the collection, not
        # just the call to ``diagnose()``.
        name = getattr(backend, "name", "sandbox")
        facts = await backend.diagnose()
        if not isinstance(facts, Mapping):
            raise TypeError(f"diagnose() returned {type(facts).__name__}, not a mapping")
        collected = dict(facts)
    except Exception as exc:
        return {"backend": name, "diagnostic_error": str(exc)}
    collected.setdefault("backend", name)
    return collected


def sandbox_report_rows(facts: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Render a backend's self-report as ``(label, value)`` rows.

    This lives here, in the package that *produces* the facts, rather than in
    each surface that displays them. The facts are part of the backend
    contract — the vocabulary of "daemon", "oci runtime", "sandbox image" and
    the meaning of a tri-state ``runtime_available`` are all defined by the
    backends that report them. Two renderers, each with its own copy of that
    vocabulary, drift: and when they do, the same runtime tells the operator
    two different things about the same host depending on which surface they
    happened to open. A disagreement between the CLI and the TUI is worse
    than either of them being wrong on its own, because the operator has no
    way to tell which one to believe.

    Returns an empty list for a backend with nothing to report, which the
    caller renders as its own "no sandbox" row.
    """
    backend = facts.get("backend")
    if not backend:
        return []

    # A diagnostic that failed is the most important thing there is to say.
    # The doctor exists to report the broken thing, and a failure that is
    # swallowed here is a failure the operator never hears about.
    diagnostic_error = facts.get("diagnostic_error")
    if diagnostic_error:
        return [
            ("sandbox", f"{backend} (diagnostics failed)"),
            ("diagnostics", f"unavailable — {diagnostic_error}"),
        ]

    level = str(facts.get("isolation_level", "?"))
    rows: list[tuple[str, str]] = [("sandbox", f"{backend} ({level})")]

    daemon = facts.get("daemon")
    if daemon is not None:
        rows.append(("docker daemon", str(daemon)))

    runtime = facts.get("container_runtime")
    if runtime is not None:
        available = facts.get("runtime_available")
        if available is True:
            state = "registered"
        elif available is False:
            # The runtime that was asked for is not the runtime that will be
            # used, so the fallback is named — and the isolation level it
            # implies, because that is the number the policy layer reads.
            state = f"NOT registered — falling back to runc, isolation is {level}"
        else:
            state = "could not determine"
        rows.append(("oci runtime", f"{runtime} ({state})"))

    image = facts.get("image")
    if image is not None:
        present = facts.get("image_present")
        if present is True:
            suffix = " (present)"
        elif present is False:
            suffix = " (NOT present on the daemon — the run will fail)"
        else:
            suffix = ""
        rows.append(("sandbox image", f"{image}{suffix}"))

    # A backend that cannot translate host paths is a backend whose mounts go
    # to the wrong place. It is reported as its own row because the fix is a
    # configuration change, not a code change.
    map_error = facts.get("host_path_map_error")
    if map_error:
        rows.append(("host path map", f"MISCONFIGURED — {map_error}"))

    return rows
