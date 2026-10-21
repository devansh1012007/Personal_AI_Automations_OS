"""The Windows-only half of the sandbox, and the contract that keeps it honest.

Windows support in this codebase is real code that runs on a real host: the
Job Object memory cap, the native RSS sampler, the WSL2 probe. None of it can
be exercised on the Linux machine that runs the suite, which is precisely why
it needs a test that pins the **contract** rather than the behaviour.

Three properties are load-bearing, and all three are invisible from the outside
on a non-Windows host.

**The Windows branches must be live.** A branch guarded by a constant a type
checker can fold is a branch nobody ever looks at — and the day the Windows
containment code is wrong is the day it is wrong on the one platform where
this codebase claims to be strong. The tests below reach into the Windows path
deliberately: they flip the platform constant and assert that the Windows code
actually *runs* and then *degrades*. "Unreachable" becomes a state the suite can
see rather than one it has to infer from a type checker's silence.

**A missing Win32 API must degrade, not crash.** Containment getting weaker is
survivable; the sandbox refusing to start is not. Off Windows there is no
``ctypes.WinDLL`` and no ``ctypes.get_last_error``, and the helpers that reach
for them have to say so clearly rather than fail obscurely.

**The platform question is asked in exactly one place.** Two answers to "are we
on Windows" can disagree, and one of them can disagree with the *checker*
rather than with the host — which is how a Windows branch ends up looking like
dead code to every tool that reads the source.
"""

from __future__ import annotations

import ast
import ctypes
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog.testing

import paa.sandbox
from paa.sandbox import base, subprocess_backend, watchdog, wsl_backend
from paa.sandbox._win32 import IS_WINDOWS, last_error, win_dll
from paa.sandbox.base import SandboxSpec
from paa.sandbox.subprocess_backend import SubprocessSandbox
from paa.sandbox.wsl_backend import WslSandbox


def _package_dir() -> Path:
    """The installed package's own source directory."""
    return Path(paa.sandbox.__file__).parent


def _platform_comparisons(path: Path) -> list[ast.Compare]:
    """Every ``sys.platform`` comparison in a module.

    Compared rather than searched for as text, because a comparison is what a
    type checker folds: ``sys.platform == "win32"`` in a condition is resolved
    against the platform being checked for, and the branch it guards is then
    reported as unreachable — dead code, unchecked and invisible. A mention of
    ``sys.platform`` inside a string or an f-string is not a comparison and
    folds nothing.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        if (
            isinstance(left, ast.Attribute)
            and left.attr == "platform"
            and isinstance(left.value, ast.Name)
            and left.value.id == "sys"
        ):
            found.append(node)
    return found


# ---------------------------------------------------------------------------
# The helpers, off Windows
# ---------------------------------------------------------------------------


def test_the_platform_constant_agrees_with_the_platform() -> None:
    """The constant is the platform's answer, and nothing else.

    If this drifted — say the constant were computed from something other than
    the platform — then every Windows branch in the package would be guarding
    the wrong thing, and the dynamic tests below would still pass, because
    they *flip* the constant rather than trusting it. What catches a drifted
    or hard-coded constant is the static test further down, which requires the
    comparison to exist at all.
    """
    assert IS_WINDOWS is (sys.platform == "win32")


def test_last_error_is_zero_where_the_platform_has_no_last_error() -> None:
    """``0`` is the Win32 convention for "no error was recorded".

    The value is only ever logged — the caller has already established that the
    operation failed — so an unreadable error code must not raise a *second*
    failure on top of the first. Off Windows there is no such code to read, and
    the helper says so by reporting "none" rather than by blowing up.
    """
    if IS_WINDOWS:  # pragma: no cover - the suite runs on a non-Windows host
        pytest.skip("this asserts the off-Windows behaviour")

    assert not hasattr(ctypes, "get_last_error")
    assert last_error() == 0


def test_win_dll_fails_clearly_where_there_is_no_win_dll() -> None:
    """The failure names the platform, so the log says why.

    The old code raised ``AttributeError: module 'ctypes' has no attribute
    'WinDLL'``, which is true but says nothing about *which* platform the
    process was on. The message is the difference between an operator reading
    "this build is not for Windows" and an operator reading "something is
    wrong with ctypes".
    """
    if IS_WINDOWS:  # pragma: no cover - the suite runs on a non-Windows host
        pytest.skip("this asserts the off-Windows behaviour")

    assert not hasattr(ctypes, "WinDLL")
    with pytest.raises(AttributeError) as excinfo:
        win_dll("kernel32")
    assert "WinDLL" in str(excinfo.value)
    assert sys.platform in str(excinfo.value)


def test_win_dll_reports_the_dll_it_was_asked_for() -> None:
    """The name reaches the message, so a wrong name is a legible error.

    The callers load more than one DLL — ``kernel32`` and ``psapi`` — and a
    report that named only the missing attribute could not say which of the
    two could not be loaded. The two fail for different reasons, so the name
    is the difference between a diagnosis and a shrug.
    """
    if IS_WINDOWS:  # pragma: no cover - the suite runs on a non-Windows host
        pytest.skip("this asserts the off-Windows behaviour")

    with pytest.raises(AttributeError) as excinfo:
        win_dll("psapi")
    assert "psapi" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The Windows branches are live
# ---------------------------------------------------------------------------


def test_the_job_object_branch_is_live_and_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows cap is attempted, and its failure is survivable.

    ``create()`` returning ``False`` is the documented "no Job Object" answer:
    the caller falls back to the sampling watchdog. What matters is *how* it
    got there — the branch must have run, and the missing Win32 API must have
    been caught rather than propagated. A branch that returned ``False``
    without ever reaching ``win_dll`` would be a Windows path that had quietly
    stopped existing.
    """
    monkeypatch.setattr(subprocess_backend, "IS_WINDOWS", True)
    attempted: list[str] = []

    def _no_win_dll(name: str) -> ctypes.CDLL:
        attempted.append(name)
        raise AttributeError(f"ctypes.WinDLL is unavailable on {sys.platform}")

    monkeypatch.setattr(subprocess_backend, "win_dll", _no_win_dll)

    job = subprocess_backend._WindowsJobObject(64)
    assert job.create() is False
    assert attempted == ["kernel32"]
    assert job.active is False


