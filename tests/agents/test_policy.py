"""The policy/risk gate — the deterministic security layer.

Every test asserts a decision the gate must make with **no model in the loop**.
The gate has no model dependency to call, so that property is structural; these
tests prove the decisions themselves are right across the permission modes.

The gate's nine irreversible-operation patterns are covered **one by one**,
each through the gate's own verdict, and the inventory of patterns is read out
of the module's source — so a pattern added to the gate without a command that
trips it cannot land uncovered (R-W2-201).
"""

from __future__ import annotations

import ast
import math
import uuid
from collections.abc import Iterable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import pytest

from paa.agents import policy as policy_module
from paa.agents.base import AgentContext, AgentMessage, MessageType
from paa.agents.policy import _IRREVERSIBLE_PATTERNS, PolicyRiskAgent, _flatten
from paa.config import PolicySettings
from paa.core.types import ComplexityModality, PermissionMode
from paa.storage.relational.connection import TransactionConnection
from paa.storage.relational.dialect import Dialect

# asyncio_mode=auto (pyproject) detects async tests; no module mark needed, and
# a module-level asyncio mark would wrongly tag the sync tests here.


def _ctx(mode: PermissionMode) -> AgentContext:
    return AgentContext(
        correlation_id=uuid.uuid4(),
        modality=ComplexityModality.STANDARD,
        permission_mode=mode,
    )


def _msg(steps: list[dict[str, Any]], goal: str = "do work") -> AgentMessage:
    return AgentMessage(
        task_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        sender="test",
        recipient="policy",
        intent=MessageType.POLICY_CHECK,
        payload={"goal": goal, "steps": steps},
    )


async def _decide(
    agent: PolicyRiskAgent, steps: list[dict[str, Any]], mode: PermissionMode
) -> dict[str, Any]:
    result = await agent.run(_msg(steps), _ctx(mode))
    assert result.ok
    verdict = result.value
    # An ``ok`` result carries a verdict; the gate's verdict is a dict. The
    # narrowing is a check rather than a cast, so the helper's contract is
    # stated where the helper is.
    assert isinstance(verdict, dict), "an ok result carries a verdict"
    return verdict


class TestFlatten:
    """The argv-list blind spot that let a dangerous command slip the scanner."""

    def test_argv_list_becomes_a_shell_string(self) -> None:
        assert _flatten(["rm", "-rf", "/"]) == "rm -rf /"

    def test_nested_structures_flatten(self) -> None:
        assert "secret" in _flatten({"env": {"KEY": "secret"}})

    def test_plain_string_is_unchanged(self) -> None:
        assert _flatten("hello world") == "hello world"


