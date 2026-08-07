"""Policy and risk gate. RFC §2.1 agent 4, §9.

**No model is ever consulted in this file.** That is the single most important
property here, and it is enforced by construction: this module has no provider
dependency to call. A model deciding "is this action safe?" is a model that can
be argued into yes by content it was asked to evaluate — which is exactly the
prompt-injection path the RFC's threat model (§17.6) is worried about.

Everything below is deterministic: set membership, regex, path arithmetic, and
a cosine threshold. All of it is replayable and auditable months later.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from paa.agents.base import Agent, AgentContext, AgentMessage, AgentResult
from paa.config import PolicySettings, get_settings
from paa.core.types import AgentRole, Permission, PermissionMode

if TYPE_CHECKING:
    from paa.storage.relational.database import Database

__all__ = ["PolicyRiskAgent", "PolicyVerdict"]

log = structlog.get_logger(__name__)


#: Operations that destroy data or are otherwise unrecoverable. Blocked
#: outright under SAFE; gated elsewhere. Written as explicit patterns rather
#: than a "dangerous?" heuristic so the list is reviewable in a diff.
_IRREVERSIBLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\brm\s+-[rRf]{1,2}\b", "recursive force delete"),
    (r"\bshutil\.rmtree\b", "recursive tree delete"),
    (r"\bos\.(remove|unlink|rmdir)\b", "file deletion"),
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "destructive DDL"),
    (r"\bTRUNCATE\s+TABLE\b", "table truncation"),
    (r"\bgit\s+push\s+.*--force\b", "force push"),
    (r"\bgit\s+reset\s+--hard\b", "hard reset"),
    (r"\bmkfs\b|\bdd\s+if=", "raw device write"),
    (r"\bDELETE\s+FROM\s+\w+\s*(;|$)", "unqualified delete"),
)

#: Paths no task may write to, regardless of mode.
_FORBIDDEN_WRITE_ROOTS: tuple[str, ...] = (
    "C:\\Windows",
    "C:\\Program Files",
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/System",
)

_NET_PATTERNS = re.compile(
    r"\b(requests\.|urllib|httpx\.|socket\.|curl\s|wget\s|aiohttp)", re.IGNORECASE
)


def _flatten(value: Any) -> str:
    """Render any plan value as searchable text, argv lists included.

    Recurses through lists/tuples/dicts space-joining their elements, so a
    forbidden token is visible to the pattern scanners regardless of whether the
    planner emitted it as a string, an argv list, or a nested structure.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    return str(value)


class PolicyVerdict(dict[str, Any]):
    """The orchestrator-facing verdict shape.

    A plain dict subclass rather than a model: the orchestrator reads it as a
    dict and it goes straight into a ledger payload, so an extra serialisation
    hop would buy nothing.
    """

    @classmethod
    def approved(cls, *, risk_score: float, gate: bool = False, reason: str = "") -> PolicyVerdict:
        return cls(
            decision="STATUS_APPROVED",
            risk_score=risk_score,
            requires_human_gate=gate,
            anti_goal_match=False,
            reason=reason,
        )

    @classmethod
    def blocked(
        cls, reason: str, *, risk_score: float = 1.0, anti_goal: bool = False
    ) -> PolicyVerdict:
        return cls(
            decision="STATUS_BLOCKED",
            risk_score=risk_score,
            requires_human_gate=False,
            anti_goal_match=anti_goal,
            reason=reason,
        )


