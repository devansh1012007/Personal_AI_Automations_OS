"""Specialist agents. PLAN:integration-fix:P011, ADR-0041 (pilot-first).

Exactly one specialist ships: ``coding`` (see :mod:`paa.agents.specialists.coding`).
The four gated specialists (research/writing/data-analysis/ops) and the five
further deferred RFC §11.8 examples are *not* built — no stubs, no empty
modules — until the pilot's routing-precision gate is met.
"""

from paa.agents.specialists.coding import CODING_TOOL_NAMES, CodingSpecialist

__all__ = ["CODING_TOOL_NAMES", "CodingSpecialist"]