def test_the_job_object_failure_is_logged_rather_than_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded memory cap is a fact the operator needs to see.

    The watchdog is a *sampled* ceiling: a workload that spikes between two
    samples sails straight through it. The Job Object is the kernel refusing
    the allocation. Swallowing the reason for losing it would turn a
    containment downgrade into a silent one.
    """
    monkeypatch.setattr(subprocess_backend, "IS_WINDOWS", True)

    def _no_win_dll(name: str) -> ctypes.CDLL:
        raise AttributeError(f"ctypes.WinDLL is unavailable on {sys.platform}")

    monkeypatch.setattr(subprocess_backend, "win_dll", _no_win_dll)

    with structlog.testing.capture_logs() as captured:
        subprocess_backend._WindowsJobObject(64).create()

    events = [entry for entry in captured if entry.get("event") == "sandbox.job_object.unavailable"]
    assert events, "the reason the Windows cap was unavailable must be reported"
    assert "WinDLL" in str(events[0]["error"])


async def test_a_run_on_the_windows_path_without_the_win32_api_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Losing the kernel-enforced cap must not cost the run its containment.

    The Job Object is the stronger of the two memory ceilings, and the
    watchdog is the fallback. A host that cannot create the job still has to
    run the workload — with the *effective* mechanism reported, not the
    requested one. This is the same rule the Docker backend follows when a
    host asks for gVisor and does not have it: the ledger records what
    happened, not what was hoped for.
    """
    monkeypatch.setattr(subprocess_backend, "IS_WINDOWS", True)

    def _no_win_dll(name: str) -> ctypes.CDLL:
        raise AttributeError(f"ctypes.WinDLL is unavailable on {sys.platform}")

    monkeypatch.setattr(subprocess_backend, "win_dll", _no_win_dll)

    sandbox = SubprocessSandbox()
    result = await sandbox.run(
        SandboxSpec(
            command=(sys.executable, "-c", "print('still running')"),
            workspace_path=tmp_path,
        )
    )

    assert result.exit_code == 0
    assert "still running" in result.stdout
    assert result.memory_enforcement == "watchdog"


