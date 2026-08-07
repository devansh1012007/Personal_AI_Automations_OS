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
import sys
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from paa.core.errors import SandboxError
from paa.core.types import ComplexityModality, ModalityProfile

__all__ = [
    "IsolationLevel",
    "Sandbox",
    "SandboxResult",
    "SandboxSpec",
    "build_child_env",
    "resolve_workspace",
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
    """How ``memory_mb`` was actually enforced: ``"job_object"`` (kernel),
    ``"cgroup"``, ``"watchdog"`` (sampled, best-effort), or ``"none"``."""

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

    if sys.platform == "win32":
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
