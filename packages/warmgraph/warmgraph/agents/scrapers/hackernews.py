"""Hacker News scraper — Algolia HN Search API (free, no key)."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import List

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent, since_dt
from warmgraph.models import Post

HN_API = "https://hn.algolia.com/api/v1/search"


class HackerNewsScraper(ScraperAgent):
    platform = "hackernews"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        since_ts = int(since_dt(since_days).timestamp())
        out: List[Post] = []
        for q in queries:
            try:
                r = httpx.get(HN_API, params={
                    "query": q, "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>{since_ts}", "hitsPerPage": limit,
                }, timeout=20.0)
                r.raise_for_status()
                hits = r.json().get("hits", [])
            except Exception:
                continue
            for h in hits:
                oid = str(h.get("objectID", ""))
                text = h.get("title") or h.get("story_text") or h.get("comment_text") or ""
                text = html.unescape(text or "")
                out.append(Post(
                    subject_domain=subject_domain, platform="hackernews", external_id=oid,
                    author=h.get("author") or "", title=html.unescape(h.get("title") or ""),
                    text=text[:2000],
                    url=h.get("url") or f"https://news.ycombinator.com/item?id={oid}",
                    posted_at=datetime.fromtimestamp(h.get("created_at_i", 0), timezone.utc),
                    score=int(h.get("points") or 0), num_comments=int(h.get("num_comments") or 0),
                    matched_query=q, raw=h,
                ))
        return out
