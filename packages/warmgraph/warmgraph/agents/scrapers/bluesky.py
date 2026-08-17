"""Bluesky scraper — public AT-protocol AppView (free, NO auth, no proxies).

`app.bsky.feed.searchPosts` on the public AppView (public.api.bsky.app) returns matching posts
without a login. Bluesky posts carry a real handle + display name, so they're far more
company-attributable than anonymous forum posts — good fuel for the social buying signal."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent, since_dt
from warmgraph.models import Post

SEARCH_API = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"


def _parse_ts(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for cand in (s, s.split(".")[0] + "+00:00" if "." in s else s):
        try:
            dt = datetime.fromisoformat(cand)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class BlueskyScraper(ScraperAgent):
    platform = "bluesky"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        cutoff = since_dt(since_days)
        out: List[Post] = []
        seen: set = set()
        for q in queries:
            try:
                r = httpx.get(SEARCH_API, params={"q": q, "limit": min(limit, 100), "sort": "latest"},
                              headers={"User-Agent": "warmgraph-bot/0.1"}, timeout=20.0)
                r.raise_for_status()
                posts = r.json().get("posts", [])
            except Exception:
                continue
            for p in posts:
                uri = p.get("uri", "")
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                author = p.get("author", {}) or {}
                handle = author.get("handle", "")
                rec = p.get("record", {}) or {}
                posted = _parse_ts(rec.get("createdAt", ""))
                if posted and posted < cutoff:
                    continue
                rkey = uri.rsplit("/", 1)[-1]
                url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""
                out.append(Post(
                    subject_domain=subject_domain, platform="bluesky", external_id=uri,
                    author=author.get("displayName") or handle, author_handle=handle,
                    text=(rec.get("text") or "")[:2000], url=url, posted_at=posted,
                    score=int(p.get("likeCount") or 0), num_comments=int(p.get("replyCount") or 0),
                    matched_query=q, raw=p,
                ))
        return out
