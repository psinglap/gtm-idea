"""News scraper — GDELT 2.0 DOC API (free, no key, global news, 3-month timespan)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent
from warmgraph.models import Post

GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def _parse_seendate(s: str):
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


class NewsScraper(ScraperAgent):
    platform = "news"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        # GDELT timespan caps at the freshness window (3m default).
        months = max(1, round(since_days / 30))
        out: List[Post] = []
        for q in queries:
            try:
                r = httpx.get(GDELT, params={
                    "query": q, "mode": "ArtList", "format": "json",
                    "timespan": f"{months}m", "maxrecords": limit, "sort": "DateDesc",
                }, timeout=25.0)
                r.raise_for_status()
                articles = r.json().get("articles", [])
            except Exception:
                continue
            for a in articles:
                out.append(Post(
                    subject_domain=subject_domain, platform="news",
                    external_id=a.get("url") or "", author=a.get("domain") or "",
                    title=a.get("title") or "", text=a.get("title") or "",
                    url=a.get("url") or "", posted_at=_parse_seendate(a.get("seendate", "")),
                    matched_query=q, raw={"domain": a.get("domain"), "lang": a.get("language")},
                ))
        return out
