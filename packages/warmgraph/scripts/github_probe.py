#!/usr/bin/env python3
"""GitHub visibility probe — see exactly what GitHub offers for ONE already-profiled URL.

Runs the expanded GitHubScraper for a company's stored CI/ICP search queries and prints the
RAW pool (issues / PRs / repos / comments) with NO trimming, classify, embed, or corpus write.
This is the "what does GitHub have for this ICP" experiment — cheap, no LLM calls.

    python scripts/github_probe.py https://serro.ai
    python scripts/github_probe.py https://serro.ai --since-days 365 --limit 200 --samples 30

Add GITHUB_TOKEN to .env first for deep pagination (30 req/min); works unauth but shallower.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from warmgraph.service import WarmgraphService
from warmgraph.agents.activities.classify import keywords, matches, looks_promo


def _ctx_from_profile(profile) -> dict:
    return {
        "name": profile.profile.name,
        "what": profile.profile.what_they_do,
        "category": profile.profile.category,
        "competitors": [c.name for c in profile.competitive.direct_competitors[:8]],
        "pains": "; ".join(p for x in profile.icp.personas for p in x.pains)[:400],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe raw GitHub coverage for one profiled URL.")
    ap.add_argument("url")
    ap.add_argument("--since-days", type=int, default=180)
    ap.add_argument("--limit", type=int, default=120, help="issues to pull per query")
    ap.add_argument("--samples", type=int, default=25, help="sample rows to print")
    args = ap.parse_args()

    svc = WarmgraphService()
    gh = next((s for s in svc.scrapers if getattr(s, "platform", "") == "github"), None)
    if gh is None:
        print("No GitHub scraper registered.", file=sys.stderr)
        return 1

    profile = svc.get_or_build_profile(args.url)
    sc = profile.context("social")
    queries = (sc.search_params.get("queries") if sc else []) \
        or [f"{profile.profile.category} alternative"]
    ctx = _ctx_from_profile(profile)
    kw = keywords(ctx)
    authed = bool(getattr(svc.settings, "github_token", None))

    print("=" * 78)
    print(f"GitHub probe — {profile.profile.name}  ({profile.domain})")
    print(f"category : {profile.profile.category}")
    print(f"auth     : {'GITHUB_TOKEN set (30 req/min)' if authed else 'UNAUTH (10 req/min, shallow)'}")
    print(f"since    : last {args.since_days} days   |   limit/query: {args.limit}")
    print(f"queries  : {queries}")
    print("=" * 78)

    posts = gh.search(queries, args.since_days, args.limit, profile.domain)

    if not posts:
        print("\n*** EMPTY — GitHub returned nothing. Check token / rate limit / queries. ***")
        return 0

    by_kind = Counter(p.raw.get("kind", "?") for p in posts)
    by_query = Counter(p.matched_query for p in posts)
    comments_total = sum(int(p.raw.get("comments_fetched", 0)) for p in posts)
    relevant = [p for p in posts if matches(p, kw) and not looks_promo(p)]

    print(f"\nRAW POOL: {len(posts)} unique items")
    print(f"  by facet : " + ", ".join(f"{k}={v}" for k, v in by_kind.most_common()))
    print(f"  comments fetched (top issues): {comments_total}")
    print(f"  keyword-relevant (cheap filter): {len(relevant)}/{len(posts)}")
    print("\n  per query:")
    for q in queries:
        print(f"    {by_query.get(q, 0):>4}  {q}")

    n = max(1, args.samples // 3)

    def show(label, kinds, unit):
        rows = sorted((p for p in posts if p.raw.get("kind") in kinds),
                      key=lambda x: x.score, reverse=True)[:n]
        print(f"\n{label}  (top {len(rows)} by {unit}):")
        print("-" * 78)
        for p in rows:
            rel = "✓" if (matches(p, kw) and not looks_promo(p)) else " "
            c = f" +{p.raw['comments_fetched']}c" if p.raw.get("comments_fetched") else ""
            print(f"[{rel}] {unit[:4]}={p.score:<6} {(p.title or '')[:60]}{c}")
            print(f"      @{p.author}  {p.url}   (q: {p.matched_query})")

    # Builders / adjacent tools, then the pain-points (issues), then commitment (PRs).
    show("🛠  REPOS — people building adjacent/similar tools", {"repo"}, "stars")
    show("🔥 ISSUES — pain-points & feature gaps", {"issue"}, "reactions")
    show("✅ PRs — commitment / integrations", {"pr"}, "reactions")

    # One real comment thread snippet, so the frustration is visible inline.
    withc = sorted((p for p in posts if p.raw.get("comments_fetched")),
                   key=lambda x: x.num_comments, reverse=True)
    if withc:
        top = withc[0]
        snippet = (top.text.split("--- comments ---", 1)[-1] or "").strip()[:600]
        print(f"\n💬 SAMPLE COMMENT THREAD — {top.title[:60]!r} ({top.url})")
        print("-" * 78)
        print("   " + snippet.replace("\n", "\n   "))
    print("-" * 78)
    print(f"\nNext: tune --since-days / --limit / the profile's queries based on signal density above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
