"""Planner, critic and the optional router — the model-using agents.

Every agent here works with **no model configured**, falling back to a
deterministic path. That is not a convenience for tests; it is what lets the
runtime keep operating when Ollama is down or an API key expires. A cognitive
OS that stops functioning because a model endpoint is unreachable is not an
operating system.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.core.types import AgentRole, ComplexityModality

__all__ = ["CriticReviewer", "ModelLike", "StrategicPlanner", "TaskRouter"]

log = structlog.get_logger(__name__)


class ModelLike(Protocol):
    """Minimal surface the reasoning agents need from a model provider.

    Deliberately narrow: it keeps these agents decoupled from
    ``paa.models`` and makes a test double a five-line class.
    """

    async def complete_structured(
        self, prompt: str, schema: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]: ...


_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["execution_steps"],
    "properties": {
        "execution_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["index", "action"],
                "properties": {
                    "index": {"type": "integer"},
                    "action": {"type": "string"},
                    "agent": {"type": "string"},
                    "requires": {"type": "array", "items": {"type": "string"}},
                    "mutates": {"type": "boolean"},
                    "risk_profile": {"type": "number"},
                },
            },
        },
        "step_requirements": {"type": "object"},
    },
}


class StrategicPlanner(Agent):
    """Turns a goal plus context into an ordered, budgeted step array.

    RFC §2.1 agent 3. Bounded by ``max_plan_nodes()`` and the modality token
    ceiling; one retry on schema failure, then it gives up and lets the
    orchestrator escalate rather than shipping a malformed plan.
    """

    role = AgentRole.STRATEGIC_PLANNER
    can_delegate = False

    def __init__(self, *, model: ModelLike | None = None, max_retries: int = 1, **kw: Any) -> None:
        super().__init__(**kw)
        self._model = model
        self._max_retries = max_retries

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        if ctx.modality is ComplexityModality.SIMPLE:
            # Defensive: the orchestrator bypasses the planner at SIMPLE.
            return AgentResult.failure("planner must not run at SIMPLE modality")

        goal = message.payload.get("goal", "")
        context = message.payload.get("context", {})
        ceiling = ctx.profile.max_plan_nodes()

        if self._model is None:
            return self._deterministic_plan(goal, ceiling)

        prompt = self._prompt(goal, context, ceiling)
        last_error: str | None = None

        for attempt in range(self._max_retries + 1):
            try:
                raw = await self._model.complete_structured(
                    prompt,
                    _PLAN_SCHEMA,
                    max_tokens=ctx.profile.token_ceiling,
                    # Forward routing context so the model layer can make a
                    # modality-aware escalation decision. A plain provider
                    # ignores these; the escalation router uses them.
                    modality=ctx.modality,
                    permission_mode=ctx.permission_mode,
                    correlation_id=ctx.correlation_id,
                    reason="strategic planning",
                )
                steps = self._normalise(raw.get("execution_steps", []))
                if not steps:
                    raise ValueError("planner returned an empty step array")
                if len(steps) > ceiling:
                    raise ValueError(
                        f"plan has {len(steps)} steps, ceiling is {ceiling} "
                        f"for {ctx.modality.value}"
                    )
                return AgentResult.success(
                    {
                        "execution_steps": steps,
                        "step_requirements": raw.get("step_requirements", {}),
                    },
                    tokens_consumed=int(raw.get("_tokens", 0)),
                    confidence=0.8,
                )
            except Exception as exc:
                last_error = str(exc)
                log.warning("planner.attempt_failed", attempt=attempt, error=last_error)

        return AgentResult.failure(f"planning failed after retries: {last_error}")

    def _deterministic_plan(self, goal: str, ceiling: int) -> AgentResult[dict]:
        """Split a goal on obvious conjunctions. Crude but honest.

        Used when no model is available. It will not decompose well, which is
        exactly why the result carries low confidence — the policy gate and
        critic then apply more scrutiny.
        """
        parts = [p.strip() for p in re.split(r"\b(?:then|and then|;|\n)\b", goal) if p.strip()]
        steps = [
            {"index": i, "action": part, "agent": "worker", "requires": [], "mutates": True}
            for i, part in enumerate(parts[:ceiling])
        ] or [{"index": 0, "action": goal, "agent": "worker", "requires": [], "mutates": True}]

        log.info("planner.deterministic_fallback", steps=len(steps))
        return AgentResult.success(
            {"execution_steps": steps, "step_requirements": {}},
            confidence=0.35,
            telemetry={"deterministic_fallback": True},
        )

    #: Step keys the normaliser coerces to a known type. Everything else on a
    #: step is passed through untouched (see below).
    _COERCED_KEYS = frozenset(
        {"index", "action", "agent", "requires", "mutates", "risk_profile"}
    )

    def _normalise(self, steps: list[Any]) -> list[dict[str, Any]]:
        """Coerce the known fields to their types, and **preserve everything else**.

        Passing extra keys through is a security requirement, not a nicety. A
        step's ``command`` / ``required_permissions`` / ``path`` are exactly
        what the policy agent scans and the worker executes; dropping unknown
        keys here (as an earlier version did) meant a dangerous ``command``
        never reached the policy gate *or* the sandbox — the plan looked benign
        because the teeth had been filed off between planning and review.
        """
        out: list[dict[str, Any]] = []
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or not step.get("action"):
                continue
            passthrough = {k: v for k, v in step.items() if k not in self._COERCED_KEYS}
            out.append(
                {
                    **passthrough,
                    "index": int(step.get("index", i)),
                    "action": str(step["action"]),
                    "agent": str(step.get("agent", "worker")),
                    "requires": list(step.get("requires", [])),
                    "mutates": bool(step.get("mutates", True)),
                    "risk_profile": float(step.get("risk_profile", 0.0)),
                }
            )
        return out

    def _prompt(self, goal: str, context: dict[str, Any], ceiling: int) -> str:
        elements = context.get("elements", [])
        rendered = "\n".join(f"- {e.get('content', '')}" for e in elements[:40])
        return (
            "Decompose the goal into an ordered execution plan.\n"
            f"Emit at most {ceiling} steps. Each step must be independently executable.\n\n"
            f"GOAL:\n{goal}\n\nVERIFIED CONTEXT:\n{rendered or '(none)'}\n\n"
            "Return JSON matching the schema. Do not invent facts absent from the context."
        )


class CriticReviewer(Agent):
    """Reviews a step's output before it may be committed. RFC §2.1 agent 7.

    **The deterministic verdict is authoritative and can only be downgraded.**
    An LLM saying PASS can never overturn an AST rejection or a failing test
    suite. This is RFC §13's "remove untrusted LLM critics from the security
    loop", implemented as an ordering constraint: deterministic checks run
    first, and a FAIL returns immediately without the model being consulted at
    all.
    """

    role = AgentRole.CRITIC
    can_delegate = False

    def __init__(
        self,
        *,
        model: ModelLike | None = None,
        validation_engine: Any = None,
        max_rejections: int = 2,
        **kw: Any,
    ) -> None:
        super().__init__(**kw)
        self._model = model
        self._validator = validation_engine
        self._max_rejections = max_rejections

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        output = message.payload.get("output") or {}
        findings: list[dict[str, Any]] = []

        deterministic_ok = await self._deterministic(output, findings)
        if not deterministic_ok:
            # Short-circuit: the model is never asked. It cannot argue a
            # failing test suite into passing if it is never consulted.
            log.info(
                "critic.deterministic_reject",
                correlation_id=str(ctx.correlation_id),
                findings=len(findings),
            )
            return AgentResult.success(
                {"verdict": "FAIL_REJECT_RETRY", "findings": findings, "source": "deterministic"},
                confidence=1.0,
            )

        if self._model is None:
            return AgentResult.success(
                {"verdict": "PASS", "findings": findings, "source": "deterministic"},
                confidence=0.9,
            )

        try:
            review = await self._model.complete_structured(
                self._prompt(message.payload),
                _REVIEW_SCHEMA,
                max_tokens=2048,
                modality=ctx.modality,
                permission_mode=ctx.permission_mode,
                correlation_id=ctx.correlation_id,
                reason="qualitative review",
            )
            verdict = str(review.get("verdict", "PASS"))
            if verdict not in ("PASS", "FAIL_REJECT_RETRY", "FAIL_ESCALATE"):
                verdict = "FAIL_REJECT_RETRY"  # unparseable review is not a pass
            findings.extend(review.get("findings", []))
            return AgentResult.success(
                {"verdict": verdict, "findings": findings, "source": "hybrid"}, confidence=0.75
            )
        except Exception as exc:
            # A critic outage must not block a deterministically-valid result.
            log.warning("critic.model_unavailable", error=str(exc))
            return AgentResult.success(
                {"verdict": "PASS", "findings": findings, "source": "deterministic_degraded"},
                confidence=0.6,
            )

    async def _deterministic(self, output: dict[str, Any], findings: list[dict]) -> bool:
        """Run host-side checks. Returns False on any hard failure."""
        if self._validator is None:
            return True
        try:
            report = await self._validator.validate(output)
        except Exception as exc:
            findings.append({"rule": "validation_engine", "message": str(exc)})
            return False  # an unusable validator fails closed

        ok = bool(getattr(report, "passed", getattr(report, "ok", True)))
        findings.extend(getattr(report, "findings", []) or [])
        return ok

    def _prompt(self, payload: dict[str, Any]) -> str:
        return (
            "Review this execution output against the step it was meant to satisfy.\n"
            "Deterministic checks have already PASSED; look for semantic problems only.\n\n"
            f"STEP: {payload.get('step_index')}\n"
            f"OUTPUT:\n{json.dumps(payload.get('output', {}), indent=2)[:4000]}\n"
        )


_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL_REJECT_RETRY", "FAIL_ESCALATE"]},
        "findings": {"type": "array", "items": {"type": "object"}},
    },
}


class TaskRouter(Agent):
    """Optional request classifier and decomposer.

    SPEC DEVIATION (docs/adr/0011). The RFC puts a router in front of every
    task. Per explicit user direction it is **optional**, and
    :meth:`should_route` is the bypass:

    * A caller who names a target agent has already routed the task.
    * With fewer than ``min_agents`` eligible, routing is overhead — there is
      no meaningful choice to make.

    When it does run it uses an LLM rather than a small classifier. The user's
    reasoning is right: a compact intent classifier sees the request string but
    not the surrounding project state, so it cannot tell "fix the login bug"
    (one file) from "fix the login bug" (an auth rewrite). A model given the
    context packet can.
    """

    role = AgentRole.ROUTER
    can_delegate = False

    def __init__(
        self, *, model: ModelLike | None = None, min_agents: int = 3, **kw: Any
    ) -> None:
        super().__init__(**kw)
        self._model = model
        self._min_agents = min_agents

    def should_route(self, *, target_agent: str | None, eligible_agents: int) -> bool:
        """Whether routing is worth doing at all."""
        if target_agent:
            return False
        return eligible_agents >= self._min_agents

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        goal = message.payload.get("goal", "")
        candidates: list[str] = list(message.payload.get("eligible_agents", []))

        if self._model is None:
            return AgentResult.success(
                {
                    "modality": ctx.modality.value,
                    "sub_requests": self.decompose_deterministic(goal),
                    "candidate_agents": candidates[:3],
                    "source": "deterministic",
                },
                confidence=0.4,
            )

        try:
            routed = await self._model.complete_structured(
                self._prompt(goal, candidates),
                _ROUTE_SCHEMA,
                max_tokens=1024,
                modality=ctx.modality,
                permission_mode=ctx.permission_mode,
                correlation_id=ctx.correlation_id,
                reason="request routing",
            )
            return AgentResult.success(
                {
                    "modality": routed.get("modality", ctx.modality.value),
                    "sub_requests": routed.get("sub_requests", []) or [goal],
                    "candidate_agents": routed.get("candidate_agents", candidates[:3]),
                    "source": "model",
                },
                confidence=0.75,
            )
        except Exception as exc:
            log.warning("router.model_unavailable", error=str(exc))
            return AgentResult.success(
                {
                    "modality": ctx.modality.value,
                    "sub_requests": self.decompose_deterministic(goal),
                    "candidate_agents": candidates[:3],
                    "source": "deterministic_degraded",
                },
                confidence=0.35,
            )

    @staticmethod
    def decompose_deterministic(goal: str) -> list[str]:
        """Split on explicit conjunctions. Used without a model."""
        parts = [
            p.strip()
            for p in re.split(r"\b(?:then|and then|after that|;|\n)\b", goal)
            if p.strip()
        ]
        return parts or [goal]

    def _prompt(self, goal: str, candidates: list[str]) -> str:
        return (
            "Classify this request and break it into independent sub-requests.\n"
            f"Available agents: {', '.join(candidates) or '(none)'}\n"
            "Choose a modality: SIMPLE, STANDARD, COMPLEX, or MAX.\n\n"
            f"REQUEST:\n{goal}\n"
        )


_ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["modality", "sub_requests"],
    "properties": {
        "modality": {"type": "string", "enum": ["SIMPLE", "STANDARD", "COMPLEX", "MAX"]},
        "sub_requests": {"type": "array", "items": {"type": "string"}},
        "candidate_agents": {"type": "array", "items": {"type": "string"}},
    },
}


class WorkerCell(Agent):
    """Executes one plan step inside a sandbox. RFC §2.1 agent 6.

    Never applies its own patch. The worker returns a diff; the host validates
    and commits it (RFC §14.3). That separation is what makes a compromised
    worker survivable — it can propose a bad change but cannot land one.
    """

    role = AgentRole.WORKER
    can_delegate = True

    def __init__(self, *, sandbox: Any = None, **kw: Any) -> None:
        from paa.core.types import Permission

        super().__init__(**kw)
        self._sandbox = sandbox
        self.required_permissions = (Permission.SANDBOX_RUN,)

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        step = message.payload.get("step", {})
        index = message.payload.get("index", 0)

        if self._sandbox is None:
            return AgentResult.success(
                {"step_index": index, "patch": "", "stdout": "", "dry_run": True},
                telemetry={"no_sandbox_configured": True},
            )

        from paa.sandbox.base import SandboxSpec

        profile = ctx.profile
        spec = SandboxSpec(
            command=list(step.get("command", [])) or ["python", "-c", "pass"],
            workspace_path=str(ctx.metadata.get("workspace_path", ".")),
            env={},  # never inherit the host environment
            memory_mb=profile.memory_mb,
            cpu_cores=profile.cpu_cores,
            timeout_seconds=profile.timeout_seconds or 30.0,
            allow_network=False,
            recursion_depth=ctx.recursion_depth,
            parent_task_id=str(ctx.parent_task_id) if ctx.parent_task_id else None,
        )

        result = await self._sandbox.run(spec)
        if result.timed_out or result.exit_code != 0:
            return AgentResult.failure(
                f"sandbox step {index} failed (exit={result.exit_code}, "
                f"timed_out={result.timed_out})",
                telemetry={
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "peak_rss_mb": result.peak_rss_mb,
                    "stderr": (result.stderr or "")[:2000],
                },
            )

        return AgentResult.success(
            {
                "step_index": index,
                "patch": result.stdout,
                "stdout": result.stdout,
                "exit_code": result.exit_code,
            },
            latency_ms=result.duration_ms,
            telemetry={"peak_rss_mb": result.peak_rss_mb},
        )