class TestPermissionSubset:
    async def test_ungranted_permission_blocks(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "call out", "required_permissions": ["PERM_NET_EGRESS"]}]
        # LOCKDOWN grants only SANDBOX_RUN.
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_granted_permission_passes(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "edit", "required_permissions": ["PERM_WRITE_HOT"], "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_unknown_permission_is_refused_not_ignored(self) -> None:
        """A typo'd permission must fail closed — never silently grant."""
        agent = PolicyRiskAgent()
        steps = [{"action": "x", "required_permissions": ["PERM_MADE_UP"]}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"


class TestLockdown:
    async def test_network_command_blocked_even_without_declared_permission(self) -> None:
        """LOCKDOWN is an air-gap promise, enforced on the command itself — not
        merely on the declared permission the planner may have omitted."""
        agent = PolicyRiskAgent()
        steps = [{"action": "fetch", "command": ["curl", "https://example.com"]}]
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_local_only_work_is_permitted_in_lockdown(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "compute", "command": ["python", "-c", "print(2+2)"]}]
        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_a_declared_egress_permission_is_refused_by_the_permission_check(
        self,
    ) -> None:
        """The air-gap branch in ``_check_lockdown`` **cannot fire**.

        The branch refuses a plan whose step declares an egress or an
        external-write permission, under LOCKDOWN. It is unreachable:
        ``LOCKDOWN`` grants only ``SANDBOX_RUN``
        (:data:`paa.core.types._MODE_GRANTS`), so
        ``_check_permissions`` refuses such a plan first, and the gate never
        reaches the branch.

        **The refusal is not lost — it is delivered by the other check**, with
        the other check's reason. This test pins that reason, because the
        reason is what the ``POLICY_BLOCKED`` ledger payload carries, and a
        silent change to it would change what a reader of the ledger sees.
        The dead branch is reported in the wave-2 close-out report, with the
        exact proposed change and its consequence; it is not patched here,
        because the change is a **policy** question — *which* refusal should
        win — and not a coverage question.
        """
        agent = PolicyRiskAgent()
        steps = [{"action": "call out", "required_permissions": ["PERM_NET_EGRESS"]}]

        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)

        assert verdict["decision"] == "STATUS_BLOCKED"
        assert verdict["reason"] == "permissions not granted in LOCKDOWN: ['PERM_NET_EGRESS']"
        # And the air-gap refusal is not the one that answered: the branch is
        # unreachable, and this assertion is what says so.
        assert "egress/external-write requested under LOCKDOWN" not in verdict["reason"]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "PolicyRiskAgent._check_lockdown's egress branch is unreachable: "
            "LOCKDOWN grants only SANDBOX_RUN, so _check_permissions refuses a plan "
            "that declares an egress or external-write permission before the branch "
            "is reached. The same plans are refused either way — only the recorded "
            "reason differs — so the fix is a policy decision (which refusal should "
            "win), not a coverage one, and it is reported rather than made silently. "
            "The proposed change is to move _check_lockdown ahead of "
            "_check_permissions in the checks tuple at src/paa/agents/policy.py:156, "
            "which makes the air-gap refusal the one that answers; the consequence "
            "is that the reason recorded in the POLICY_BLOCKED payload changes for a "
            "LOCKDOWN plan that both declares an ungranted permission and trips the "
            "air-gap check. This marker cannot outlive that decision: it fails "
            "(strict) the day the branch starts answering."
        ),
    )
    async def test_the_lockdown_air_gap_refusal_names_the_egress(self) -> None:
        """The proposed end state: the air-gap refusal is the one that answers.

        **This test is not expected to pass today**, and it is not a claim
        that it should. It pins the *proposed* change — the one-line reorder
        that makes the unreachable branch reachable — so that the deferral
        cannot be forgotten and the decision cannot be made silently. If the
        operator decides instead to delete the branch, this test goes with
        it.
        """
        agent = PolicyRiskAgent()
        steps = [{"action": "call out", "required_permissions": ["PERM_NET_EGRESS"]}]

        verdict = await _decide(agent, steps, PermissionMode.LOCKDOWN)

        assert verdict["reason"] == "egress/external-write requested under LOCKDOWN"


class TestSafeMode:
    @pytest.mark.parametrize(
        "command",
        [
            ["rm", "-rf", "/data"],
            ["git", "push", "--force"],
            ["python", "-c", "import shutil; shutil.rmtree('/x')"],
        ],
    )
    async def test_irreversible_ops_are_blocked(self, command: list[str]) -> None:
        agent = PolicyRiskAgent()
        verdict = await _decide(
            agent, [{"action": "danger", "command": command}], PermissionMode.SAFE
        )
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_reversible_work_passes_in_safe(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "write", "command": ["python", "-c", "open('a','w').write('x')"]}]
        verdict = await _decide(agent, steps, PermissionMode.SAFE)
        assert verdict["decision"] == "STATUS_APPROVED"


# ---------------------------------------------------------------------------
# The irreversible-pattern inventory
# ---------------------------------------------------------------------------

#: How many irreversible-operation patterns the gate declares today.
#:
#: Written out by hand, and deliberately so. The identity check below already
#: turns red when the gate gains a pattern that this file has no command for;
#: this number is the second, blunter tripwire. A tenth pattern means a tenth
#: row in the table below *and* an edit to this number, in the same diff — a
#: number that followed the source could never fail.
EXPECTED_PATTERN_COUNT = 9


