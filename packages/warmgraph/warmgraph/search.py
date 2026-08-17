from __future__ import annotations

import html as _html
import re
from typing import Dict, List, Optional

import httpx

from warmgraph.config import Settings

TAVILY_URL = "https://api.tavily.com/search"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS.sub(" ", _TAG.sub(" ", _html.unescape(s or ""))).strip()


def _ddg_unwrap(href: str) -> str:
    """DDG lite sometimes wraps the real URL in a redirect (?uddg=...)."""
    m = re.search(r"uddg=([^&]+)", href)
    return _html.unquote(m.group(1)) if m else href


def _ddg_search(query: str, max_results: int, include_domains: Optional[List[str]]) -> List[Dict]:
    """FREE web search via DuckDuckGo lite — no key, no quota. Replaces exhausted Tavily."""
    if include_domains:
        query = query + " " + " OR ".join(f"site:{d}" for d in include_domains)
    try:
        r = httpx.post(DDG_LITE_URL, data={"q": query}, timeout=20.0,
                       headers={"User-Agent": "Mozilla/5.0"})
        page = r.text
    except Exception:
        return []
    # href and class appear in either order on DDG-lite <a> tags — match the whole tag, then pull href.
    links = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.DOTALL | re.IGNORECASE):
        attrs, title = m.group(1), m.group(2)
        if "result-link" not in attrs:
            continue
        href = re.search(r"href=[\"']([^\"']+)", attrs)
        if href:
            links.append((href.group(1), title))
    snippets = re.findall(
        r"class=[\"']result-snippet[\"'][^>]*>(.*?)</td>", page, re.DOTALL | re.IGNORECASE)
    out: List[Dict] = []
    for i, (href, title) in enumerate(links[:max_results]):
        out.append({
            "title": _clean(title), "url": _ddg_unwrap(href),
            "content": _clean(snippets[i]) if i < len(snippets) else "", "score": 0.0,
        })
    return out


def _tavily_search(query: str, settings: Settings, max_results: int,
                   include_domains: Optional[List[str]], topic: Optional[str],
                   days: Optional[int]) -> Optional[List[Dict]]:
    """Tavily if a working key exists. Returns None on quota/error so the caller can fall back."""
    if not settings.tavily_api_key:
        return None
    payload: dict = {"api_key": settings.tavily_api_key, "query": query,
                     "max_results": max_results, "search_depth": "basic"}
    if include_domains:
        payload["include_domains"] = include_domains
    if topic:
        payload["topic"] = topic
    if days:
        payload["days"] = days
    try:
        resp = httpx.post(TAVILY_URL, json=payload, timeout=30.0)
        if resp.status_code == 432:  # quota exhausted — don't swallow silently
            print("[search] Tavily quota exhausted (432) -> falling back to DuckDuckGo")
            return None
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception:
        return None


def web_search(query: str, settings: Settings, max_results: int = 5,
               include_domains: Optional[List[str]] = None,
               topic: Optional[str] = None, days: Optional[int] = None) -> List[Dict]:
    """Web search with graceful degradation: Tavily if it works, else FREE DuckDuckGo (no key).
    Result items: {title, url, content, score}. include_domains restricts results.
    (DDG ignores topic/days; Tavily uses them for fresh news.)"""
    res = _tavily_search(query, settings, max_results, include_domains, topic, days)
    if res:
        return res
    return _ddg_search(query, max_results, include_domains)


# Back-compat alias — existing callers (competitive/hiring/fundraising) keep working, now with the
# free DuckDuckGo fallback when Tavily is unavailable.
def tavily_search(query: str, settings: Settings, max_results: int = 5,
                  include_domains: Optional[List[str]] = None,
                  topic: Optional[str] = None, days: Optional[int] = None) -> List[Dict]:
    return web_search(query, settings, max_results, include_domains, topic, days)
