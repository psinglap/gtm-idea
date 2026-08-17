"""Competitive Intelligence agent. quick = single-pass landscape. deep = parallel
per-competitor dossiers + strategic frameworks (Five Forces / moats) + ICP/winning-category."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import List

from pydantic import BaseModel

from warmgraph.agents.activities.icp import derive_icp
from warmgraph.agents.base import Agent
from warmgraph.competitive import analyze_competition
from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import (
    CompanyProfile,
    Competitor,
    CompetitiveAnalysis,
    CompetitiveIntelligenceReport,
    CompetitorDossier,
    Frameworks,
)
from warmgraph.profile import derive_profile
from warmgraph.scraper import crawl_site

_DOSSIER_TASK = "competitor_dossier"
_DOSSIER_SYSTEM = (
    "You are a competitive-intelligence analyst. Produce a tight, factual dossier on ONE competitor "
    "relative to our company. Infer pricing/GTM conservatively from the site; say 'unknown' if unclear. "
    "Do not invent funding or news."
)
_DOSSIER_SCHEMA = """Return ONLY JSON:
{"summary":"","pricing":"","gtm":"","target_customers":"","strengths":["..."],
 "weaknesses":["..."],"recent_news":["..."],"sentiment_summary":""}"""

_FW_TASK = "frameworks"
_FW_SYSTEM = (
    "You are a strategy consultant. Given a company and its competitive landscape, produce Porter's "
    "Five Forces, the company's defensible moats, a one-line positioning map, and crisp where-to-play "
    "/ how-to-win calls. Be specific and realistic."
)
_FW_SCHEMA = """Return ONLY JSON:
{"five_forces":{"rivalry":"","new_entrants":"","substitutes":"","buyer_power":"","supplier_power":""},
 "moats":["..."],"positioning_map":"","where_to_play":"","how_to_win":""}"""


def _build_dossier(registry: ModelRegistry, settings: Settings, profile: CompanyProfile,
                   c: Competitor) -> CompetitorDossier:
    site = ""
    if c.url and settings.has_firecrawl:
        try:
            site = crawl_site(settings, c.url, paths=["", "/pricing"])[:4000]
        except Exception:
            site = ""
    user = (
        f"Our company: {profile.name} ({profile.category}).\n"
        f"Competitor: {c.name} ({c.url or 'url unknown'})\n"
        f"Known: positioning={c.positioning}; size={c.size_note}; targets={c.target_customers}\n"
        f"Competitor site excerpt:\n{site or '(none)'}\n\n{_DOSSIER_SCHEMA}"
    )
    d = extract_json(registry.complete(_DOSSIER_TASK, _DOSSIER_SYSTEM, user,
                                       max_tokens=1400, want_json=True)) or {}
    return CompetitorDossier(
        name=c.name, url=c.url, summary=str(d.get("summary", "")), pricing=str(d.get("pricing", "")),
        gtm=str(d.get("gtm", "")), target_customers=str(d.get("target_customers", c.target_customers)),
        strengths=[str(x) for x in d.get("strengths", []) if x] or c.strengths,
        weaknesses=[str(x) for x in d.get("weaknesses", []) if x] or c.weaknesses,
        recent_news=[str(x) for x in d.get("recent_news", []) if x],
        sentiment_summary=str(d.get("sentiment_summary", "")),
    )


def _build_frameworks(registry: ModelRegistry, settings: Settings, profile: CompanyProfile,
                      comp: CompetitiveAnalysis) -> Frameworks:
    names = ", ".join(c.name for c in comp.direct_competitors[:8])
    user = (
        f"Company: {profile.name} — {profile.what_they_do}\nCategory: {profile.category}\n"
        f"Stage: {profile.stage}\nCompetitors: {names}\nCrowdedness: {comp.market_crowdedness}\n"
        f"Our advantage: {comp.our_unique_advantage}\nMoat: {comp.our_moat}\n"
        f"Whitespace: {comp.whitespace}\n\n{_FW_SCHEMA}"
    )
    d = extract_json(registry.complete(_FW_TASK, _FW_SYSTEM, user, max_tokens=1600, want_json=True)) or {}
    ff = d.get("five_forces", {})
    return Frameworks(
        five_forces=ff if isinstance(ff, dict) else {},
        moats=[str(x) for x in d.get("moats", []) if x],
        positioning_map=str(d.get("positioning_map", "")),
        where_to_play=str(d.get("where_to_play", "")), how_to_win=str(d.get("how_to_win", "")),
    )


def run_ci(registry: ModelRegistry, settings: Settings, store, url: str,
           depth: str = "quick") -> CompetitiveIntelligenceReport:
    text = crawl_site(settings, url)
    profile = derive_profile(registry, settings, url, text)
    competitive = analyze_competition(registry, settings, profile, text)
    report = CompetitiveIntelligenceReport(
        url=url, depth=depth, profile=profile, competitive=competitive,
        source=registry.provider_name,
    )
    if depth == "deep" and registry.has_llm:
        comps: List[Competitor] = competitive.direct_competitors[:6]
        with ThreadPoolExecutor(max_workers=6) as ex:
            report.dossiers = list(ex.map(
                lambda c: _build_dossier(registry, settings, profile, c), comps))
        report.frameworks = _build_frameworks(registry, settings, profile, competitive)
        report.icp = derive_icp(registry, settings, profile, competitive)
    if store is not None:
        store.save_ci_report(report)
    return report


class CIInput(BaseModel):
    url: str
    depth: str = "quick"  # "quick" | "deep"


class CompetitiveIntelligenceAgent(Agent):
    name = "competitive_intelligence"
    description = "Deep competitive intelligence from a URL: competitors, per-competitor dossiers, Five Forces/moats, ICP + winning-category (depth='deep')."
    InputModel = CIInput
    OutputModel = CompetitiveIntelligenceReport

    def run(self, inp: CIInput) -> CompetitiveIntelligenceReport:
        # read the STORED profile (CI + ICP built once); add deep dossiers/frameworks on top
        reg, s, store = self.ctx.registry, self.ctx.settings, self.ctx.store
        p = self.ctx.get_or_build_profile(inp.url)
        report = CompetitiveIntelligenceReport(
            url=inp.url, depth=inp.depth, profile=p.profile, competitive=p.competitive,
            icp=p.icp, source=p.source,
        )
        if inp.depth == "deep" and reg.has_llm:
            comps = p.competitive.direct_competitors[:6]
            with ThreadPoolExecutor(max_workers=6) as ex:
                report.dossiers = list(ex.map(
                    lambda c: _build_dossier(reg, s, p.profile, c), comps))
            report.frameworks = _build_frameworks(reg, s, p.profile, p.competitive)
        if store is not None:
            store.save_ci_report(report)
        return report
