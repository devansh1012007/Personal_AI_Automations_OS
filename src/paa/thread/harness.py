"""Harness bindings — the registry point where real harnesses plug in (C7.5).

A :class:`HarnessBinding` is the whole knowledge the thread control plane
holds about one harness: its name, what argv to run given the segment's
context, and what environment the harness needs. Nothing about *how* the
harness behaves is known here — that is the honest boundary. The thread
does not need the harness to cooperate to hold the thread; a cooperating
harness (one that routes tool calls through ``paa record proxy``) gets
mediation on top, and both kinds get the same ledger, the same handoff
packets and the same evidence chain.

Two fake bindings ship in this module — ``fake-alpha`` and ``fake-beta`` —
each bound to one of the scripts in :mod:`paa.thread.fixtures`. They exist
so the swap demo is real *offline and deterministically*: two distinct
command lines, two distinct harness identities, one shared workspace, no
network, no vendor CLI. A real harness binding is one more row in the
registry — ``claude-code`` → ``("claude",)``, ``codex`` → ``("codex",)``,
``cursor`` → ``("cursor-agent", ...)``, ``opencode`` → ``("opencode", ...)`` —
and those rows are deliberately **not** added until they can carry their
real flags and env honestly: a registry that names a harness it cannot
launch is a registry that lies about what it can run.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from paa.thread.errors import ThreadError

__all__ = [
    "BindingContext",
    "HarnessBinding",
    "get_binding",
    "known_harnesses",
]

#: Where the shipped fake-harness scripts live. Resolved from this module's
#: own file so the binding works under a source checkout (tests,
#: ``PYTHONPATH=src``) and an installed package alike; a missing script is
#: a packaging defect and is refused loudly rather than spawn-checked.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@dataclass(frozen=True, slots=True)
class BindingContext:
    """What a binding may know when it builds one segment's command.

    Deliberately minimal: the workspace the segment runs in, and the thread
    and segment identities (for env markers the harness can attribute its
    output to). A binding that needed more than this would be a harness
    that is not a harness.
    """

    workspace: Path
    thread_id: str
    segment_id: str


def _fixture_script(name: str) -> Path:
    """The path of one shipped fake-harness script, checked to exist."""
    script = _FIXTURES_DIR / f"{name.replace('-', '_')}.py"
    if not script.is_file():
        # Unreachable on a healthy tree (the scripts ship with the module);
        # the check exists so an unhealthy install refuses here, at the
        # boundary, instead of inside a sandbox spawn where the reason
        # would be buried in a child's stderr.
        raise ThreadError(
            "the fake-harness script is missing from the install",
            binding=name,
            expected_path=str(script),
        )
    return script


def _fake_argv(script_name: str) -> tuple[str, ...]:
    """The command for one shipped fake harness.

    ``sys.executable`` — not ``"python"`` — because the sandbox builds the
    child's environment from scratch with a system-only ``PATH``: a bare
    ``python`` resolves to whatever the host happens to expose there, and
    on a host without ``python-is-python3`` the fake harness would die for
    a reason that has nothing to do with the thread. The absolute
    interpreter is the one whose package tree this module lives in.
    """
    return (sys.executable, str(_fixture_script(script_name)))


def _fake_env(context: BindingContext) -> dict[str, str]:
    """The env markers every shipped fake harness reads, if it wants them.

    Real harness bindings will add their own (API key *names* resolved by
    the proxy, config flags); the markers below are the thread's half of
    the contract — a harness that mentions which stint it was can be
    attributed in the workspace's own artifacts.
    """
    return {
        "PAA_THREAD_ID": context.thread_id,
        "PAA_THREAD_SEGMENT": context.segment_id,
    }


@dataclass(frozen=True, slots=True)
class HarnessBinding:
    """One harness the thread can run a segment under.

    ``argv_factory`` returns the argv for one segment; the caller wraps it
    in containment, workspace cwd and the sandbox's own env allowlist.
    ``env_factory`` returns extra environment variables the harness needs,
    merged into that allowlist — never a replacement for it. Both are
    plain callables so a binding is a value, registrable and inspectable
    without subclassing anything.
    """

    name: str
    description: str
    argv_factory: Callable[[BindingContext], tuple[str, ...]]
    env_factory: Callable[[BindingContext], dict[str, str]] | None = None

    def command_for(self, context: BindingContext) -> tuple[str, ...]:
        return self.argv_factory(context)

    def env_for(self, context: BindingContext) -> dict[str, str]:
        return dict(self.env_factory(context)) if self.env_factory is not None else {}


#: ``fake-alpha``: the harness a thread starts on in the swap demo.
FAKE_ALPHA = HarnessBinding(
    name="fake-alpha",
    description=(
        "offline fake harness: opens the work — one notes line, one file "
        "edit in the workspace"
    ),
    argv_factory=lambda context: _fake_argv("fake-alpha"),
    env_factory=_fake_env,
)

#: ``fake-beta``: the harness the thread swaps to mid-task.
FAKE_BETA = HarnessBinding(
    name="fake-beta",
    description=(
        "offline fake harness: continues the work — appends to the same "
        "notes and the same file the alpha stint left"
    ),
    argv_factory=lambda context: _fake_argv("fake-beta"),
    env_factory=_fake_env,
)

#: The registry itself. Ship-two, grow-by-one-row: adding a real harness
#: is a named binding here plus whatever flags its argv needs — nothing
#: else in the control plane changes.
_REGISTRY: dict[str, HarnessBinding] = {
    FAKE_ALPHA.name: FAKE_ALPHA,
    FAKE_BETA.name: FAKE_BETA,
}


def known_harnesses() -> tuple[str, ...]:
    """Every binding name the registry holds, sorted for stable output."""
    return tuple(sorted(_REGISTRY))


def get_binding(name: str) -> HarnessBinding:
    """The binding registered under ``name``, or a refusal naming the options.

    An unknown harness is an operator typo until real bindings exist, and
    the refusal lists what *is* registered so the correction is one edit
    away — a bare "not found" would send the operator digging for the
    registry by hand.
    """
    binding = _REGISTRY.get(name)
    if binding is None:
        raise ThreadError(f"unknown harness {name!r}", known=known_harnesses())
    return binding