class PolicyRiskAgent(Agent):
    """Evaluates a plan before any sandbox boots."""

    role = AgentRole.POLICY_RISK
    can_delegate = False

    def __init__(
        self,
        *,
        db: Database | None = None,
        vector_store: Any = None,
        embedder: Any = None,
        settings: PolicySettings | None = None,
        workspace_root: Path | None = None,
        anti_goals: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._db = db
        self._vectors = vector_store
        self._embedder = embedder
        self._settings = settings or get_settings().policy
        self._workspace_root = workspace_root
        self._anti_goals = anti_goals or []
        self._rule_cache: list[dict[str, Any]] | None = None

    async def handle(self, message: AgentMessage, ctx: AgentContext) -> AgentResult[dict]:
        started = time.perf_counter()
        steps: list[dict[str, Any]] = message.payload.get("steps", [])
        goal: str = message.payload.get("goal", "")
        blob = self._render(goal, steps)

        checks = (
            self._check_permissions(steps, ctx),
            self._check_lockdown(blob, steps, ctx),
            self._check_irreversible(blob, ctx),
            self._check_paths(blob),
            await self._check_rules(blob),
            await self._check_anti_goals(blob),
        )
        for verdict in checks:
            if verdict is not None:
                self._record_latency(started)
                log.warning(
                    "policy.blocked",
                    correlation_id=str(ctx.correlation_id),
                    reason=verdict["reason"],
                )
                return AgentResult.success(verdict, confidence=1.0)

        risk = self._risk_score(steps, blob)
        gate, reason = self._needs_gate(risk, steps, ctx)
        self._record_latency(started)

        return AgentResult.success(
            PolicyVerdict.approved(risk_score=risk, gate=gate, reason=reason),
            confidence=1.0,
        )

    # -- individual checks -------------------------------------------------

    def _check_permissions(
        self, steps: list[dict[str, Any]], ctx: AgentContext
    ) -> PolicyVerdict | None:
        """Every declared permission must be granted by the active mode."""
        required: set[Permission] = set()
        for step in steps:
            for name in step.get("required_permissions", []):
                try:
                    required.add(Permission(name))
                except ValueError:
                    # An unrecognised permission is a refusal, not a shrug —
                    # failing open here would let a typo grant anything.
                    return PolicyVerdict.blocked(f"unknown permission requested: {name!r}")

        if missing := [p.value for p in required if not ctx.grants(p)]:
            return PolicyVerdict.blocked(
                f"permissions not granted in {ctx.permission_mode.value}: {sorted(missing)}"
            )
        return None

    def _check_lockdown(
        self, blob: str, steps: list[dict[str, Any]], ctx: AgentContext
    ) -> PolicyVerdict | None:
        """LOCKDOWN is an air-gap promise, enforced independently of declarations.

        A step that *declares* no network permission but whose command clearly
        makes network calls is still refused. Trusting the declaration alone
        would make the guarantee a matter of the planner's honesty.
        """
        if ctx.permission_mode is not PermissionMode.LOCKDOWN:
            return None
        if _NET_PATTERNS.search(blob):
            return PolicyVerdict.blocked("network egress attempted under LOCKDOWN")
        for step in steps:
            perms = set(step.get("required_permissions", []))
            if {Permission.NET_EGRESS.value, Permission.EXTERNAL_WRITE.value} & perms:
                return PolicyVerdict.blocked("egress/external-write requested under LOCKDOWN")
        return None

    def _check_irreversible(self, blob: str, ctx: AgentContext) -> PolicyVerdict | None:
        """SAFE mode hard-blocks anything unrecoverable."""
        if ctx.permission_mode is not PermissionMode.SAFE:
            return None
        for pattern, label in _IRREVERSIBLE_PATTERNS:
            if re.search(pattern, blob, re.IGNORECASE):
                return PolicyVerdict.blocked(f"irreversible operation under SAFE mode: {label}")
        return None

    def _check_paths(self, blob: str) -> PolicyVerdict | None:
        """Refuse writes to system roots and traversal out of the workspace."""
        for root in _FORBIDDEN_WRITE_ROOTS:
            if root.lower() in blob.lower():
                return PolicyVerdict.blocked(f"write to protected system path: {root}")

        if self._workspace_root is not None:
            for candidate in re.findall(r"[\"']([^\"']*(?:/|\\\\)[^\"']*)[\"']", blob):
                if ".." in candidate:
                    return PolicyVerdict.blocked(f"path traversal in argument: {candidate!r}")
        return None

    async def _check_rules(self, blob: str) -> PolicyVerdict | None:
        """Apply operator-authored rules from ``hot_serving_policy_rules``."""
        for rule in await self._load_rules():
            if rule["rule_kind"] != "regex" or not rule["is_active"]:
                continue
            try:
                if re.search(rule["pattern"], blob, re.IGNORECASE):
                    if rule["severity"] == "block":
                        return PolicyVerdict.blocked(f"policy rule {rule['rule_name']!r} matched")
            except re.error:
                # A malformed operator rule must not crash the gate; log and
                # skip, because failing closed on every task would be worse.
                log.error("policy.malformed_rule", rule=rule["rule_name"])
        return None

    async def _load_rules(self) -> list[dict[str, Any]]:
        if self._rule_cache is not None:
            return self._rule_cache
        if self._db is None:
            self._rule_cache = []
            return self._rule_cache
        rows = await self._db.fetch_all(
            "SELECT rule_name, rule_kind, pattern, severity, threshold, is_active "
            "FROM hot_serving_policy_rules WHERE is_active = 1"
        )
        self._rule_cache = [dict(r) for r in rows]
        return self._rule_cache

    async def _check_anti_goals(self, blob: str) -> PolicyVerdict | None:
        """Semantic match against the user's declared anti-goals. RFC §2.1(4).

        With a vector store this is cosine similarity at the 0.82 threshold.
        Without one it degrades to phrase matching, which is materially weaker
        — it catches restatements of an anti-goal only when they reuse its
        wording. That limitation is logged rather than hidden.
        """
        if not self._anti_goals:
            return None

        if self._vectors is None or self._embedder is None:
            lowered = blob.lower()
            for goal in self._anti_goals:
                if goal.strip() and goal.strip().lower() in lowered:
                    return PolicyVerdict.blocked(
                        f"anti-goal phrase matched: {goal[:60]!r}", anti_goal=True
                    )
            log.debug("policy.anti_goal_keyword_only", reason="no vector store configured")
            return None

        try:
            vectors = await self._embedder.embed([blob, *self._anti_goals])
        except Exception as exc:
            # Embedding failure must not silently disable a security check.
            log.error("policy.anti_goal_embedding_failed", error=str(exc))
            return PolicyVerdict.blocked(
                "anti-goal check unavailable; refusing rather than proceeding unchecked"
            )

        query, goals = vectors[0], vectors[1:]
        for goal_text, goal_vec in zip(self._anti_goals, goals, strict=True):
            similarity = float(query @ goal_vec)  # both are unit vectors
            if similarity >= self._settings.anti_goal_threshold:
                return PolicyVerdict.blocked(
                    f"anti-goal similarity {similarity:.3f} >= "
                    f"{self._settings.anti_goal_threshold}: {goal_text[:60]!r}",
                    anti_goal=True,
                )
        return None

    # -- scoring -----------------------------------------------------------

    def _risk_score(self, steps: list[dict[str, Any]], blob: str) -> float:
        """Coarse risk in [0,1]. Used only to decide gating, never to block."""
        score = 0.0
        for _pattern, _label in _IRREVERSIBLE_PATTERNS:
            if re.search(_pattern, blob, re.IGNORECASE):
                score = max(score, 0.8)
        if _NET_PATTERNS.search(blob):
            score = max(score, 0.5)
        score = max(score, min(1.0, len(steps) / 20.0))
        for step in steps:
            score = max(score, float(step.get("risk_profile", 0.0)))
        return round(min(1.0, score), 3)

    def _needs_gate(
        self, risk: float, steps: list[dict[str, Any]], ctx: AgentContext
    ) -> tuple[bool, str]:
        """Decide whether a human must approve before execution."""
        mode = ctx.permission_mode

        if any(
            float(s.get("risk_profile", 0.0)) >= self._settings.always_gate_risk_profile
            for s in steps
        ):
            return True, "step exceeds the always-gate risk profile"

        if any(s.get("always_human_gate") for s in steps):
            return True, "step is from an agent class that mandates human approval"

        if mode is PermissionMode.SUPERVISED:
            if any(s.get("mutates", True) for s in steps):
                return True, "SUPERVISED mode gates every mutating step"
            return False, ""

        if mode is PermissionMode.ASK:
            if risk >= 0.5:
                return True, f"risk {risk:.2f} exceeds tolerance in ASK mode"
            return False, ""

        if mode is PermissionMode.AUTO:
            confidence = 1.0 - risk
            if confidence < self._settings.auto_confidence_floor:
                return True, (
                    f"confidence {confidence:.2f} below AUTO floor "
                    f"{self._settings.auto_confidence_floor}"
                )
            return False, ""

        return False, ""

    # -- helpers -----------------------------------------------------------

    def _render(self, goal: str, steps: list[dict[str, Any]]) -> str:
        """Flatten a plan into one searchable string.

        Scanning the rendered whole rather than per-field means a payload cannot
        hide a forbidden command in a field the checks forgot to look at.

        Containers (argv lists, nested dicts) are **space-joined**, not
        ``repr``'d. This matters for security: a plan carries a command as an
        argv list like ``["rm", "-rf", "/"]``, and ``repr`` renders that as
        ``['rm', '-rf', '/']`` — the quotes and commas break ``\\brm\\s+-rf``
        and every other shell-shaped pattern, so a dangerous command would slip
        past simply because it arrived pre-tokenised. Space-joining restores it
        to ``rm -rf /``, which the patterns match. The scanner must see the same
        thing whether a command comes as a string or a list.
        """
        parts = [goal]
        for step in steps:
            for value in step.values():
                parts.append(_flatten(value))
        return "\n".join(parts)

    def _record_latency(self, started: float) -> None:
        elapsed_ms = (time.perf_counter() - started) * 1000
        if elapsed_ms > self._settings.latency_budget_ms:
            # Budget breach is logged, never fatal: a slow security check is
            # far better than a skipped one.
            log.warning(
                "policy.latency_budget_exceeded",
                elapsed_ms=round(elapsed_ms, 2),
                budget_ms=self._settings.latency_budget_ms,
            )
