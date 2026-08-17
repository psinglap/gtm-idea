"""Agent registry builder. Registers every scraper agent + every activity agent so the API
(`/agents/{name}`) and MCP can auto-expose all of them."""
from __future__ import annotations

from warmgraph.agents.base import Agent, AgentRegistry

__all__ = ["Agent", "AgentRegistry", "build_agent_registry"]


def build_agent_registry(ctx) -> AgentRegistry:
    from warmgraph.agents.activities import ACTIVITY_AGENT_CLASSES

    reg = AgentRegistry()
    for scraper in getattr(ctx, "scrapers", []):
        reg.register(scraper)
    for cls in ACTIVITY_AGENT_CLASSES:
        reg.register(cls(ctx))
    return reg
