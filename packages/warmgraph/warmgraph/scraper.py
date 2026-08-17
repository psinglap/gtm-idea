"""Site crawler with two backends behind one function:
  - Firecrawl (/v1/scrape, markdown, renders JS)  -> when FIRECRAWL_API_KEY is set
  - plain httpx multi-page                         -> fallback (no JS rendering)

Same signature either way, so intelligence/bench don't care which is used.
"""
from __future__ import annotations

from typing import List, Optional

import httpx

from warmgraph.config import Settings
from warmgraph.web import _norm_base, fetch_site_pages

FIRECRAWL_SCRAPE = "https://api.firecrawl.dev/v1/scrape"
# Key pages worth a credit each (Firecrawl bills ~1 credit/page).
_FC_PAGES = ["", "/about", "/pricing", "/product", "/use-cases", "/solutions", "/customers"]


def _firecrawl_scrape(settings: Settings, url: str) -> Optional[str]:
    try:
        resp = httpx.post(
            FIRECRAWL_SCRAPE,
            headers={"Authorization": f"Bearer {settings.firecrawl_api_key}"},
            json={"url": url, "formats": ["markdown"], "onlyMainContent": True},
            timeout=60.0,
        )
        resp.raise_for_status()
        return (resp.json().get("data") or {}).get("markdown")
    except Exception:
        return None


def crawl_site(
    settings: Settings, url: str, paths: Optional[List[str]] = None,
    per_page_chars: int = 6000, max_total_chars: int = 22000,
) -> str:
    """Return labeled, concatenated page text for LLM synthesis."""
    if settings.has_firecrawl:
        base = _norm_base(url)
        pages = paths if paths is not None else _FC_PAGES
        chunks, seen, total = [], set(), 0
        for path in pages:
            page_url = base if path == "" else f"{base}{path}"
            md = _firecrawl_scrape(settings, page_url)
            if not md:
                continue
            md = md[:per_page_chars]
            fingerprint = md[:200]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            chunks.append(f"=== PAGE {path or '/ (home)'} ===\n{md}")
            total += len(md)
            if total >= max_total_chars:
                break
        if chunks:
            return "\n\n".join(chunks)
    # fallback: plain HTTP multi-page (no JS rendering)
    return fetch_site_pages(url, settings.user_agent, paths)
