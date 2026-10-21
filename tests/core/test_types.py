"""The core type vocabulary: ids, modalities, and the resource envelope.

``ModalityProfile`` is the *hard* resource envelope for one complexity
modality — every field in it is enforced somewhere concrete — so the
arithmetic that reads it has to be exact. A node budget that is not an
integer is not a budget.
"""

from __future__ import annotations

import pytest

from paa.core.types import MODALITY_PROFILES, ComplexityModality, ModalityProfile


def _profile(*, recursion_ceiling: int, branch_factor: int = 2) -> ModalityProfile:
    """A profile carrying the two fields the node arithmetic reads."""
    return ModalityProfile(
        modality=ComplexityModality.STANDARD,
        graph_hops=1,
        recursion_ceiling=recursion_ceiling,
        token_ceiling=2048,
        memory_mb=256,
        cpu_cores=0.25,
        timeout_seconds=10.0,
        branch_factor=branch_factor,
    )


class TestMaxPlanNodes:
    """RFC §11.1: ``(B^(D+1) - 1)/(B - 1)``."""

    def test_the_shipped_profiles_are_unaffected(self) -> None:
        """The shipped ceilings are pinned: they are budgets, not hints.

        A plan that fits under one modality must not fit under another, and
        the node ceiling is what says so.
        """
        assert MODALITY_PROFILES[ComplexityModality.SIMPLE].max_plan_nodes() == 1
        assert MODALITY_PROFILES[ComplexityModality.STANDARD].max_plan_nodes() == 3
        assert MODALITY_PROFILES[ComplexityModality.COMPLEX].max_plan_nodes() == 7
        assert MODALITY_PROFILES[ComplexityModality.MAX].max_plan_nodes() == 31

    @pytest.mark.parametrize("modality", list(ComplexityModality))
    def test_every_shipped_profile_returns_an_int(self, modality: ComplexityModality) -> None:
        assert isinstance(MODALITY_PROFILES[modality].max_plan_nodes(), int)

    def test_a_negative_recursion_ceiling_is_refused(self) -> None:
        """A depth below zero is not a depth.

        The sibling method — ``token_quota_at_depth`` — has always refused a
        negative depth, and the two halves of the same envelope refusing
        nonsense the same way is what a reader should be able to assume.
        """
        with pytest.raises(ValueError, match="recursion_ceiling must be non-negative"):
            _profile(recursion_ceiling=-1).max_plan_nodes()

    def test_the_refusal_covers_the_case_that_returned_a_float(self) -> None:
        """``D == -2`` is where the closed form stopped being an integer.

        Python's ``**`` returns a float for a negative exponent, so
        ``2 ** -1`` is ``0.5`` and the closed form was a float — handed back
        from a method annotated ``-> int``. The refusal is what makes that
        annotation true rather than aspirational.
        """
        with pytest.raises(ValueError, match="non-negative"):
            _profile(recursion_ceiling=-2).max_plan_nodes()

    def test_the_linear_chain_gets_no_pass(self) -> None:
        """``B == 1`` does not excuse a nonsense ceiling.

        The degenerate branch returned ``D + 1``, which for a negative depth
        is a *negative* number of plan nodes — an int, so no type error, and
        just as meaningless. The guard is on the ceiling, not on the branch.
        """
        with pytest.raises(ValueError, match="non-negative"):
            _profile(recursion_ceiling=-3, branch_factor=1).max_plan_nodes()

    def test_the_two_halves_of_the_envelope_refuse_the_same_way(self) -> None:
        """The consistency the refusal exists to keep.

        One profile, two methods, one rule: a depth below zero is refused
        wherever it is named.
        """
        with pytest.raises(ValueError, match="non-negative"):
            _profile(recursion_ceiling=-1).max_plan_nodes()
        with pytest.raises(ValueError, match="non-negative"):
            _profile(recursion_ceiling=1).token_quota_at_depth(-1)
