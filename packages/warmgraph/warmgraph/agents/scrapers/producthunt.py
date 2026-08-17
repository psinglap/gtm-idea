"""Product Hunt scraper — GraphQL API v2 (needs PRODUCTHUNT_TOKEN). Fetches recent launches
and keeps those whose name/tagline match a query term. Returns [] without a token."""
from __future__ import annotations

from datetime import datetime
from typing import List

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent, since_dt
from warmgraph.models import Post

PH_API = "https://api.producthunt.com/v2/api/graphql"
_QUERY = """
query($after: DateTime){ posts(order: NEWEST, first: 50, postedAfter: $after){
  edges{ node{ id name tagline url votesCount commentsCount createdAt
    topics{ edges{ node{ name } } } user{ name username } } } } }
"""


class ProductHuntScraper(ScraperAgent):
    platform = "producthunt"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        token = getattr(self.ctx.settings, "producthunt_token", None)
        if not token:
            return []
        after = since_dt(since_days).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = httpx.post(PH_API, headers={"Authorization": f"Bearer {token}"},
                           json={"query": _QUERY, "variables": {"after": after}}, timeout=25.0)
            r.raise_for_status()
            edges = r.json().get("data", {}).get("posts", {}).get("edges", [])
        except Exception:
            return []
        terms = [q.lower() for q in queries]
        out: List[Post] = []
        for e in edges:
            n = e.get("node", {})
            hay = f"{n.get('name','')} {n.get('tagline','')}".lower()
            if terms and not any(t in hay for t in terms):
                continue
            try:
                posted = datetime.fromisoformat(n.get("createdAt", "").replace("Z", "+00:00"))
            except Exception:
                posted = None
            user = n.get("user") or {}
            out.append(Post(
                subject_domain=subject_domain, platform="producthunt", external_id=str(n.get("id", "")),
                author=user.get("name") or "", author_handle=user.get("username") or "",
                title=n.get("name") or "", text=n.get("tagline") or "", url=n.get("url") or "",
                posted_at=posted, score=int(n.get("votesCount") or 0),
                num_comments=int(n.get("commentsCount") or 0),
                matched_query=next((q for q in queries if q.lower() in hay), ""), raw=n,
            ))
            if len(out) >= limit:
                break
        return out
