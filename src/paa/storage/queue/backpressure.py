"""Queue backpressure and graceful degradation. RFC §6.2.

The runtime targets a machine with ~3.5 GB of free RAM and two concurrent
generative streams. Under those constraints an unbounded intake queue does not
degrade gracefully — it degrades into an OOM kill, which destroys in-flight
work that backpressure would merely have slowed down. This module is the
control law that keeps the queue bounded:

===============  ===========================================================
NORMAL           Depth below ``backpressure_depth``. Full cognitive depth.
DEGRADED         Depth at or above ``backpressure_depth``. Still accept
                 everything, but plan more cheaply — one modality step down
                 per task, which cuts token ceiling and recursion budget
                 roughly in half and drains the backlog faster than refusing
                 work would.
SHEDDING         Depth at or above ``shed_load_depth``. Refuse non-essential
                 ingestion so the API can answer HTTP 429 immediately rather
                 than accepting work it has no prospect of running.
===============  ===========================================================

Degrading before shedding is deliberate. Shedding loses requests; degrading
only makes them cheaper. The system therefore spends its whole middle band
trading quality for throughput, and only discards work once that is exhausted.
"""

from __future__ import annotations

import enum

import structlog

from paa.config import QueueSettings
from paa.core.types import ComplexityModality
from paa.storage.queue.base import CONTROL_PLANE_STREAMS, StreamName

__all__ = ["BackpressureController", "BackpressureState", "Verdict"]

log = structlog.get_logger(__name__)


class BackpressureState(enum.StrEnum):
    """How hard the queue is pushing back. RFC §6.2."""

    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    SHEDDING = "SHEDDING"

    @property
    def is_degraded(self) -> bool:
        """Whether planning should drop a modality step."""
        return self in (BackpressureState.DEGRADED, BackpressureState.SHEDDING)

    @property
    def is_shedding(self) -> bool:
        return self is BackpressureState.SHEDDING


#: One rung down the RFC §9.2 modality ladder.
#:
#: SPEC NOTE: RFC §6.2 says degradation steps "MAX down to COMPLICATED", but
#: COMPLICATED appears nowhere in the RFC's own modality enum, which is
#: SIMPLE / STANDARD / COMPLEX / MAX (§9.2). Read literally the rule references
#: a level that cannot be represented. We treat COMPLICATED as COMPLEX — the
#: only neighbouring level that makes the sentence coherent — and step one rung
#: at a time so degradation is proportional to sustained pressure rather than a
#: cliff from MAX straight to the LLM-bypassed SIMPLE tier.
_DEGRADATION_LADDER: dict[ComplexityModality, ComplexityModality] = {
    ComplexityModality.MAX: ComplexityModality.COMPLEX,
    ComplexityModality.COMPLEX: ComplexityModality.STANDARD,
    ComplexityModality.STANDARD: ComplexityModality.SIMPLE,
    # SIMPLE is the floor: it already bypasses the LLM entirely, so there is
    # nothing cheaper to fall back to.
    ComplexityModality.SIMPLE: ComplexityModality.SIMPLE,
}


class BackpressureController:
    """Pure decision logic for load shedding and modality degradation.

    Deliberately stateless and free of I/O. The caller supplies an observed
    depth and receives a verdict, which means every threshold decision is
    directly unit-testable without a queue, a clock, or a database — and the
    admission path stays synchronous and allocation-free on the hot route.
    """

    def __init__(self, settings: QueueSettings | None = None) -> None:
        self._settings = settings or QueueSettings()

    @property
    def backpressure_depth(self) -> int:
        return self._settings.backpressure_depth

    @property
    def shed_load_depth(self) -> int:
        return self._settings.shed_load_depth

    def assess(self, depth: int) -> BackpressureState:
        """Classify an observed queue depth.

        Thresholds are inclusive lower bounds: at exactly ``backpressure_depth``
        the system is already DEGRADED. The setting names the depth *at which*
        the runtime degrades, so treating it as an exclusive bound would defer
        every response by one message.
        """
        if depth >= self._settings.shed_load_depth:
            return BackpressureState.SHEDDING
        if depth >= self._settings.backpressure_depth:
            return BackpressureState.DEGRADED
        return BackpressureState.NORMAL

    def degrade_modality(self, modality: ComplexityModality) -> ComplexityModality:
        """Step one rung down the modality ladder, never below SIMPLE."""
        return _DEGRADATION_LADDER[modality]

    def should_accept(self, stream: StreamName, state: BackpressureState) -> bool:
        """Whether new work may enter ``stream`` under ``state``.

        Control-plane streams are always accepted, whatever the pressure.
        Shedding them would be self-defeating: ORCHESTRATOR_CORE carries the
        messages that drain the backlog, HEARTBEAT_PING is how the watchdog
        distinguishes a busy worker from a dead one, and DEAD_LETTER_POISON is
        where exhausted messages go to *leave* the queue. Refusing any of the
        three would deepen the very backlog the shedding is meant to relieve,
        and the runtime would have no path back to NORMAL.
        """
        if not state.is_shedding or stream in CONTROL_PLANE_STREAMS:
            return True
        log.info("queue.load_shed", stream=stream.value, state=state.value)
        return False

    def plan(self, stream: StreamName, depth: int, modality: ComplexityModality) -> Verdict:
        """One-shot admission decision: assess, then apply both consequences."""
        state = self.assess(depth)
        return Verdict(
            state=state,
            accepted=self.should_accept(stream, state),
            modality=self.degrade_modality(modality) if state.is_degraded else modality,
        )


class Verdict:
    """Outcome of :meth:`BackpressureController.plan`."""

    __slots__ = ("accepted", "modality", "state")

    def __init__(
        self, *, state: BackpressureState, accepted: bool, modality: ComplexityModality
    ) -> None:
        self.state = state
        self.accepted = accepted
        self.modality = modality

    def __repr__(self) -> str:
        return (
            f"Verdict({self.state.value} "
            f"{'accept' if self.accepted else 'shed'} {self.modality.value})"
        )
