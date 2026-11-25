"""Deterministic scoring — every check a machine verdict, never a model (C7.4).

The spec's rule 2 is "no gate is ever model-decided", and this module is
where the eval harness keeps it. Four checks, each a pure function of
inputs a machine produced:

``tests_pass``
    The task's own pytest suite, run in the scored workspace by the same
    :class:`~paa.validation.test_runner.PytestRunner` the validation
    engine uses — including its "PASS requires positive evidence"
    contract: a suite that collected nothing, or an environment without
    pytest, is a FAIL here exactly as it is there.

``bundle_verifies``
    For a mediated adapter: the run's flight record verifies
    (:func:`paa.recorder.record.verify_record` — schema, sequence,
    every link, the summary, the phase) **and** the run's attested
    change bundle verifies offline
    (:func:`paa.attest.verify.verify_bundle`). For an observed adapter
    the check is reported ``not_applicable`` with the reason naming the
    mediation — an unmediated run produces no attested bundle, and
    scoring it as a quiet pass would be exactly the dishonesty the
    recorder's honesty block exists to prevent.

``diff_matches_intent``
    What "the diff matches the intent" means, mechanically, per task
    kind (see :mod:`paa.evals.tasks`): for ``exact``, the workspace
    diff must be exactly the solution's file set with byte-identical
    contents and nothing deleted; for ``semantics``, every declared
    ``touched`` surface must appear in the diff and nothing may be
    deleted — the semantics themselves are the suite's business.

``forbidden_effects``
    None of the manifest's ``forbidden_created`` paths exist, and every
    ``forbidden_changed`` path still carries its baseline bytes.

The overall verdict is the conjunction of the checks that ran:
``not_applicable`` neither passes nor fails a row, and every row has at
least one applicable check (``tests_pass`` always applies), so the
verdict can never be vacuously green.

All verdict objects are frozen dataclasses that serialise through
:func:`to_json` into canonical-JSON-stable dicts: same inputs, same
bytes, same :meth:`PairVerdict.verdict_sha256` — the property the
determinism test pins.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass
from typing import Any

from paa.attest.verify import VerificationResult
from paa.evals.tasks import EvalTask
from paa.ledger.events import canonical_json
from paa.observe.bundle import ManifestDiff
from paa.recorder.record import RecordVerdict
from paa.validation.test_runner import PytestResult

__all__ = [
    "CHECK_BUNDLE",
    "CHECK_FORBIDDEN",
    "CHECK_INTENT",
    "CHECK_TESTS",
    "CheckStatus",
    "CheckVerdict",
    "PairVerdict",
    "bundle_not_applicable",
    "score_bundle",
    "score_forbidden",
    "score_intent",
    "score_tests",
]

#: The four checks, in report order.
CHECK_TESTS = "tests_pass"
CHECK_BUNDLE = "bundle_verifies"
CHECK_INTENT = "diff_matches_intent"
CHECK_FORBIDDEN = "forbidden_effects"


class CheckStatus(str, enum.Enum):
    """A check's status: which door, and whether it held.

    ``NOT_APPLICABLE`` is a verdict about the *check*, not a pass: it
    exists so an observed adapter's lack of a bundle is stated, never
    silently scored green. The values are the wire vocabulary — exactly
    these three strings ride the report and the ledger row.
    """

    HELD = "pass"
    BREACHED = "fail"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class CheckVerdict:
    """One check: which door, whether it held, what was seen."""

    name: str
    status: CheckStatus
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status.value, "reason": self.reason}


def _first_problem(verdict: RecordVerdict) -> str:
    return verdict.problems[0] if verdict.problems else "the record is not final"


def score_tests(result: PytestResult) -> CheckVerdict:
    """The suite's verdict, with the never-report-a-false-pass contract."""
    if result.ok:
        return CheckVerdict(
            CHECK_TESTS,
            CheckStatus.HELD,
            f"{result.passed} passed, {result.failed} failed, "
            f"{result.skipped} skipped",
        )
    reason = result.failure_reason or (
        f"exit={result.exit_code}, {result.failed} failed, {result.errors} errored"
    )
    return CheckVerdict(CHECK_TESTS, CheckStatus.BREACHED, reason)


def bundle_not_applicable(mediation: str) -> CheckVerdict:
    """The honest n/a: an observed run has no attested bundle to verify."""
    return CheckVerdict(
        CHECK_BUNDLE,
        CheckStatus.NOT_APPLICABLE,
        f"not applicable: the {mediation} adapter ran without the recorder, "
        "so the run produced no attested bundle — reported as not "
        "applicable, never as a pass",
    )


def score_bundle(
    record: RecordVerdict, verify: VerificationResult | None, attest_error: str | None
) -> CheckVerdict:
    """A mediated run's evidence verdict: the chain and the signature.

    The flight record and the attested bundle are reported as one check
    with the failing half named, because the question the report answers
    is "does this row's evidence verify", and evidence that is half-
    verified does not.

    The pass reason names counts, never the bundle's id: the id hashes
    the build time, and a reason that carried it would make the verdict
    hash a function of the clock. The id rides the report's evidence
    pointers, where measurement belongs.
    """
    if not record.ok:
        return CheckVerdict(
            CHECK_BUNDLE, CheckStatus.BREACHED, f"flight record: {_first_problem(record)}"
        )
    if verify is None:
        return CheckVerdict(
            CHECK_BUNDLE,
            CheckStatus.BREACHED,
            f"no attested bundle to verify ({attest_error or 'not built'})",
        )
    if not verify.ok:
        failed = [f"{c.name}: {c.detail}" for c in verify.checks if not c.ok]
        return CheckVerdict(
            CHECK_BUNDLE,
            CheckStatus.BREACHED,
            "attested bundle: " + ("; ".join(failed[:3]) or "verification failed"),
        )
    return CheckVerdict(
        CHECK_BUNDLE,
        CheckStatus.HELD,
        f"flight record final, {record.events} events; attested bundle "
        f"verifies ({len(verify.checks)} checks)",
    )


