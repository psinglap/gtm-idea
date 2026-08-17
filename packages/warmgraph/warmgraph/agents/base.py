"""Agent framework — every product action and every scraper is an Agent with a uniform
contract, so the REST API and the MCP server can auto-expose ALL of them (each can be made
publicly available on its own later).

`ctx` passed to each agent is the WarmgraphService — it gives the agent the store, the model
registry, the settings, and the other agents (so agents compose).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Type

from pydantic import BaseModel


class Agent:
    name: str = "agent"
    description: str = ""
    InputModel: Type[BaseModel] = BaseModel
    OutputModel: Type[BaseModel] = BaseModel

    def __init__(self, ctx):
        self.ctx = ctx  # WarmgraphService

    def run(self, inp: BaseModel) -> BaseModel:  # pragma: no cover - overridden
        raise NotImplementedError


class AgentRegistry:
    """name -> Agent. The API exposes `POST /agents/{name}` and the MCP server exposes each
    agent as a tool by iterating this registry."""

    def __init__(self):
        self._agents: Dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        self._agents[agent.name] = agent

    def get(self, name: str) -> Optional[Agent]:
        return self._agents.get(name)

    def names(self) -> List[str]:
        return list(self._agents)

    def describe(self) -> List[dict]:
        out = []
        for a in self._agents.values():
            out.append({
                "name": a.name,
                "description": a.description,
                "input_schema": a.InputModel.model_json_schema(),
            })
        return out

    def run(self, name: str, payload: Optional[dict] = None) -> dict:
        agent = self._agents.get(name)
        if agent is None:
            raise KeyError(name)
        inp = agent.InputModel.model_validate(payload or {})
        out = agent.run(inp)
        return out.model_dump(mode="json")
