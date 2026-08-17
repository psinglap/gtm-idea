from __future__ import annotations

import re
from typing import Any, Dict, Optional

import httpx

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_HTML_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)", re.IGNORECASE)


def _meta(html: str, key: str, attr: str = "name") -> str:
    """Pull a <meta> content value (handles content before OR after the name/property attr)."""
    for pat in (
        r"<meta[^>]*" + attr + r"=[\"']" + re.escape(key) + r"[\"'][^>]*content=[\"']([^\"']+)",
        r"<meta[^>]*content=[\"']([^\"']+)[\"'][^>]*" + attr + r"=[\"']" + re.escape(key),
    ):
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def extract_structured(html: str) -> str:
    """High-signal company self-description from the HTML head (free, works even on JS SPAs).
    title + meta description + OpenGraph + first JSON-LD — these carry the positioning even when the
    body is a JS shell (validated: serro.ai's 404 shell still yields its one-liner)."""
    parts = []
    t = _TITLE_RE.search(html)
    if t:
        parts.append(f"Title: {t.group(1).strip()}")
    for label, key, attr in (
        ("Description", "description", "name"),
        ("OG title", "og:title", "property"),
        ("OG description", "og:description", "property"),
        ("Site name", "og:site_name", "property"),
    ):
        v = _meta(html, key, attr)
        if v:
            parts.append(f"{label}: {v}")
    ld = re.search(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
    if ld:
        parts.append("JSON-LD: " + _WS_RE.sub(" ", _HTML_RE.sub(" ", ld.group(1)))[:600])
    return "\n".join(parts)


def fetch_page_text(url: str, user_agent: str, max_chars: int = 6000) -> str:
    """Fetch a URL and return a rough plain-text version of the page.

    Intentionally dependency-light (regex strip). Firecrawl can replace this later
    for cleaner extraction; the interface stays the same.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = httpx.get(
            url, headers={"User-Agent": user_agent}, timeout=15.0, follow_redirects=True
        )
    except Exception as exc:  # only a real network failure is unrecoverable
        return f"[could not fetch {url}: {exc}]"
    html = resp.text
    if not html:
        return f"[could not fetch {url}: empty body (HTTP {resp.status_code})]"
    # IGNORE the HTTP status — JS SPAs (e.g. serro.ai) return 404 but serve a full shell whose
    # title/meta still identify the company. Pull the structured head first, then the readable body.
    structured = extract_structured(html)
    body = _HTML_RE.sub(" ", _TAG_RE.sub(" ", html))
    body = _WS_RE.sub(" ", body).strip()
    combined = (structured + "\n\n" + body).strip() if structured else body
    return combined[:max_chars]


def _norm_base(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


# Key pages that usually carry the company's positioning, ICP and pricing.
DEFAULT_PAGES = ["", "/about", "/about-us", "/product", "/products", "/platform",
                 "/pricing", "/customers", "/solutions", "/use-cases", "/blog"]


def fetch_site_pages(
    url: str, user_agent: str, paths: Optional[list] = None, per_page_chars: int = 3500,
    max_total_chars: int = 16000,
) -> str:
    """Fetch several key pages and return labeled, concatenated text for LLM synthesis.

    Best-effort: missing pages (404/redirect to home) are skipped/deduped. Firecrawl can
    replace this later for cleaner extraction behind the same signature.
    """
    base = _norm_base(url)
    paths = paths if paths is not None else DEFAULT_PAGES
    chunks, seen, total = [], set(), 0
    for path in paths:
        page_url = base if path == "" else f"{base}{path}"
        text = fetch_page_text(page_url, user_agent, max_chars=per_page_chars)
        if text.startswith("[could not fetch"):
            continue
        fingerprint = text[:200]
        if fingerprint in seen:  # likely redirected to an already-seen page
            continue
        seen.add(fingerprint)
        label = path or "/ (home)"
        chunks.append(f"=== PAGE {label} ===\n{text}")
        total += len(text)
        if total >= max_total_chars:
            break
    return "\n\n".join(chunks) if chunks else f"[could not fetch any pages for {base}]"


def get_json(
    url: str, headers: Optional[Dict[str, str]] = None, params: Optional[dict] = None,
    timeout: float = 20.0,
) -> Any:
    resp = httpx.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