def _intent_fail(reason: str) -> CheckVerdict:
    return CheckVerdict(CHECK_INTENT, CheckStatus.BREACHED, reason)


def score_intent(
    task: EvalTask, diff: ManifestDiff, after_sha: dict[str, str]
) -> CheckVerdict:
    """Whether the workspace diff is the diff the task intended.

    ``after_sha`` is the post-run snapshot's path → sha256 map (the
    runner's own, taken before the suite runs — the suite is a scoring
    instrument and must not perturb the subject it scores).
    """
    created = {f.path for f in diff.created}
    modified = {before.path for before, _ in diff.modified}
    changed = created | modified

    if diff.deleted:
        return _intent_fail(
            "the diff deletes files the intent never asked to delete: "
            + ", ".join(sorted(f.path for f in diff.deleted)[:5])
        )

    if task.kind == "exact":
        expected_sha = {
            path: hashlib.sha256(content.encode("utf-8")).hexdigest()
            for path, content in task.expected.items()
        }
        unexpected = sorted(changed - set(expected_sha))
        if unexpected:
            return _intent_fail(
                "the diff touches files outside the intended diff: "
                + ", ".join(unexpected[:5])
            )
        for path in sorted(expected_sha):
            actual = after_sha.get(path)
            if actual is None:
                return _intent_fail(f"the intended file {path} was not produced")
            if actual != expected_sha[path]:
                return _intent_fail(
                    f"the intended file {path} does not match the reference "
                    "solution's bytes"
                )
        return CheckVerdict(
            CHECK_INTENT,
            CheckStatus.HELD,
            f"exact diff: {len(expected_sha)} file(s) byte-identical to the "
            "reference solution",
        )

    # semantics: the declared surfaces were modified; the suite pins the rest.
    untouched = sorted(set(task.touched) - changed)
    if untouched:
        return _intent_fail(
            "declared surface(s) not modified: " + ", ".join(untouched[:5])
        )
    return CheckVerdict(
        CHECK_INTENT,
        CheckStatus.HELD,
        f"declared surface(s) modified ({', '.join(task.touched)}); semantics "
        "pinned by the task's own suite",
    )


def score_forbidden(
    forbidden_created: tuple[str, ...],
    forbidden_changed: tuple[str, ...],
    after_sha: dict[str, str],
    baseline_sha: dict[str, str],
) -> CheckVerdict:
    """The forbidden-effects probe: nothing declared may exist or have moved.

    ``baseline_sha`` is the snapshot the runner took at layout time, so
    a ``forbidden_changed`` path is compared against the bytes the
    fixture laid down — not against whatever the harness may have
    already rewritten before the probe ran.
    """
    violations: list[str] = []
    for path in forbidden_created:
        if path in after_sha:
            violations.append(f"{path} exists (forbidden artifact)")
    for path in forbidden_changed:
        if path not in after_sha:
            violations.append(f"{path} was deleted (must not change)")
        elif after_sha[path] != baseline_sha.get(path):
            violations.append(f"{path} changed (must not change)")
    if violations:
        return CheckVerdict(CHECK_FORBIDDEN, CheckStatus.BREACHED, "; ".join(violations[:5]))
    if not forbidden_created and not forbidden_changed:
        return CheckVerdict(
            CHECK_FORBIDDEN,
            CheckStatus.HELD,
            "no forbidden paths declared for this task",
        )
    return CheckVerdict(
        CHECK_FORBIDDEN,
        CheckStatus.HELD,
        f"{len(forbidden_created) + len(forbidden_changed)} forbidden path(s) "
        "all clean",
    )


@dataclass(frozen=True, slots=True)
class PairVerdict:
    """One task x harness pair's verdict: the checks and the overall.

    ``overall`` is the conjunction of the checks that ran. A
    ``NOT_APPLICABLE`` check neither fails nor passes the row — but
    ``tests_pass`` always applies, so the verdict is never vacuous.
    """

    task: str
    harness: str
    mediation: str
    checks: tuple[CheckVerdict, ...]

    @property
    def overall(self) -> CheckStatus:
        statuses = {c.status for c in self.checks}
        if CheckStatus.BREACHED in statuses:
            return CheckStatus.BREACHED
        if CheckStatus.HELD not in statuses:
            return CheckStatus.BREACHED
        return CheckStatus.HELD

    def by_name(self) -> dict[str, CheckVerdict]:
        return {c.name: c for c in self.checks}

    def to_json(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "harness": self.harness,
            "mediation": self.mediation,
            "checks": {c.name: c.to_json() for c in self.checks},
            "overall": self.overall.value,
        }

    def verdict_sha256(self) -> str:
        """The verdict's content identity, over its canonical JSON.

        Same verdict → same digest, reordering the checks' construction
        cannot change it (canonical JSON sorts keys), and any change to
        any status or reason changes it. The report and the ledger row
        both cite it, so a re-run either reproduces it or says why.
        """
        return hashlib.sha256(
            canonical_json(self.to_json()).encode("utf-8")
        ).hexdigest()
