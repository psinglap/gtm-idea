"""GitHub scraper — high-coverage pull via the official API (free; 10 req/min unauth,
30 with GITHUB_TOKEN). Three facets, all high-signal for a CI/ICP experiment:

  * issues/PRs  — devs describe real problems + feature gaps (pain-points, "seeking_solution")
  * repositories — people *building* adjacent/competing tools, and similar repos = intent
  * issue comments — where the frustration actually lives (fetched for the top issues only)

REST `/search/issues` does NOT include GitHub Discussions (those need the GraphQL
`search(type:DISCUSSION)` endpoint) — left as a future add. Pagination + light rate-limit
backoff let one run pull hundreds of items per query instead of ~30.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Dict, List, Optional

import httpx

from warmgraph.agents.scrapers.base import ScraperAgent, since_dt
from warmgraph.models import Post

GH_ISSUES = "https://api.github.com/search/issues"
GH_REPOS = "https://api.github.com/search/repositories"

MAX_QUERIES = 6          # how many of the profile's search queries to use (was hard 3)
PER_PAGE = 100           # GitHub max page size for search
MAX_PAGES = 3            # up to PER_PAGE*MAX_PAGES issues per query (Search caps at 1000 total)
REPOS_PER_QUERY = 30     # adjacent/similar repos to pull per query
COMMENT_TOP_K = 15       # only the highest-signal issues get their comment thread fetched
COMMENTS_PER_ISSUE = 30  # comments appended per issue (newest of)
BODY_CHARS = 4000        # richer body for classification (was 2000)


def _parse(dt: str):
    try:
        return datetime.fromisoformat(dt.replace("Z", "+00:00"))
    except Exception:
        return None


def _get(url: str, headers: Dict, params: Optional[Dict] = None,
         timeout: float = 20.0) -> Optional[httpx.Response]:
    """GET with a single rate-limit-aware retry. Returns the response or None. Honors
    `Retry-After` / `X-RateLimit-Reset` but never sleeps longer than ~30s (experiment-friendly)."""
    for attempt in range(2):
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except Exception:
            return None
        if r.status_code == 200:
            return r
        if r.status_code in (403, 429) and attempt == 0:
            wait = 0.0
            ra = r.headers.get("Retry-After")
            if ra and ra.isdigit():
                wait = float(ra)
            elif r.headers.get("X-RateLimit-Remaining") == "0":
                try:
                    wait = float(r.headers.get("X-RateLimit-Reset", "0")) - time.time()
                except ValueError:
                    wait = 0.0
            if 0 < wait <= 30:
                time.sleep(wait + 0.5)
                continue
            return None  # rate-limited longer than we'll wait, or a hard 403/422
        return None
    return None


def _topics(it: Dict) -> List[str]:
    return [str(t) for t in (it.get("topics") or []) if t]


# --- comment noise filter (bots + CLA/command boilerplate) — keep only human signal ---
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_NOISE_SUBSTR = (
    "i have read the cla", "signed the cla", "all contributors have signed",
    "auto-generated comment", "summarize by coderabbit", "coderabbit.ai",
    "you have reached your rate limits", "review skipped", "finishing touches",
    "cla assistant", "posted by the", "i hereby sign",
)


def _clean_comment_body(body: str) -> str:
    return _HTML_COMMENT.sub("", body or "").strip()


def _is_noise_comment(login: str, body: str) -> bool:
    """True for bot comments and CLA/command boilerplate — i.e. everything that is NOT human signal."""
    if login.endswith("[bot]"):
        return True
    b = body.lower()
    if not b:
        return True
    if any(s in b for s in _NOISE_SUBSTR):
        return True
    # command-only comment, e.g. "@codex review", "@coderabbitai full review"
    if b.startswith("@") and len(b.split()) <= 4:
        return True
    return False


class GitHubScraper(ScraperAgent):
    platform = "github"

    def search(self, queries, since_days, limit, subject_domain="") -> List[Post]:
        s = self.ctx.settings
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": getattr(s, "user_agent", "warmgraph-bot")}
        authed = bool(getattr(s, "github_token", None))
        if authed:
            headers["Authorization"] = f"Bearer {s.github_token}"
        # unauth search is 10 req/min — pace ourselves so a multi-query run survives
        pace = 0.0 if authed else 6.5
        since = since_dt(since_days).strftime("%Y-%m-%d")

        out: List[Post] = []
        seen: set = set()  # dedup by external_id across queries/facets

        def add(p: Post) -> None:
            if p.external_id and p.external_id in seen:
                return
            if p.external_id:
                seen.add(p.external_id)
            out.append(p)

        for q in list(queries)[:MAX_QUERIES]:
            self._issues(q, since, limit, headers, pace, subject_domain, add)
            self._repos(q, since, headers, pace, subject_domain, add)

        self._attach_comments(out, headers, pace)
        return out

    # --- issues + PRs ---------------------------------------------------------
    def _issues(self, q, since, limit, headers, pace, domain, add) -> None:
        target = max(int(limit), 1)   # issues to pull for this query (probe passes a big limit)
        got = 0
        for page in range(1, MAX_PAGES + 1):
            r = _get(GH_ISSUES, headers, params={
                "q": f"{q} in:title,body created:>{since}",
                "sort": "reactions", "order": "desc",
                "per_page": min(PER_PAGE, target), "page": page,
            })
            if pace:
                time.sleep(pace)
            if r is None:
                break
            items = r.json().get("items", [])
            if not items:
                break
            for it in items:
                kind = "pr" if it.get("pull_request") else "issue"
                add(Post(
                    subject_domain=domain, platform="github",
                    external_id=str(it.get("id", "")), author=(it.get("user") or {}).get("login", ""),
                    title=it.get("title") or "", text=(it.get("body") or "")[:BODY_CHARS],
                    url=it.get("html_url") or "", posted_at=_parse(it.get("created_at", "")),
                    score=int((it.get("reactions") or {}).get("total_count", 0) or 0),
                    num_comments=int(it.get("comments") or 0), matched_query=q,
                    raw={"kind": kind, "repo": it.get("repository_url", ""),
                         "comments_url": it.get("comments_url", "")},
                ))
                got += 1
            if len(items) < min(PER_PAGE, target) or got >= target:
                break


    # --- repositories (people building adjacent / similar tools) --------------
    def _repos(self, q, since, headers, pace, domain, add) -> None:
        r = _get(GH_REPOS, headers, params={
            "q": f"{q} pushed:>{since}", "sort": "stars", "order": "desc",
            "per_page": REPOS_PER_QUERY,
        })
        if pace:
            time.sleep(pace)
        if r is None:
            return
        for it in r.json().get("items", []):
            topics = _topics(it)
            desc = it.get("description") or ""
            text = (desc + (" — topics: " + ", ".join(topics) if topics else ""))[:BODY_CHARS]
            add(Post(
                subject_domain=domain, platform="github",
                external_id=f"repo:{it.get('id','')}", author=(it.get("owner") or {}).get("login", ""),
                title=it.get("full_name") or it.get("name") or "", text=text,
                url=it.get("html_url") or "", posted_at=_parse(it.get("pushed_at", "")),
                score=int(it.get("stargazers_count", 0) or 0),
                num_comments=int(it.get("open_issues_count", 0) or 0), matched_query=q,
                raw={"kind": "repo", "topics": topics, "forks": it.get("forks_count", 0)},
            ))

    # --- comments on the top issues (where the frustration lives) ------------
    def _attach_comments(self, posts: List[Post], headers, pace) -> None:
        issues = [p for p in posts if p.raw.get("kind") in ("issue", "pr")
                  and p.raw.get("comments_url") and p.num_comments > 0]
        issues.sort(key=lambda p: (p.score + p.num_comments), reverse=True)
        for p in issues[:COMMENT_TOP_K]:
            r = _get(p.raw["comments_url"], headers,
                     params={"per_page": COMMENTS_PER_ISSUE, "sort": "created", "direction": "desc"})
            if pace:
                time.sleep(pace)
            if r is None:
                continue
            bits, dropped = [], 0
            for c in r.json():
                who = (c.get("user") or {}).get("login", "")
                body = _clean_comment_body(c.get("body") or "")
                if _is_noise_comment(who, body):
                    dropped += 1
                    continue
                bits.append(f"[{who}] {body[:800]}")
            if bits:
                extra = "\n\n--- comments ---\n" + "\n".join(bits)
                p.text = (p.text + extra)[:BODY_CHARS * 2]
            p.raw["comments_fetched"] = len(bits)   # human comments kept
            p.raw["comments_dropped"] = dropped     # bots / CLA / command spam removed
