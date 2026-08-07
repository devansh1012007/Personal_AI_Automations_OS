"""Skill execution adapters — one per RFC §8 execution modality."""

from __future__ import annotations

from paa.skills.adapters.base import SecretProvider, SkillAdapter, SkillResult
from paa.skills.adapters.claw_hub import ClawHubAdapter
from paa.skills.adapters.mcp import MCPClient, McpServerAdapter
from paa.skills.adapters.native import NativeAdapter

__all__ = [
    "ClawHubAdapter",
    "MCPClient",
    "McpServerAdapter",
    "NativeAdapter",
    "SecretProvider",
    "SkillAdapter",
    "SkillResult",
]
