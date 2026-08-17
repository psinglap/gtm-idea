"""Reddit scraper — no Apify. Reddit blocks public .json from datacenter IPs (403), so on a
server we need either:
  (1) a free Reddit OAuth app (REDDIT_CLIENT_ID/SECRET) -> oauth.reddit.com, works from servers; or
  (2) Tavily domain-search on reddit.com (fallback, uses our Tavily key).
Picks relevant subreddits first (LLM) for the OAuth path."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent, since_dt
from warmgraph.jsonutil import extract_json
from warmgraph.models import Post
from warmgraph.search import tavily_search

_DEFAULTS = ["startups", "SaaS", "Entrepreneur", "smallbusiness", "marketing", "sales"]


class RedditScraper(ScraperAgent):
    platform = "reddit"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        s = self.ctx.settings
        if s.reddit_client_id and s.reddit_client_secret:
            posts = self._via_oauth(s, queries, since_days, limit, subject_domain)
            if posts:
                return posts
        return self._via_tavily(s, queries, limit, subject_domain)

    # --- (1) free Reddit OAuth app: works from datacenter IPs ---
    def _token(self, s):
        r = httpx.post("https://www.reddit.com/api/v1/access_token",
                       auth=(s.reddit_client_id, s.reddit_client_secret),
                       data={"grant_type": "client_credentials"},
                       headers={"User-Agent": s.user_agent}, timeout=15.0)
        r.raise_for_status()
        return r.json().get("access_token")

    def _subreddits(self, queries: List[str]) -> List[str]:
        reg = getattr(self.ctx, "registry", None)
        if not reg or not reg.has_llm:
            return _DEFAULTS
        user = ("Topics: " + "; ".join(queries[:5]) + "\nList 6 ACTIVE subreddit names (no 'r/'). "
                'Return ONLY JSON: {"subreddits":["..."]}')
        d = extract_json(reg.complete("reddit_subreddits", "You know Reddit communities.", user,
                                      max_tokens=200, want_json=True)) or {}
        return [str(x).strip().lstrip("r/").strip("/") for x in d.get("subreddits", []) if x] or _DEFAULTS

    def _via_oauth(self, s, queries, since_days, limit, subject_domain) -> List[Post]:
        try:
            token = self._token(s)
        except Exception:
            return []
        if not token:
            return []
        hdr = {"Authorization": f"Bearer {token}", "User-Agent": s.user_agent}
        since = since_dt(since_days)
        out, seen = [], set()
        for sub in self._subreddits(queries)[:6]:
            for q in queries[:3]:
                try:
                    r = httpx.get(f"https://oauth.reddit.com/r/{sub}/search",
                                  params={"q": q, "restrict_sr": 1, "sort": "new", "t": "year",
                                          "limit": limit}, headers=hdr, timeout=20.0)
                    r.raise_for_status()
                    children = r.json().get("data", {}).get("children", [])
                except Exception:
                    continue
                for child in children:
                    d = child.get("data", {})
                    pid = str(d.get("id", ""))
                    if pid in seen:
                        continue
                    created = datetime.fromtimestamp(d.get("created_utc", 0) or 0, timezone.utc)
                    if created < since:
                        continue
                    seen.add(pid)
                    out.append(Post(
                        subject_domain=subject_domain, platform="reddit", external_id=pid,
                        author=d.get("author") or "", author_handle=f"u/{d.get('author', '')}",
                        title=d.get("title") or "", text=(d.get("selftext") or "")[:2000],
                        url="https://www.reddit.com" + (d.get("permalink") or ""), posted_at=created,
                        score=int(d.get("score") or 0), num_comments=int(d.get("num_comments") or 0),
                        matched_query=q, raw={"subreddit": sub}))
        return out

    # --- (2) Tavily fallback (no Reddit creds) ---
    def _via_tavily(self, s, queries, limit, subject_domain) -> List[Post]:
        out, seen = [], set()
        for q in queries:
            for r in tavily_search(q, s, max_results=limit, include_domains=["reddit.com"]):
                url = r.get("url", "")
                if not url or url in seen or "/comments/" not in url:
                    continue
                seen.add(url)
                out.append(Post(
                    subject_domain=subject_domain, platform="reddit", external_id=url,
                    title=(r.get("title", "") or "")[:200], text=(r.get("content", "") or "")[:2000],
                    url=url, matched_query=q, raw={"via": "tavily"}))
        return out
