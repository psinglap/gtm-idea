"""Social Listening agent — LIGHT: 2 LLM calls total.

1) derive_context: one call → category, competitors, ICP pains, short search keywords.
2) scrape every platform in parallel (free) → free keyword pre-filter → ONE classify call keeps
   only relevant posts + the overall rollup. Per-platform view is grouped for free.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List
from urllib.parse import urlparse

from pydantic import BaseModel

from warmgraph.agents.activities.classify import classify_posts, keywords, looks_promo, matches
from warmgraph.agents.activities.prospects import pitch_posts
from warmgraph.llm.embeddings import cosine, get_embedder
from warmgraph.agents.base import Agent
from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import (
    PlatformInsight,
    Post,
    Signal,
    SocialInsights,
    SocialListeningReport,
)
from warmgraph.scraper import crawl_site

_PER_PLATFORM_CANDIDATES = 6   # default keyword-matched posts sent to the classifier per platform
# GitHub is high-signal and now pulls issues+repos+comments — don't starve it at 6.
# HN is free + dense; pull a big batch and rank it by engagement before classifying (see run()).
_PLATFORM_CANDIDATE_OVERRIDES = {"github": 20, "hackernews": 100}
_DEFAULT_DISPLAY = 8           # posts surfaced per platform in the report
_PLATFORM_DISPLAY_OVERRIDES = {"hackernews": 60}
# Platforms with real engagement metrics (points/comments) — rank candidates by them before slicing.
_ENGAGEMENT_RANKED = {"hackernews"}
_CLASSIFY_CAP = 150            # total posts sent to the classifier across all platforms
_RETRIEVE_MIN = 6              # if the shared corpus yields >= this many, skip scraping
_RETRIEVE_THRESHOLD = 0.62     # cosine cutoff for "relevant to this context"
# Experiment switch: force a fresh scrape even when the corpus is warm (set WG_FORCE_SCRAPE=1).
_FORCE_SCRAPE = bool(os.getenv("WG_FORCE_SCRAPE"))


def domain_of(url: str) -> str:
    netloc = urlparse(url if "://" in url else "https://" + url).netloc
    return netloc.replace("www.", "") or url


def _majority(items: List[str]) -> str:
    items = [i for i in items if i]
    return max(set(items), key=items.count) if items else ""


def derive_context(registry: ModelRegistry, settings: Settings, url: str, text: str) -> Dict:
    """ONE LLM call → everything social listening needs to be grounded + targeted."""
    brand = domain_of(url).split(".")[0]
    fallback = {"name": brand, "category": "", "what": "", "competitors": [], "pains": "",
                "queries": [f"{brand} alternative"]}
    if not registry.has_llm:
        return fallback
    user = (
        f"Company website:\n{text[:4000]}\n\n"
        "Return ONLY JSON: {\"name\":\"\",\"category\":\"\",\"what\":\"(1 sentence)\","
        "\"competitors\":[\"real competitor names\"],\"pains\":[\"the ICP's pain-points\"],"
        "\"queries\":[\"5 SHORT search keywords, 2-4 words each, that people type to discuss this "
        "problem — category, 'X alternative', pain phrases\"]}"
    )
    d = extract_json(registry.complete("social_context",
                                       "You profile a company for social listening.", user,
                                       max_tokens=1200, want_json=True)) or {}
    if not d:
        return fallback
    queries = [str(q) for q in d.get("queries", []) if q and len(str(q).split()) <= 5][:5]
    return {
        "name": str(d.get("name", brand)), "category": str(d.get("category", "")),
        "what": str(d.get("what", "")),
        "competitors": [str(c) for c in d.get("competitors", []) if c][:8],
        "pains": "; ".join(str(p) for p in d.get("pains", []) if p)[:400],
        "queries": queries or fallback["queries"],
    }


class SocialInput(BaseModel):
    url: str
    since_days: int = 90      # 3-month fresh window
    limit: int = 50           # per query per platform (pre-filter) — feeds HN's engagement ranking


class SocialListeningAgent(Agent):
    name = "social_listening"
    description = "Relevance-driven social listening (2 LLM calls): per-platform signals (pain-points, competitor mentions+sentiment) about the PROBLEM, keeping only relevant posts."
    InputModel = SocialInput
    OutputModel = SocialListeningReport

    def run(self, inp: SocialInput) -> SocialListeningReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        # read the STORED profile (built once by company_icp) — no re-crawl, no recompute
        profile = self.ctx.get_or_build_profile(inp.url)
        sc = profile.context("social")
        ctx = {
            "name": profile.profile.name, "what": profile.profile.what_they_do,
            "category": profile.profile.category,
            "competitors": [c.name for c in profile.competitive.direct_competitors[:8]],
            "pains": "; ".join(p for x in profile.icp.personas for p in x.pains)[:400],
        }
        queries = (sc.search_params.get("queries") if sc else []) or [f"{profile.profile.category} alternative"]
        domain = profile.domain
        kw = keywords(ctx)

        soc_emb = sc.embedding if sc else []
        overall_d: dict = {}

        # 1. RETRIEVE from the SHARED corpus first (semantic) — reuse across customers, no scraping
        kept: List[Post] = self._retrieve(store, soc_emb, inp.since_days) if soc_emb else []

        # 2. If the corpus is thin, SCRAPE -> classify -> embed -> STORE (grows the shared corpus)
        if len(kept) < _RETRIEVE_MIN or _FORCE_SCRAPE:
            def scrape(scraper):
                try:
                    return scraper.search(queries, inp.since_days, inp.limit, domain)
                except Exception:
                    return []
            scrapers = self.ctx.scrapers
            with ThreadPoolExecutor(max_workers=max(1, len(scrapers))) as ex:
                scraped = list(ex.map(scrape, scrapers))
            candidates: List[Post] = []
            for scraper, posts in zip(scrapers, scraped):
                cap = _PLATFORM_CANDIDATE_OVERRIDES.get(scraper.platform, _PER_PLATFORM_CANDIDATES)
                clean = [p for p in posts if not looks_promo(p)]
                matched = [p for p in clean if matches(p, kw)] or clean
                # keep the most-discussed first so the cap (and the classifier) sees the best signal
                if scraper.platform in _ENGAGEMENT_RANKED:
                    matched.sort(key=lambda p: (p.score, p.num_comments), reverse=True)
                candidates.extend(matched[:cap])
            fresh, overall_d = classify_posts(reg, ctx, candidates, cap=_CLASSIFY_CAP)
            embedder = get_embedder(s)
            for p in fresh:
                p.industry = profile.industry
                if embedder:
                    try:
                        p.embedding = embedder.embed_one(((p.title or "") + " " + (p.text or ""))[:1500])
                    except Exception:
                        pass
            if store is not None and fresh:
                store.save_posts(fresh)
                from warmgraph.storage import mirror
                mirror.dual_write("posts", mirror.mirror_posts, store, fresh)
                store.save_signals([Signal(
                    subject_domain=domain, source=p.platform, type="problem_discussion",
                    strength=p.relevance, recency=p.posted_at, entity_name=p.author,
                    evidence=(p.problem_theme or p.title or p.text or "")[:200], url=p.url, post_id=p.id)
                    for p in fresh])
            kept = kept + fresh

        # annotate genuine-prospect posts with a tailored reply (customer-specific) + persist as
        # customer_leads. Done AFTER the corpus save above, so the shared `posts` stay pitch-free.
        if kept:
            leads = pitch_posts(reg, profile, kept)
            if store is not None and leads:
                store.save_leads(leads)

        # 3. group kept posts by platform → per-platform view
        by_plat: Dict[str, List[Post]] = {}
        for p in kept:
            by_plat.setdefault(p.platform, []).append(p)
        platforms: List[PlatformInsight] = []
        for platform, pp in by_plat.items():
            comp_m: Dict[str, int] = {}
            for p in pp:
                for c in p.competitors_mentioned:
                    comp_m[c] = comp_m.get(c, 0) + 1
            for p in pp:
                p.raw, p.embedding = {}, []  # slim the response payload
            # engagement-ranked platforms (HN) show the most-discussed posts first
            if platform in _ENGAGEMENT_RANKED:
                pp.sort(key=lambda p: (p.score, p.num_comments), reverse=True)
            show = _PLATFORM_DISPLAY_OVERRIDES.get(platform, _DEFAULT_DISPLAY)
            platforms.append(PlatformInsight(
                platform=platform, post_count=len(pp), scanned=len(pp),
                pain_points=list(dict.fromkeys(p.problem_theme for p in pp if p.problem_theme))[:4],
                competitor_mentions=comp_m, sentiment=_majority([p.sentiment for p in pp]),
                posts=pp[:show],
            ))

        overall = self._overall(overall_d, platforms)
        return SocialListeningReport(subject_domain=domain, queries=queries,
                                     platforms=platforms, overall=overall)

    @staticmethod
    def _retrieve(store, query_emb: List[float], since_days: int,
                  k: int = 100) -> List[Post]:
        """Semantic retrieval from the shared corpus (brute-force cosine; pgvector HNSW at scale)."""
        if store is None or not query_emb:
            return []
        recent = store.get_recent_posts(since_days=since_days, limit=800)
        scored = [(cosine(query_emb, p.embedding), p) for p in recent if p.embedding]
        scored = [(c, p) for c, p in scored if c >= _RETRIEVE_THRESHOLD]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:k]]

    @staticmethod
    def _overall(d: dict, platforms: List[PlatformInsight]) -> SocialInsights:
        # programmatic aggregate from per-platform signals (always populated)...
        pains, themes, comp = [], [], {}
        for i in platforms:
            pains += i.pain_points
            for k, v in (i.competitor_sentiment or {}).items():
                comp.setdefault(k, v)
        base_pains = list(dict.fromkeys(pains))[:6]
        base_sent = _majority([i.sentiment for i in platforms if i.post_count > 0])
        # ...enhanced by the single classify call's overall fields when present.
        try:
            score = float(d.get("sentiment_score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        llm_pains = [str(x) for x in d.get("pain_points", []) if x]
        llm_themes = [str(x) for x in d.get("themes", []) if x]
        cs = d.get("competitor_sentiment") or {}
        return SocialInsights(
            problem_sentiment=str(d.get("problem_sentiment") or base_sent),
            sentiment_score=score,
            major_pain_points=llm_pains if len(llm_pains) >= 2 else base_pains,
            trending_themes=llm_themes,
            competitor_sentiment=cs if isinstance(cs, dict) and cs else comp,
            summary=str(d.get("summary", "")),
        )
