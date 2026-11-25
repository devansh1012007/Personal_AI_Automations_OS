"""The harness registry: named adapters, refused ambiguity (C7.4).

"Same tasks, N harnesses" needs the N to be *identities*, not flags: the
registry refuses a duplicate id because two harnesses answering to one
name would make every ledger row that cites the name ambiguous, and the
mediation mode is a closed vocabulary because the report labels mediation
from it. These tests pin both, plus the two shipped adapters' own
contract: their scripts exist and their ids are what the matrix cites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from paa.evals.harnesses import (
    HarnessAdapter,
    get_adapter,
    known_adapters,
    register_adapter,
)
from paa.evals.tasks import EvalError, load_tasks, task_set_root


def _scratch_adapter(harness_id: str, mediation: str = "observed") -> HarnessAdapter:
    return HarnessAdapter(
        harness_id=harness_id,
        mediation=mediation,
        description="a scratch adapter for registry tests",
        command=lambda task, ws: ("true",),
        child_env=lambda task, ws: {},
    )


class TestTheRegistry:
    def test_both_shipped_adapters_resolve_by_name(self) -> None:
        recorded = get_adapter("recorded-fake")
        direct = get_adapter("scripted-direct")
        assert recorded.mediation == "mediated"
        assert recorded.uses_recorder is True
        assert direct.mediation == "observed"
        assert direct.uses_recorder is False

    def test_the_refusal_names_the_known_harnesses(self) -> None:
        with pytest.raises(EvalError, match=r"known harnesses:.*recorded-fake"):
            get_adapter("claude-code")

    def test_a_duplicate_id_is_refused_not_repointed(self) -> None:
        with pytest.raises(EvalError, match="already registered"):
            register_adapter(_scratch_adapter("recorded-fake"))

    def test_an_unregistered_scratch_adapter_comes_and_goes(self) -> None:
        adapter = register_adapter(_scratch_adapter("scratch-adapter"))
        assert get_adapter("scratch-adapter") is adapter

    def test_the_mediation_vocabulary_is_closed(self) -> None:
        with pytest.raises(EvalError, match="not one of"):
            _scratch_adapter("telepathic-harness", mediation="telepathic")


class TestTheShippedAdapters:
    def test_the_builtins_register_first_in_a_stable_order(self) -> None:
        # Built-ins register at import; a plug-in appends after. The
        # report's row order depends on the first two being the shipped
        # pair, in this order — that is the contract, not "nothing else
        # can ever register" (the registry exists so things can).
        ids = [a.harness_id for a in known_adapters()]
        assert ids[:2] == ["recorded-fake", "scripted-direct"]

    def test_every_shipped_adapter_names_a_script_that_exists(self) -> None:
        # The command factory runs inside the runner's refusal boundary,
        # so a missing script is a scored refusal — but the fixtures ship
        # with the package, and a regression that loses one should fail
        # here rather than as a mysterious failed row. Only the shipped
        # pair is checked by name: a plug-in registered by an earlier
        # test in this session has its own contract, not this one.
        tasks = load_tasks(task_set_root())
        root = task_set_root()
        for harness_id in ("recorded-fake", "scripted-direct"):
            adapter = get_adapter(harness_id)
            for task in tasks:
                command = adapter.command(task, Path("/tmp/unused-ws"))
                script = Path(command[-1])
                assert script.is_file(), f"{adapter.harness_id} lost {script}"
                assert script.parent == root / "harnesses"

    def test_the_child_env_names_the_task_and_its_directory(self) -> None:
        task = load_tasks(task_set_root())[0]
        env = get_adapter("recorded-fake").child_env(task, Path("/tmp/unused-ws"))
        assert env["PAA_EVAL_TASK_ID"] == task.task_id
        assert env["PAA_EVAL_TASK_DIR"] == str(task.source_dir)
