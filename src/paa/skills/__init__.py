"""Skills subsystem — the Unified Skill Adapter runtime (RFC §8).

Layers, bottom to top:

* :mod:`paa.skills.contracts` — the ``SkillContract`` a capability declares.
* :mod:`paa.skills.secrets` — the host-side secret broker and proxy.
* :mod:`paa.skills.registry` — the ``hot_serving_skill_registry`` index.
* :mod:`paa.skills.adapters` — one execution adapter per provider.
* :mod:`paa.skills.usa` — the dispatch state machine that ties them together.
"""

from __future__ import annotations

from paa.skills.contracts import SkillContract, SkillInvocation, SkillResult
from paa.skills.registry import SkillRegistry
from paa.skills.usa import UnifiedSkillAdapter

__all__ = [
    "SkillContract",
    "SkillInvocation",
    "SkillRegistry",
    "SkillResult",
    "UnifiedSkillAdapter",
]