class _Case(NamedTuple):
    """One pattern's coverage: the label the gate reports, and the commands.

    ``commands`` holds argv lists, because that is how a plan carries a
    command (``{"command": ["rm", "-rf", "/data"]}``) and the gate's renderer
    space-joins an argv list before it scans. A command given as a bare
    string would take a different path through that renderer, so it would not
    be the same test.
    """

    label: str
    commands: tuple[tuple[str, ...], ...]


#: One row per pattern: the command that trips **that** pattern, and no other.
#:
#: The key is the pattern's own regex source, so a row cannot be filed under
#: the wrong rule, and the inventory check compares these keys against the
#: module's source. Every command is checked to trip exactly one pattern —
#: the gate reports the *first* pattern it matches, so a command that matched
#: two would be reported under the earlier label and the test would be
#: asserting about a rule the command does not really belong to.
#:
#: A pattern with an alternation carries a command for **each** branch, not
#: one for the alternation: ``DROP (TABLE|DATABASE|SCHEMA)`` has three, and a
#: test that only ever says ``DROP TABLE`` leaves the other two branches
#: unpinned. The three commands the suite already used are carried here
#: verbatim.
#:
#: A SQL rule carries **both** cases, because a planner writes SQL in lower
#: case as often as in upper case and the gate's matcher ignores case. A
#: matcher that stopped ignoring case would leave the lower-case spelling —
#: the more common one — unrefused, and every other assertion here would
#: still pass.
_COVERED: dict[str, _Case] = {
    r"\brm\s+-[rRf]{1,2}\b": _Case(
        "recursive force delete",
        (
            ("rm", "-rf", "/data"),
            ("rm", "-r", "/data/tree"),
        ),
    ),
    r"\bshutil\.rmtree\b": _Case(
        "recursive tree delete",
        (("python", "-c", "import shutil; shutil.rmtree('/x')"),),
    ),
    r"\bos\.(remove|unlink|rmdir)\b": _Case(
        "file deletion",
        (
            ("python", "-c", "import os; os.remove('/data/x')"),
            ("python", "-c", "import os; os.unlink('/data/x')"),
            ("python", "-c", "import os; os.rmdir('/data/d')"),
        ),
    ),
    r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b": _Case(
        "destructive DDL",
        (
            ("psql", "-c", "DROP TABLE production_users"),
            ("psql", "-c", "DROP DATABASE analytics"),
            ("psql", "-c", "DROP SCHEMA public"),
            ("psql", "-c", "drop table production_users"),
        ),
    ),
    r"\bTRUNCATE\s+TABLE\b": _Case(
        "table truncation",
        (
            ("psql", "-c", "TRUNCATE TABLE events"),
            ("psql", "-c", "truncate table events"),
        ),
    ),
    r"\bgit\s+push\s+.*--force\b": _Case(
        "force push",
        (
            ("git", "push", "--force"),
            ("git", "push", "origin", "main", "--force"),
        ),
    ),
    r"\bgit\s+reset\s+--hard\b": _Case(
        "hard reset",
        (("git", "reset", "--hard", "HEAD~1"),),
    ),
    r"\bmkfs\b|\bdd\s+if=": _Case(
        "raw device write",
        (
            ("mkfs.ext4", "/dev/sda1"),
            ("dd", "if=/dev/zero", "of=/dev/sda"),
        ),
    ),
    r"\bDELETE\s+FROM\s+\w+\s*(;|$)": _Case(
        "unqualified delete",
        (
            ("psql", "-c", "DELETE FROM audit_log;"),
            ("psql", "-c", "DELETE FROM audit_log"),
            ("psql", "-c", "delete from audit_log;"),
        ),
    ),
}


def _assigned_name(target: ast.expr) -> str:
    """The name a ``Assign``/``AnnAssign`` target binds, or ``""``."""
    return target.id if isinstance(target, ast.Name) else ""


