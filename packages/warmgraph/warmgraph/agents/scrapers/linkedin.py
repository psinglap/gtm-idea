"""LinkedIn scraper. Default path = Tavily search restricted to linkedin.com (free-ish, uses our
existing Tavily key — no Apify). Public posts only. If APIFY_TOKEN is set, uses an Apify actor
instead. Public LinkedIn posts are indexed by search engines, so the free path works reasonably;
ToS-sensitive — public data only, never logged-in automation."""
from __future__ import annotations

from typing import List

from warmgraph.agents.scrapers.apify import first, run_actor
from warmgraph.agents.scrapers.base import ScraperAgent
from warmgraph.models import Post
from warmgraph.search import tavily_search


def _author_from_title(title: str) -> str:
    # Tavily titles look like "Jane Doe on LinkedIn: <post>"
    for sep in (" on LinkedIn", " | LinkedIn", " - LinkedIn"):
        if sep in title:
            return title.split(sep)[0].strip()
    return ""


class LinkedInScraper(ScraperAgent):
    platform = "linkedin"

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
            for r in tavily_search(q, s, max_results=limit, include_domains=["linkedin.com"]):
                url = r.get("url", "")
                if not url or url in seen or "/posts/" not in url and "/pulse/" not in url:
                    continue
                seen.add(url)
                title = r.get("title", "") or ""
                out.append(Post(
                    subject_domain=subject_domain, platform="linkedin", external_id=url,
                    author=_author_from_title(title), title=title[:200],
                    text=(r.get("content", "") or "")[:2000], url=url, matched_query=q,
                    raw={"via": "tavily"},
                ))
        return out

    def _via_apify(self, s, queries, limit, subject_domain) -> List[Post]:
        out: List[Post] = []
        for q in queries:
            try:
                items = run_actor(s.apify_token, s.apify_linkedin_actor,
                                  {"keywords": q, "maxItems": limit, "postedLimit": "past-month"})
            except Exception:
                continue
            for it in items:
                author = it.get("author") or {}
                out.append(Post(
                    subject_domain=subject_domain, platform="linkedin",
                    external_id=str(first(it, "id", "urn", "postId")),
                    author=author.get("name", "") or first(it, "authorName"),
                    text=first(it, "text", "content")[:2000],
                    url=first(it, "url", "postUrl", "link"), matched_query=q, raw={"via": "apify"},
                ))
        return out
