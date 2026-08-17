"""Scraper-agent base — uniform `search(queries, since_days, limit) -> Post[]`. Each platform
gets its own subclass/module. run() wraps search() so a scraper is also a first-class Agent
(callable via /agents/{name} + MCP)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List

from pydantic import BaseModel, Field

from warmgraph.agents.base import Agent
from warmgraph.models import Post


class SearchInput(BaseModel):
    queries: List[str] = Field(default_factory=list)  # problem / category / competitor terms
    subject_domain: str = ""
    since_days: int = 90   # 3-month freshness window
    limit: int = 30        # max posts per query


class PostList(BaseModel):
    platform: str = ""
    posts: List[Post] = Field(default_factory=list)


def since_dt(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


class ScraperAgent(Agent):
    platform: str = ""
    InputModel = SearchInput
    OutputModel = PostList

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"scrape_{self.platform}"

    @property
    def description(self) -> str:  # type: ignore[override]
        return f"Scrape recent {self.platform} posts/comments matching the queries (last N days)."

    def search(self, queries: List[str], since_days: int, limit: int,
               subject_domain: str = "") -> List[Post]:
        raise NotImplementedError

    def run(self, inp: SearchInput) -> PostList:
        try:
            posts = self.search(inp.queries, inp.since_days, inp.limit, inp.subject_domain)
        except Exception:
            posts = []
        return PostList(platform=self.platform, posts=posts)