def _literal_pairs(value: ast.expr) -> tuple[tuple[str, str], ...]:
    """The ``(pattern, label)`` pairs a tuple literal holds.

    Only literal strings are read. A pattern built at runtime cannot be read
    out of the source, and a sweep that quietly skipped it would report the
    gate's inventory as complete while missing the row that matters — so a
    non-literal is an error, not a skip.
    """
    if not isinstance(value, ast.Tuple):
        raise AssertionError("_IRREVERSIBLE_PATTERNS is not a tuple literal")
    pairs: list[tuple[str, str]] = []
    for element in value.elts:
        if not isinstance(element, ast.Tuple) or len(element.elts) != 2:
            raise AssertionError("_IRREVERSIBLE_PATTERNS holds a row that is not a pair")
        pattern, label = element.elts
        if not isinstance(pattern, ast.Constant) or not isinstance(pattern.value, str):
            raise AssertionError("a pattern is not a literal string")
        if not isinstance(label, ast.Constant) or not isinstance(label.value, str):
            raise AssertionError("a label is not a literal string")
        pairs.append((pattern.value, label.value))
    return tuple(pairs)


def _patterns_in_source() -> tuple[tuple[str, str], ...]:
    """The ``(pattern, label)`` pairs the policy module declares, read from its source.

    The sweep reads the module's own text rather than importing the table, so
    the list this file checks itself against is the module's *declared* list —
    the way ``tests/validation/test_ast_scanner.py`` derives the scanner's
    rule names from the scanner's source. A pattern added to the gate is a
    pattern this helper knows about; what the inventory check then demands is
    that it also has a command that trips it.

    The assignment carries a type annotation, so it is an ``AnnAssign``; a
    plain ``Assign`` is recognised too, so the sweep keeps working if the
    annotation is ever dropped.
    """
    source = Path(policy_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            if _assigned_name(node.target) == "_IRREVERSIBLE_PATTERNS":
                value = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if _assigned_name(target) == "_IRREVERSIBLE_PATTERNS":
                    value = node.value
                    break
        if value is None:
            continue
        return _literal_pairs(value)

    raise AssertionError("_IRREVERSIBLE_PATTERNS is not in the policy module's source")


def _plan(command: tuple[str, ...]) -> list[dict[str, Any]]:
    """The plan the gate is handed for one command."""
    return [{"action": "danger", "command": list(command)}]


def _blob(command: tuple[str, ...]) -> str:
    """The text the gate actually scans for one command.

    Rendered by the gate's own ``_render`` — the same call the agent makes on
    the way to the check — so this helper cannot drift from what the gate
    sees. Re-implementing the join here would be a second copy of the
    rendering rule, and a second copy is a second thing to forget.
    """
    agent = PolicyRiskAgent(settings=PolicySettings())
    return agent._render("do work", _plan(command))


class TestEveryIrreversiblePattern:
    """Every irreversible-operation pattern, exercised through the gate.

    The gate has nine patterns and the ROADMAP's phrase "SAFE blocks
    deletion" is a claim about all nine. Three of them had a test; the other
    six had none, and deleting those six left the suite green — nothing in
    the tree noticed that the gate had stopped refusing to drop a production
    table (R-W2-201).

    Two properties make each case a test of **this** pattern rather than of
    the gate in general:

    * the verdict's *reason* names the pattern's label, and the gate reports
      the **first** pattern it matches — so a command that matched two rules
      would be reported under the earlier label;
    * every command is checked to trip exactly one pattern, which is what
      keeps the first property honest.

    Both are checked against the gate's own source, so a pattern added to the
    module without a command that trips it is a red inventory check rather
    than a silent gap.
    """

    @pytest.mark.parametrize(
        ("pattern", "case"),
        [pytest.param(pattern, case, id=case.label) for pattern, case in _COVERED.items()],
    )
    async def test_the_pattern_refuses_the_command_that_trips_it(
        self, pattern: str, case: _Case
    ) -> None:
        """SAFE refuses the command, and says which rule refused it.

        Asserting the label is what makes this a test of *this* pattern: a
        test that only asserted "blocked" would still pass if the gate had
        blocked the command for some other reason, and it would keep passing
        after this pattern was deleted from the module.
        """
        for command in case.commands:
            agent = PolicyRiskAgent(settings=PolicySettings())
            verdict = await _decide(agent, _plan(command), PermissionMode.SAFE)

            assert verdict["decision"] == "STATUS_BLOCKED", (pattern, command, verdict)
            assert verdict["reason"] == f"irreversible operation under SAFE mode: {case.label}"

    @pytest.mark.parametrize(
        ("pattern", "case"),
        [pytest.param(pattern, case, id=case.label) for pattern, case in _COVERED.items()],
    )
    async def test_the_pattern_raises_the_risk_outside_safe(
        self, pattern: str, case: _Case
    ) -> None:
        """Outside SAFE the command is not blocked, but it is not free either.

        The pattern table feeds the risk score as well as the SAFE refusal,
        and the risk score is what decides whether AUTO mode asks a human. A
        pattern that stops matching takes the score from 0.8 down to the
        step count's share (0.05 for a one-step plan), so the gate stops
        asking — which is why this fails too when the pattern is deleted.
        """
        for command in case.commands:
            agent = PolicyRiskAgent(settings=PolicySettings())
            verdict = await _decide(agent, _plan(command), PermissionMode.AUTO)

            assert verdict["decision"] == "STATUS_APPROVED", (pattern, command, verdict)
            assert verdict["risk_score"] == 0.8, (pattern, command, verdict)
            assert verdict["requires_human_gate"] is True, (pattern, command, verdict)

    def test_the_inventory_is_complete(self) -> None:
        """Every pattern the gate declares has a command that trips it.

        The comparison is between the module's *source* and this file's
        table. A tenth pattern added to the gate is a pattern this table
        does not have, and this check is red until it does — so the next
        pattern cannot land uncovered, which is exactly what happened to the
        six that were missing when this file was written.
        """
        source_pairs = set(_patterns_in_source())
        table_pairs = {(pattern, case.label) for pattern, case in _COVERED.items()}

        assert table_pairs == source_pairs, (
            "the gate's patterns and this file's coverage disagree; "
            f"uncovered: {sorted(source_pairs - table_pairs)}; "
            f"stale: {sorted(table_pairs - source_pairs)}"
        )
        assert len(source_pairs) == EXPECTED_PATTERN_COUNT

    def test_the_sweep_reads_the_table_the_gate_runs(self) -> None:
        """The source sweep finds the table the gate actually uses.

        The inventory check trusts the sweep, so the sweep has to be right: if
        the gate ever built its table in a way the sweep cannot read, the
        sweep would refuse rather than skip — a row that is not a literal
        pair is an error, not a row to overlook — so the inventory check
        could never report "complete" for a gate it had not really read.
        """
        assert _patterns_in_source() == _IRREVERSIBLE_PATTERNS

    def test_each_command_trips_exactly_one_pattern(self) -> None:
        """A command must not trip two rules.

        The gate reports the **first** pattern it matches, so a command that
        matched two would be reported under the earlier label — and the
        per-pattern test above would then be asserting about a rule the
        command does not really belong to. Checking every command against
        every pattern in the module keeps each case honest, and stops a
        future broad pattern from quietly taking over an existing case.

        The match is the **gate's own** matcher
        (``policy._matches``), not a copy of it: a copy would be a second
        place to forget, and the day the gate's rule changed the copy would
        go quietly stale while this check went on reporting "exactly one".
        """
        patterns = _patterns_in_source()
        for pattern, case in _COVERED.items():
            for command in case.commands:
                blob = _blob(command)
                matched = [
                    other for other, _label in patterns if policy_module._matches(other, blob)
                ]
                assert matched == [pattern], (pattern, command, matched)

    async def test_a_qualified_delete_is_not_an_unqualified_delete(self) -> None:
        """The delete rule is about the *absence* of a WHERE clause.

        ``DELETE FROM t WHERE id = 1`` removes one row; the pattern's label
        is "unqualified delete" and that is what it matches. A rule that
        refused every ``DELETE FROM`` would be a different and broader rule,
        and nothing else in the suite would notice the difference.

        This is a **near-miss** case, not one of the nine: it holds while the
        rule is present *and* while it is absent, and it turns red only if
        the rule is broadened.
        """
        agent = PolicyRiskAgent(settings=PolicySettings())
        command = ("psql", "-c", "DELETE FROM audit_log WHERE id = 1")
        verdict = await _decide(agent, _plan(command), PermissionMode.SAFE)

        assert verdict["decision"] == "STATUS_APPROVED", verdict


class TestPathGuards:
    async def test_write_to_system_path_blocked(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "install", "command": ["cp", "x", "C:\\Windows\\System32\\y"]}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"

    async def test_a_path_that_climbs_out_of_the_workspace_is_blocked(self) -> None:
        """The other half of the path guard: traversal, not a system root.

        The forbidden-roots check refuses the system directories; this is the
        branch that refuses a relative path climbing out of the workspace the
        gate was told about. The branch only runs when the gate knows its
        workspace root, so the agent is built with one.

        This is **beyond the nine patterns** — it is the same gate and the
        same class of defect (a branch of the security check that no test
        reached), found while auditing the gate's coverage under mutation.
        """
        agent = PolicyRiskAgent(
            settings=PolicySettings(), workspace_root=Path("/workspace/project")
        )
        steps = [{"action": "write", "command": "cp 'x' '../../other-project/secrets.txt'"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)

        assert verdict["decision"] == "STATUS_BLOCKED", verdict
        assert "traversal" in verdict["reason"], verdict


class TestAntiGoals:
    async def test_keyword_anti_goal_match_blocks(self) -> None:
        """Without a vector store the gate degrades to phrase matching — still
        catches a verbatim anti-goal restatement."""
        agent = PolicyRiskAgent(anti_goals=["exfiltrate customer data"])
        steps = [{"action": "exfiltrate customer data to pastebin"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"
        assert verdict["anti_goal_match"] is True

    async def test_unrelated_goal_passes(self) -> None:
        agent = PolicyRiskAgent(anti_goals=["exfiltrate customer data"])
        steps = [{"action": "write documentation", "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"


class _StubVectorStore:
    """A vector store that is never asked to do anything.

    The anti-goal check asks the gate only whether a vector store is
    *configured* — the similarity itself is computed from the embedder's
    answer — so the store is a presence marker. Passing one is what puts the
    gate on the cosine path instead of the keyword fallback.
    """

    def __bool__(self) -> bool:
        return True


class _StubEmbedder:
    """An embedder that answers with hand-built unit vectors.

    The gate compares the plan against an anti-goal with a **cosine
    similarity**, so the only thing a test needs from an embedder is a
    similarity it can choose. A real embedder cannot be asked for "exactly
    0.82", which is why the threshold's boundary was untested: the only
    anti-goal test in the suite took the keyword fallback, which never reads
    the threshold at all.

    The vectors are unit vectors, and the dot product is **exact**: the query
    is ``[1, 0]`` and the anti-goal is ``[s, sqrt(1 - s²)]``, so the product
    is ``1 * s + 0 * t`` — exactly ``s`` in IEEE-754. The boundary case is
    therefore a boundary case and not a rounding accident.
    """

    def __init__(self, similarity: float) -> None:
        self._similarity = similarity
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[np.ndarray]:
        self.calls.append(list(texts))
        perpendicular = math.sqrt(max(0.0, 1.0 - self._similarity * self._similarity))
        vectors = [np.array([1.0, 0.0])]
        vectors.extend(np.array([self._similarity, perpendicular]) for _ in texts[1:])
        return vectors


class TestAntiGoalCosine:
    """The 0.82 cosine path — the path the threshold actually lives on.

    The keyword fallback catches a verbatim restatement and nothing else.
    The cosine path is the one the RFC describes and the one the threshold
    is read on, and it had no coverage at all.

    **Every test here passes an explicit ``settings=``.** A
    ``PolicyRiskAgent`` built without one reads the *global* settings — the
    repository's ``.env`` and ``~/.paa`` — so a threshold assertion that
    leaned on the default would be environment-dependent, and would quietly
    stop testing the number it names on a machine whose ``.env`` says
    something else.
    """

    ANTI_GOAL = "exfiltrate customer data"
    BENIGN = "write the quarterly report"

    def _agent(self, similarity: float, *, threshold: float = 0.82) -> PolicyRiskAgent:
        return PolicyRiskAgent(
            vector_store=_StubVectorStore(),
            embedder=_StubEmbedder(similarity),
            anti_goals=[self.ANTI_GOAL],
            settings=PolicySettings(anti_goal_threshold=threshold),
        )

    async def test_the_cosine_path_blocks_at_the_threshold(self) -> None:
        """Exactly 0.82 is *at* the threshold, and the threshold blocks.

        The step deliberately does **not** contain the anti-goal phrase, so
        the keyword fallback would not have blocked it: whatever blocks here
        is the cosine.
        """
        agent = self._agent(0.82)
        verdict = await _decide(agent, [{"action": self.BENIGN}], PermissionMode.AUTO)

        assert verdict["decision"] == "STATUS_BLOCKED"
        assert verdict["anti_goal_match"] is True
        # The reason names the computed similarity and the threshold it was
        # compared against — so the number in the verdict is the number the
        # gate actually read.
        assert "similarity 0.820 >= 0.82" in verdict["reason"]

    async def test_just_below_the_threshold_passes(self) -> None:
        """One ten-thousandth below the threshold is not at it."""
        agent = self._agent(0.8199)
        verdict = await _decide(agent, [{"action": self.BENIGN}], PermissionMode.AUTO)

        assert verdict["decision"] == "STATUS_APPROVED"
        assert verdict["anti_goal_match"] is False

    async def test_the_threshold_is_read_from_the_settings(self) -> None:
        """0.82 is not hard-coded into the comparison.

        At a threshold of 0.5 a similarity of 0.6 blocks and 0.4 passes, so
        the gate is reading the configured number rather than a literal.
        """
        blocking = self._agent(0.6, threshold=0.5)
        passing = self._agent(0.4, threshold=0.5)

        assert (
            await _decide(blocking, [{"action": self.BENIGN}], PermissionMode.AUTO)
        )["decision"] == "STATUS_BLOCKED"
        assert (
            await _decide(passing, [{"action": self.BENIGN}], PermissionMode.AUTO)
        )["decision"] == "STATUS_APPROVED"

    async def test_an_embedder_failure_blocks_rather_than_passes(self) -> None:
        """Fail closed.

        A security check that cannot run must not become a security check that
        passes. The gate refuses the plan rather than proceeding unchecked.
        """

        class BrokenEmbedder:
            async def embed(self, texts: list[str]) -> list[np.ndarray]:
                raise RuntimeError("embedding backend is down")

        agent = PolicyRiskAgent(
            vector_store=_StubVectorStore(),
            embedder=BrokenEmbedder(),
            anti_goals=[self.ANTI_GOAL],
            settings=PolicySettings(anti_goal_threshold=0.82),
        )
        verdict = await _decide(agent, [{"action": self.BENIGN}], PermissionMode.AUTO)

        assert verdict["decision"] == "STATUS_BLOCKED"
        assert "unavailable" in verdict["reason"]

    async def test_the_cosine_path_replaces_the_keyword_path(self) -> None:
        """With a vector store configured, the phrase match is not consulted.

        The gate is "cosine at 0.82" or "phrase match", not both — the
        keyword scan is the *degraded* path, used only when there is no
        vector store. This test pins which of the two ran: a plan that
        contains the anti-goal phrase verbatim is judged by the cosine, and a
        cosine of zero lets it through.
        """
        agent = self._agent(0.0)
        verdict = await _decide(agent, [{"action": self.ANTI_GOAL}], PermissionMode.AUTO)

        assert verdict["decision"] == "STATUS_APPROVED"


class TestHumanGating:
    async def test_supervised_gates_every_mutation(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "edit a file", "mutates": True}]
        verdict = await _decide(agent, steps, PermissionMode.SUPERVISED)
        assert verdict["requires_human_gate"] is True

    async def test_supervised_does_not_gate_read_only(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "read a file", "mutates": False}]
        verdict = await _decide(agent, steps, PermissionMode.SUPERVISED)
        assert verdict["requires_human_gate"] is False

    async def test_always_gate_risk_profile_forces_a_gate(self) -> None:
        agent = PolicyRiskAgent()
        steps = [{"action": "risky", "mutates": True, "risk_profile": 0.95}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["requires_human_gate"] is True

    async def test_always_human_gate_flag_forces_a_gate(self) -> None:
        """Communication / robotics specialists carry this flag (RFC §14)."""
        agent = PolicyRiskAgent()
        steps = [{"action": "send email", "mutates": True, "always_human_gate": True}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["requires_human_gate"] is True


class TestStructuralModelFreedom:
    def test_policy_agent_has_no_model_dependency(self) -> None:
        """The security decision cannot call a model because there is none to
        call — RFC §13 made unbreakable by construction."""
        agent = PolicyRiskAgent()
        assert "_model" not in agent.__dict__


class _FakeRuleDB:
    """A relational backend that serves operator rules and nothing else.

    ``_load_rules`` reads ``hot_serving_policy_rules`` with a single
    ``fetch_all`` and turns each row into a plain dict, so one table's worth
    of rows is the whole surface this fake has to offer. The rest of the
    backend is present but inert, because the policy gate is read-only by
    construction: it never writes, never opens a transaction, and never asks
    for a dialect.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.queries: list[str] = []

    @property
    def dialect(self) -> Dialect:
        raise NotImplementedError("the rule loader never asks for a dialect")

    async def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        self.queries.append(sql)
        return list(self._rows)

    async def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> Any | None:
        return None

    async def fetch_value(self, sql: str, params: Sequence[Any] = ()) -> Any:
        return None

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise NotImplementedError("the policy gate never writes")

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        raise NotImplementedError("the policy gate never writes")

    def transaction(self) -> AbstractAsyncContextManager[TransactionConnection]:
        raise NotImplementedError("the policy gate never opens a transaction")

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def table_names(self) -> list[str]:
        return ["hot_serving_policy_rules"]


def _rule(**overrides: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "rule_name": "no-docs",
        "rule_kind": "regex",
        "pattern": "documentation",
        "severity": "block",
        "threshold": 0.0,
        "is_active": 1,
    }
    rule.update(overrides)
    return rule


class TestOperatorRules:
    """The operator-authored rule path (``hot_serving_policy_rules``).

    A matching pattern is only a block when the rule says so: the severity
    is part of the decision, not an afterthought. These tests pin that, and
    pin that a malformed operator rule is skipped rather than being allowed
    to take the gate down with it.
    """

    async def test_matching_block_rule_blocks_the_plan(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule()]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_BLOCKED"
        assert "no-docs" in str(verdict["reason"])

    async def test_non_blocking_severity_does_not_block(self) -> None:
        """The pattern matched; the severity says warn. Nothing is blocked."""
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(severity="warn")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_inactive_rule_is_ignored(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(is_active=0)]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_non_regex_rule_kind_is_ignored(self) -> None:
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(rule_kind="keyword")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"

    async def test_malformed_pattern_is_skipped_not_fatal(self) -> None:
        """A broken operator regex must not take the security gate down."""
        agent = PolicyRiskAgent(db=_FakeRuleDB([_rule(pattern="[unclosed")]))
        steps = [{"action": "write documentation"}]
        verdict = await _decide(agent, steps, PermissionMode.AUTO)
        assert verdict["decision"] == "STATUS_APPROVED"
