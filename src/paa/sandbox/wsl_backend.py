"""Containment inside a WSL2 distribution.

WSL2 runs a real Linux kernel in a lightweight VM, so this is a genuine step up
from :class:`~paa.sandbox.subprocess_backend.SubprocessSandbox`: the workload
lands on a *different kernel* than the host Windows one, and Linux's own
containment primitives become available.

Where it lands relative to the RFC's gVisor requirement:

* **Better than the subprocess backend.** ``unshare`` gives PID/mount/network
  namespaces; ``ulimit -v`` gives an address-space cap the process cannot
  exceed; the Windows host filesystem is only reachable through ``/mnt/c``.
* **Still not gVisor.** Namespaces are a *kernel* feature enforced *by* the
  kernel being escaped from; a kernel exploit inside WSL2 defeats them. gVisor
  interposes a user-space kernel so the guest never issues a host syscall at
  all. Declared as :attr:`~paa.sandbox.base.IsolationLevel.NAMESPACE`, not VM,
  even though WSL2 technically runs in a Hyper-V VM — because the VM boundary
  here protects the *host* from the distro, not the *distro* from the workload,
  and it is the latter that this class is being asked to guarantee.

A specific WSL2 caveat worth stating: work under ``/mnt/c`` crosses the 9p/
virtio-fs bridge and is roughly an order of magnitude slower than the distro's
native ext4. Correct, but slow enough that callers doing IO-heavy work should
stage into the Linux filesystem first.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import sys
import time
from pathlib import Path, PureWindowsPath

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

__all__ = ["WslSandbox", "to_wsl_path"]

log = structlog.get_logger(__name__)

_HEALTHCHECK_TIMEOUT_SECONDS = 15.0
_DRAIN_TIMEOUT_SECONDS = 5.0

#: Shell metacharacters that must never reach the ``bash -lc`` string we build.
#: The command itself is quoted, but a workspace path containing a quote would
#: otherwise break out of the quoting — so paths are validated, not trusted.
_UNSAFE_PATH_CHARS = re.compile(r"""['"$`\\\n\r]""")


def to_wsl_path(windows_path: Path | str) -> str:
    """Translate a Windows path to its ``/mnt/<drive>/...`` form.

    Raises :class:`~paa.core.errors.SandboxError` for UNC paths: they have no
    ``/mnt`` equivalent, and silently mangling one would mount the *wrong*
    directory into the sandbox — a containment failure that presents as a
    confusing file-not-found.
    """
    raw = str(windows_path)
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise SandboxError("UNC paths cannot be translated into WSL", path=raw)

    pure = PureWindowsPath(raw)
    drive = pure.drive  # e.g. "C:"
    if not drive or len(drive) != 2 or not drive[0].isalpha():
        raise SandboxError("path has no drive letter to translate", path=raw)

    tail = raw[len(drive) :].replace("\\", "/").lstrip("/")
    return f"/mnt/{drive[0].lower()}/{tail}" if tail else f"/mnt/{drive[0].lower()}"


def _decode_wsl(raw: bytes) -> str:
    """Decode ``wsl.exe`` output, which is inconsistently encoded.

    ``wsl.exe``'s *own* messages (``--status``, ``--list``, error text) are
    UTF-16LE, while bytes produced by the Linux process it launched pass
    through as-is and are normally UTF-8. Decoding one as the other yields
    either mojibake or NUL-interleaved text, and a healthcheck that greps that
    text will silently decide WSL is unavailable.

    Heuristic: a high proportion of NUL bytes means UTF-16LE.
    """
    if not raw:
        return ""
    sample = raw[:512]
    if sample.count(0) > len(sample) // 4:
        return raw.decode("utf-16-le", errors="replace").replace("\x00", "")
    return raw.decode("utf-8", errors="replace")


class WslSandbox(Sandbox):
    """Execute inside a WSL2 distribution with namespace isolation.

    Availability is probed lazily and cached: ``wsl.exe --status`` takes a
    noticeable moment (it may have to start the VM), and the factory calls
    :meth:`healthcheck` on every backend during ``auto`` selection.
    """

    def __init__(
        self,
        settings: SandboxSettings | None = None,
        *,
        distro: str = "Ubuntu-24.04",
    ) -> None:
        self._settings = settings or SandboxSettings()
        self._distro = distro
        self._healthy: bool | None = None

    @property
    def name(self) -> str:
        return "wsl"

    @property
    def isolation_level(self) -> IsolationLevel:
        return IsolationLevel.NAMESPACE

    @property
    def distro(self) -> str:
        return self._distro

    async def healthcheck(self) -> bool:
        """Whether ``wsl.exe`` exists and the distro responds. Never raises."""
        if self._healthy is not None:
            return self._healthy
        self._healthy = await self._probe()
        return self._healthy

    async def _probe(self) -> bool:
        if sys.platform != "win32" or shutil.which("wsl.exe") is None:
            log.debug("sandbox.wsl.unavailable", reason="wsl.exe not on PATH")
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl.exe",
                "-d",
                self._distro,
                "--",
                "true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_HEALTHCHECK_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError) as exc:
            log.debug("sandbox.wsl.unavailable", reason=str(exc))
            return False

        if proc.returncode != 0:
            log.debug(
                "sandbox.wsl.unavailable",
                reason=_decode_wsl(stderr).strip()[:200],
                distro=self._distro,
                returncode=proc.returncode,
            )
            return False
        return True

    async def _has_tool(self, tool: str) -> bool:
        """Whether ``tool`` exists inside the distro.

        Probed rather than assumed: ``unshare`` needs util-linux and is absent
        from minimal images. Building a command around a missing binary would
        fail the *workload* with a confusing error instead of degrading the
        *containment*, which is the wrong trade — a weaker sandbox that runs
        beats a stronger one that does not exist.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl.exe",
                "-d",
                self._distro,
                "--",
                "sh",
                "-c",
                f"command -v {tool} >/dev/null 2>&1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=_HEALTHCHECK_TIMEOUT_SECONDS)
            return proc.returncode == 0
        except (OSError, TimeoutError):
            return False

    def _build_inner_command(
        self,
        spec: SandboxSpec,
        wsl_workspace: str,
        *,
        have_unshare: bool,
    ) -> str:
        """Compose the shell string executed inside the distro.

        Layered, weakest assumption first, so that a distro missing a tool
        loses only that tool's guarantee:

        ``ulimit -v``
            Address-space cap in KB. Enforced by the kernel at ``brk``/``mmap``
            time, so unlike a sampling watchdog it cannot be outrun by a spike.
            Note it bounds *virtual* address space, which for a workload that
            reserves large sparse mappings (the JVM, some allocators) is
            stricter than RSS — a deliberate choice, since over-restriction
            fails loudly and under-restriction fails silently.

        ``ulimit -t``
            CPU-seconds. Complements the host-side wall-clock timeout: a
            process that spins is killed by the kernel even if the host-side
            waiter is somehow starved.

        ``unshare --net``
            Real network severance when ``allow_network`` is false. This is
            the guarantee the subprocess backend cannot make.

        ``unshare --pid --fork --mount-proc``
            A private PID namespace, so the workload cannot see or signal host
            processes, and every descendant dies with the namespace — closing
            the orphan-survivor hole by construction.
        """
        limits = [f"ulimit -v {spec.memory_mb * 1024}"]
        if spec.timeout_seconds is not None:
            limits.append(f"ulimit -t {max(1, int(spec.timeout_seconds) + 1)}")
        # No core dumps: a 512 MB core written into the workspace on crash is a
        # denial of service against the host disk.
        limits.append("ulimit -c 0")

        quoted_cmd = " ".join(_shell_quote(part) for part in spec.command)
        inner = f"cd {_shell_quote(wsl_workspace)} && {'; '.join(limits)}; exec {quoted_cmd}"

        if have_unshare:
            unshare_flags = ["--pid", "--fork", "--mount-proc"]
            if not spec.allow_network:
                unshare_flags.append("--net")
            flags = " ".join(unshare_flags)
            return f"unshare {flags} sh -c {_shell_quote(inner)}"
        return f"sh -c {_shell_quote(inner)}"

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute ``spec`` inside the WSL2 distro."""
        if not await self.healthcheck():
            raise SandboxError(
                "WSL backend is not available",
                distro=self._distro,
                hint="run `wsl.exe --install -d Ubuntu-24.04` or select another backend",
            )

        workspace = resolve_workspace(spec.workspace_path)
        if _UNSAFE_PATH_CHARS.search(str(workspace).replace("\\", "")):
            raise SandboxError(
                "workspace path contains characters unsafe for shell interpolation",
                workspace_path=str(workspace),
            )
        wsl_workspace = to_wsl_path(workspace)

        have_unshare = await self._has_tool("unshare")
        if not have_unshare:
            log.warning(
                "sandbox.wsl.unshare_missing",
                detail="namespace isolation unavailable; falling back to ulimit only",
                distro=self._distro,
            )
        if not spec.allow_network and not have_unshare:
            log.warning(
                "sandbox.wsl.network_not_severed",
                detail="unshare absent, so --net could not be applied",
            )

        inner = self._build_inner_command(spec, wsl_workspace, have_unshare=have_unshare)

        # The child env is passed through WSLENV-free explicit assignment via
        # `env -i`, so the distro's own environment is not inherited either.
        # Same rule as the host backend: build from empty, never subtract.
        child_env = build_child_env(spec, workspace=workspace)
        env_assignments = " ".join(
            f"{k}={_shell_quote(v)}"
            for k, v in sorted(child_env.items())
            if k not in {"PATH", "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "PATHEXT"}
        )
        wrapped = f"env -i PATH=/usr/local/bin:/usr/bin:/bin HOME={_shell_quote(wsl_workspace)} "
        wrapped += f"{env_assignments} {inner}" if env_assignments else inner

        started = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                "wsl.exe",
                "-d",
                self._distro,
                "--",
                "sh",
                "-c",
                wrapped,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            raise SandboxError(
                "failed to launch wsl.exe", distro=self._distro, reason=str(exc)
            ) from exc

        timed_out = False
        killed_reason: str | None = None
        cap = self._settings.max_capture_bytes

        try:
            if spec.timeout_seconds is None:
                stdout_raw, stderr_raw = await proc.communicate()
            else:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    proc.communicate(), timeout=spec.timeout_seconds
                )
        except TimeoutError:
            timed_out = True
            killed_reason = "timeout"
            stdout_raw, stderr_raw = b"", b""
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=_DRAIN_TIMEOUT_SECONDS)
            log.warning(
                "sandbox.wsl.timeout", distro=self._distro, timeout=spec.timeout_seconds
            )

        truncated = len(stdout_raw) > cap or len(stderr_raw) > cap
        duration_ms = (time.perf_counter() - started) * 1000.0

        return SandboxResult(
            exit_code=proc.returncode,
            stdout=_decode_wsl(stdout_raw[:cap]),
            stderr=_decode_wsl(stderr_raw[:cap]),
            duration_ms=duration_ms,
            # ulimit -v enforces at the kernel; we get no reading back from it,
            # and reporting a fabricated number would be worse than None.
            peak_rss_mb=None,
            timed_out=timed_out,
            killed_reason=killed_reason,
            truncated_output=truncated,
            backend=self.name,
            isolation_level=self.isolation_level,
            memory_enforcement="ulimit",
        )


def _shell_quote(value: str) -> str:
    """POSIX single-quote a value for safe interpolation.

    ``shlex.quote`` is the obvious answer, and this is exactly it — spelled out
    because the WSL command is assembled as a string and every unquoted
    interpolation into it is a command-injection site. Agent-authored argv is
    attacker-influenced input by definition.
    """
    if not value:
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"