def test_the_native_rss_sampler_is_live_and_reports_no_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sampler degrades to "not measured", never to a fabricated zero.

    ``None`` and ``0`` are different facts. ``None`` means the ceiling was not
    enforced; ``0`` means the process used no memory, which is a claim about
    the workload that nobody measured. The sampler must be able to say the
    first without ever saying the second.
    """
    monkeypatch.setattr(watchdog, "IS_WINDOWS", True)
    attempted: list[str] = []

    def _no_win_dll(name: str) -> ctypes.CDLL:
        attempted.append(name)
        raise AttributeError(f"ctypes.WinDLL is unavailable on {sys.platform}")

    monkeypatch.setattr(watchdog, "win_dll", _no_win_dll)

    assert watchdog._windows_rss_mb(4242) is None
    assert attempted == ["kernel32"]


async def test_the_wsl_probe_is_live_and_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WSL host that cannot run the probe is unavailable, not broken.

    The probe is the factory's basis for choosing a backend, so it must
    answer "no" rather than raise: a backend whose healthcheck explodes takes
    the whole factory down with it.

    The subprocess is stubbed rather than really launched, so the assertion is
    about the *probe* — that it reaches the attempt and survives its failure —
    and not about whether this particular machine has a WSL distribution.
    """
    monkeypatch.setattr(wsl_backend, "IS_WINDOWS", True)
    monkeypatch.setattr(
        wsl_backend,
        "shutil",
        SimpleNamespace(which=lambda name: f"/usr/bin/{name}"),
    )
    attempted: list[tuple[object, ...]] = []

    async def _no_wsl(*argv: object, **kwargs: object) -> None:
        attempted.append(argv)
        raise OSError("wsl.exe is not installed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _no_wsl)

    assert await WslSandbox().healthcheck() is False
    assert attempted, "the probe must actually try to run wsl.exe"


# ---------------------------------------------------------------------------
# One answer to the platform question
# ---------------------------------------------------------------------------


def test_the_platform_question_is_asked_in_exactly_one_place() -> None:
    """No call site may compare ``sys.platform`` for itself.

    A direct comparison is folded by the type checker against the platform it
    is checking for, so a Windows branch written that way reads as *dead code*
    on a Linux host: unreachable, and therefore never type-checked. The
    containment code would be unguarded and nobody would be told.

    The constant in :mod:`paa.sandbox._win32` is the one place the question is
    asked, and it is asked once. This test is the enforcement: a second
    comparison anywhere in the package fails the build rather than quietly
    turning a Windows branch into dead code.
    """
    offenders: dict[str, int] = {}
    for path in sorted(_package_dir().glob("*.py")):
        count = len(_platform_comparisons(path))
        if count:
            offenders[path.name] = count

    assert offenders == {"_win32.py": 1}, (
        "sys.platform must only be compared in paa/sandbox/_win32.py, and only once: "
        "a direct comparison elsewhere is folded by the type checker and turns the "
        f"branch it guards into dead code. Found: {offenders}"
    )


def test_the_platform_constant_is_annotated_as_a_bool() -> None:
    """The annotation is what keeps the branches from being folded.

    ``IS_WINDOWS: bool`` is deliberately *not* ``Literal[False]`` and not a
    platform-narrowed literal. A literal type would let the type checker
    resolve the constant to a single value, and every ``if not IS_WINDOWS``
    in the package would become a statically decidable branch again — the
    Windows half of the containment code would be dead code once more, with
    the same silence as before.
    """
    source = (_package_dir() / "_win32.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    annotated: list[ast.AnnAssign] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "IS_WINDOWS"
    ]
    assert len(annotated) == 1, "IS_WINDOWS must be defined exactly once, with an annotation"

    annotation = annotated[0].annotation
    assert isinstance(annotation, ast.Name) and annotation.id == "bool", (
        "IS_WINDOWS must be annotated as plain 'bool' — a literal type would let the "
        "type checker fold the Windows branches back into dead code"
    )


def test_the_package_shares_one_copy_of_the_constant() -> None:
    """Every backend asks the *same* constant, not its own copy.

    Two answers to "are we on Windows" can disagree, and one of them can
    disagree with the *checker* rather than with the host — which is how a
    Windows branch ends up looking like dead code to every tool that reads the
    source. Importing the one constant keeps the answer single-valued, and
    defining a local one is exactly how a second answer would appear.
    """
    for module in (base, subprocess_backend, watchdog, wsl_backend):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "paa.sandbox._win32"
            and any(alias.name == "IS_WINDOWS" for alias in node.names)
            for node in ast.walk(tree)
        )
        assert imported, f"{module.__name__} must import IS_WINDOWS from paa.sandbox._win32"

        defined = any(
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "IS_WINDOWS"
                for target in node.targets
            )
            for node in ast.walk(tree)
        )
        assert not defined, f"{module.__name__} must not define its own IS_WINDOWS"
