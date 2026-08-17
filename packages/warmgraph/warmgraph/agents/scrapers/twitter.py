"""Twitter / X scraper. Default path = Tavily search restricted to x.com/twitter.com (free-ish,
uses our existing Tavily key — no Apify). If APIFY_TOKEN is set, uses an Apify actor instead
(more structured, pay-per-result). X is locked down, so the free path is lower-volume — honest."""
from __future__ import annotations

from datetime import datetime
from typing import List
from urllib.parse import urlparse

from warmgraph.agents.scrapers.apify import first, run_actor
from warmgraph.agents.scrapers.base import ScraperAgent
from warmgraph.models import Post
from warmgraph.search import tavily_search


def _handle_from_url(url: str) -> str:
    try:
        parts = urlparse(url).path.strip("/").split("/")
        h = parts[0] if parts else ""
        return "" if h in ("i", "search", "hashtag", "home", "") else h
    except Exception:
        return ""


class TwitterScraper(ScraperAgent):
    platform = "twitter"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        s = self.ctx.settings
        if s.apify_token:
            posts = self._via_apify(s, queries, limit, subject_domain)
            if posts:
                return posts
        return self._via_search(s, queries, limit, subject_domain)

    def _via_search(self, s, queries, limit, subject_domain) -> List[Post]:
        out: List[Post] = []
        seen: set = set()
        for q in queries:
            for r in tavily_search(q, s, max_results=limit,
                                   include_domains=["x.com", "twitter.com"]):
                url = r.get("url", "")
                # keep only real tweets (/status/), not profile/listing pages
                if not url or url in seen or "/status/" not in url:
                    continue
                seen.add(url)
                handle = _handle_from_url(url)
                out.append(Post(
                    subject_domain=subject_domain, platform="twitter", external_id=url,
                    author=handle, author_handle=(f"@{handle}" if handle else ""),
                    title=(r.get("title", "") or "")[:200],
                    text=(r.get("content", "") or "")[:2000], url=url, matched_query=q,
                    raw={"via": "tavily"},
                ))
        return out

    def _via_apify(self, s, queries, limit, subject_domain) -> List[Post]:
        out: List[Post] = []
        for q in queries:
            try:
                items = run_actor(s.apify_token, s.apify_twitter_actor,
                                  {"searchTerms": [q], "maxItems": limit, "sort": "Latest"})
            except Exception:
                continue
            for it in items:
                author = it.get("author") or {}
                handle = first(it, "username") or author.get("userName", "")
                out.append(Post(
                    subject_domain=subject_domain, platform="twitter",
                    external_id=str(first(it, "id", "id_str", "tweetId")),
                    author=author.get("name", "") or first(it, "name"), author_handle=handle,
                    text=first(it, "text", "full_text")[:2000],
                    url=first(it, "url", "twitterUrl") or (f"https://x.com/{handle}" if handle else ""),
                    score=int(first(it, "likeCount", default=0) or 0), matched_query=q,
                    raw={"via": "apify"},
                ))
        return out
