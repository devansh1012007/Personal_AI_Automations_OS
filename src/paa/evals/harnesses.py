"""The harness adapters — named ways to run a task, in a registry (C7.4).

"Same tasks, N harnesses" only means something if a harness is a *named,
registered thing* rather than a flag buried in the runner. An adapter is
exactly three facts and no behaviour of its own:

* an **id** — what the report and the ledger rows call it;
* a **mediation mode** — ``"mediated"`` (the run happens under
  ``paa record`` and the harness's tool calls ride the MCP policy proxy:
  journalled, policy-checked, contained) or ``"observed"`` (a contained
  run with no recorder: the harness's effects are visible only as the
  T1 observe bundle's manifest/git deltas). The mode is the adapter's
  own claim about itself and the runner enforces it structurally — a
  ``"mediated"`` adapter is wrapped by
  :class:`~paa.recorder.core.RecorderCore`, an ``"observed"`` one by
  :class:`~paa.observe.observer.ObserverCore` — so the report's label
  is never a string the adapter could have mis-stated;
* a **command factory** — the argv to run, given the task and the
  workspace, plus the extra child env the harness needs.

The registry (:func:`register` / :func:`get` / :func:`known`) is the
seam the spec's "no vendor will do this neutrally" line is about: the
two adapters shipped here are **scripted fakes** (deterministic, offline,
no model key — see ``evals/harnesses/``), and a real harness
(claude-code, codex, …) plugs in by registering an adapter that names
its command. Nothing in the runner knows any adapter by name.

Why the fakes are honest: their scripts state, in their own docstrings,
exactly which tasks they solve and how they mis-solve the rest. The
point of the eval harness is to *measure* the difference — a fake that
sometimes fails a test and sometimes leaves a forbidden artifact behind
is the cheapest subject that makes every check in the report mean
something.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from paa.evals.tasks import EvalError, EvalTask

__all__ = [
    "MEDIATION_MODES",
    "HarnessAdapter",
    "get_adapter",
    "known_adapters",
    "register_adapter",
]

#: The mediation modes, in the recorder's own vocabulary (the honesty
#: block's two keys). ``mediated`` implies the recorder; ``observed``
#: is the T1 floor and nothing more.
MEDIATION_MODES: tuple[str, ...] = ("mediated", "observed")


@dataclass(frozen=True, slots=True)
class HarnessAdapter:
    """One registered way to run a task: id, mediation, command, env."""

    harness_id: str
    mediation: str
    description: str
    command: Callable[[EvalTask, Path], tuple[str, ...]]
    child_env: Callable[[EvalTask, Path], Mapping[str, str]]

    def __post_init__(self) -> None:
        if self.mediation not in MEDIATION_MODES:
            raise EvalError(
                f"adapter {self.harness_id!r} declares mediation "
                f"{self.mediation!r}, which is not one of {MEDIATION_MODES} — "
                "the report labels mediation from this field, so it cannot "
                "be a free-form string"
            )

    @property
    def uses_recorder(self) -> bool:
        """Only a mediated adapter runs under ``paa record``.

        The bundle check's applicability derives from this, not from a
        second flag the adapter could forget to keep in step.
        """
        return self.mediation == "mediated"


#: The registry, keyed by adapter id. Module-level and append-only in
#: practice: the built-ins register at import, a plug-in registers once
#: at its own import, and a duplicate id is refused loudly rather than
#: silently re-pointed (two harnesses answering to one name would make
#: every ledger row that cites the name ambiguous).
_REGISTRY: dict[str, HarnessAdapter] = {}


def register_adapter(adapter: HarnessAdapter) -> HarnessAdapter:
    """Register ``adapter``; a duplicate id is refused, not re-pointed."""
    if adapter.harness_id in _REGISTRY:
        raise EvalError(
            f"a harness adapter named {adapter.harness_id!r} is already "
            "registered — harness ids are identities, not slots"
        )
    _REGISTRY[adapter.harness_id] = adapter
    return adapter


def known_adapters() -> tuple[HarnessAdapter, ...]:
    """Every registered adapter, in registration order (built-ins first)."""
    return tuple(_REGISTRY.values())


def get_adapter(harness_id: str) -> HarnessAdapter:
    """One adapter by id; the refusal names the known ones."""
    try:
        return _REGISTRY[harness_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none registered)"
        raise EvalError(
            f"no harness adapter named {harness_id!r} — known harnesses: {known}"
        ) from None


# ---------------------------------------------------------------------------
# The two shipped adapters — both scripted fakes, stated as such
# ---------------------------------------------------------------------------


def _harness_script(name: str) -> Path:
    """The path of a shipped harness script, beside the task set.

    The scripts are repository data under ``evals/harnesses/`` — the
    same tree the task set lives in — so ``task_set_root()`` locates
    them too. An adapter whose script has gone missing refuses at
    selection time, not mid-run.
    """
    from paa.evals.tasks import task_set_root

    script = task_set_root() / "harnesses" / name
    if not script.is_file():
        raise EvalError(
            f"the harness script {script} does not exist — the adapter "
            "cannot run without it"
        )
    return script


def _task_env(task: EvalTask, workspace: Path) -> dict[str, str]:
    """The env every shipped harness needs: which task, where its data is.

    ``PAA_EVAL_TASK_DIR`` points at the task's own directory (the ask
    and the reference solution live there); a scripted fake reads the
    solution from it the way a real harness reads the repository. A real
    harness would ignore both variables and read ``TASK.md`` from the
    workspace instead.
    """
    return {
        "PAA_EVAL_TASK_ID": task.task_id,
        "PAA_EVAL_TASK_DIR": str(task.source_dir),
    }


RECORDED_FAKE = register_adapter(
    HarnessAdapter(
        harness_id="recorded-fake",
        mediation="mediated",
        description=(
            "A scripted cooperating harness (a fake, not a model): spawns the "
            "run's MCP policy proxy and makes every edit through it — "
            "mediated, journalled, policy-checked — then exits. Solves every "
            "task in the set from the reference solution."
        ),
        command=lambda task, ws: (
            sys.executable,
            "-u",
            str(_harness_script("recorded_fake.py")),
        ),
        child_env=_task_env,
    )
)

SCRIPTED_DIRECT = register_adapter(
    HarnessAdapter(
        harness_id="scripted-direct",
        mediation="observed",
        description=(
            "A scripted unmediated harness (a fake, not a model): writes "
            "files with its own hands, no proxy, no recorder — T1-observed "
            "only. Deliberately naive: it implements palindrome-guard "
            "without the non-alphanumeric rule (its tests fail) and leaves a "
            "debug-dump.txt behind on inventory-report (a forbidden effect)."
        ),
        command=lambda task, ws: (
            sys.executable,
            "-u",
            str(_harness_script("scripted_direct.py")),
        ),
        child_env=_task_env,
    )
)
