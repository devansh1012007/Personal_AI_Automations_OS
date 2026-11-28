"""Skill execution adapters — one per RFC §8 execution modality.

The shipped modalities are exported here as classes; which class serves a
*name* is the :mod:`paa.skills.adapters.registry`'s word, not a caller's
import list — the registration surface (``register_adapter`` /
``adapter_for`` / entry-point discovery) is re-exported below so a
call site needs only this package to reach it.
"""

from __future__ import annotations

from paa.skills.adapters.base import SecretProvider, SkillAdapter, SkillResult
from paa.skills.adapters.claw_hub import ClawHubAdapter
from paa.skills.adapters.mcp import MCPClient, McpServerAdapter
from paa.skills.adapters.native import NativeAdapter
from paa.skills.adapters.registry import (
    ADAPTER_ENTRY_POINT_GROUP,
    AdapterFactory,
    adapter_for,
    discover_entry_point_adapters,
    register_adapter,
)

__all__ = [
    "ADAPTER_ENTRY_POINT_GROUP",
    "AdapterFactory",
    "ClawHubAdapter",
    "MCPClient",
    "McpServerAdapter",
    "NativeAdapter",
    "SecretProvider",
    "SkillAdapter",
    "SkillResult",
    "adapter_for",
    "discover_entry_point_adapters",
    "register_adapter",
]
