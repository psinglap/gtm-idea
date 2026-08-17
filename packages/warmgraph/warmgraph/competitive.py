"""Competitive analysis — its OWN use-case (task `competitive_analysis`), separate from
the company/ICP synthesis so it gets a focused, deeper pass and its own model later.

Produces a full competitive layer: competitors tiered by size vs the company, market
crowdedness, the company's unique advantage + defensible moat + whitespace, how
competitors' targeting differs from where this company should play, pricing positioning,
and where the company lands in the landscape. Grounded with Tavily when available.
"""
from __future__ import annotations

from typing import Any, Dict, List

from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import CompanyProfile, CompetitiveAnalysis, Competitor
from warmgraph.search import web_search

TASK = "competitive_analysis"

_SYSTEM = (
    "You are a senior competitive-intelligence + GTM strategist. Given a company and its "
    "category, map the competitive landscape precisely and realistically. Rules:\n"
    "- Only name REAL companies. List AT LEAST 10 direct competitors plus indirect alternatives "
    "(incl. 'status quo' like spreadsheets/manual).\n"
    "- This is OFTEN an EARLY/seed-stage company, so its real rivals are mostly OTHER startups — NOT "
    "just famous incumbents. USE the 'Competitor discovery' web results below to surface real, "
    "lesser-known and early-stage players ('{name} alternatives' pages, Product Hunt / YC startups). "
    "PREFER names that actually appear in those results over generic big brands you recall from memory.\n"
    "- Span ALL stages and EMPHASIZE the peer/early-stage ones: aim for a spread of 'enterprise "
    "incumbent', 'funded scale-up', and 'peer / similar-stage' (early/seed) competitors.\n"
    "- For EACH competitor set 'tier' RELATIVE TO THE ENTERED COMPANY: 'enterprise incumbent' "
    "| 'funded scale-up' | 'peer / similar-stage' | 'adjacent'; add 'size_note' (funding/stage/"
    "scale if known), who THEY target ('target_customers'), and their 'strengths'/'weaknesses'.\n"
    "- 'crowdedness_score' = 1 (blue ocean) to 5 (saturated); 'market_crowdedness' explains it.\n"
    "- 'our_unique_advantage': what the entered company does better/differently.\n"
    "- 'our_moat': what it can DEFENSIBLY own that incumbents can't easily copy.\n"
    "- 'whitespace': underserved segments/use-cases competitors ignore that this company can win.\n"
    "- 'competitor_targets_vs_ours': contrast who the big competitors chase vs the wedge this "
    "(often earlier-stage) company should target instead.\n"
    "- 'pricing_landscape': infer competitors' pricing posture (enterprise/seat/usage) and this "
    "company's pricing advantage IF inferable from the site; say 'unknown' if not.\n"
    "- 'positioning': one crisp sentence on where this company lands in the landscape.\n"
    "Treat the WEBSITE EXCERPT as the source of truth for what the entered company does. If "
    "web results describe a DIFFERENT company with the same or similar name, IGNORE them — "
    "every competitor must be relevant to the company described in the website excerpt.\n"
    "Do not invent facts; infer conservatively and flag uncertainty."
)

_SCHEMA = """Return ONLY JSON:
{
  "direct_competitors": [{"name":"","url":"","positioning":"","how_they_differ":"","tier":"","size_note":"","target_customers":"","strengths":["..."],"weaknesses":["..."]}],
  "indirect_alternatives": [{"name":"","url":"","positioning":"","tier":"","target_customers":""}],
  "category_landscape": "",
  "market_crowdedness": "", "crowdedness_score": 3,
  "our_unique_advantage": "", "our_moat": "", "whitespace": "",
  "competitor_targets_vs_ours": "", "pricing_landscape": "", "positioning": ""
}"""


def _competitor(c: Dict[str, Any]) -> Competitor:
    return Competitor(
        name=str(c.get("name", "")), url=c.get("url") or None,
        positioning=str(c.get("positioning", "")), how_they_differ=str(c.get("how_they_differ", "")),
        tier=str(c.get("tier", "")), size_note=str(c.get("size_note", "")),
        target_customers=str(c.get("target_customers", "")),
        strengths=[str(x) for x in c.get("strengths", []) if x],
        weaknesses=[str(x) for x in c.get("weaknesses", []) if x],
    )


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _discover_competitors(settings: Settings, profile: CompanyProfile) -> str:
    """Find REAL competitors — incl. early/seed startups — via TARGETED free search (DuckDuckGo).
    '{name} alternatives/competitors' surfaces actual rival lists; '{niche} startup / Product Hunt /
    Y Combinator' surfaces early players. Far better than 'best {category} tools' (which returns big
    listicles). Returns deduped result snippets to ground the LLM in real names."""
    name = (profile.name or "").strip()
    niche = (profile.subcategory or profile.category or "").strip()
    queries: List[str] = []
    if name:
        queries += [f"{name} alternatives", f"{name} competitors"]
    if niche:
        queries += [f"{niche} tools", f"{niche} startup", f"{niche} Product Hunt",
                    f"{niche} Y Combinator"]
    if not queries:
        return ""
    seen: set = set()
    snippets: List[str] = []
    for q in queries:
        for r in web_search(q, settings, max_results=5):
            url = r.get("url", "")
            key = url or r.get("title", "")
            if not key or key in seen:
                continue
            seen.add(key)
            snippets.append(f"- {r.get('title', '')}: {r.get('content', '')[:160]} ({url})")
    return "\n".join(snippets[:20])


def _verify(comp: CompetitiveAnalysis, settings: Settings) -> None:
    for c in (comp.direct_competitors + comp.indirect_alternatives)[:10]:
        results = web_search(c.name, settings, max_results=1)
        if results:
            c.verified = True
            if not c.url:
                c.url = results[0].get("url")


def analyze_competition(
    registry: ModelRegistry, settings: Settings, profile: CompanyProfile, site_text: str,
    verify: bool = True,
) -> CompetitiveAnalysis:
    web = _discover_competitors(settings, profile)
    user = (
        f"Entered company: {profile.name}\nWhat they do: {profile.what_they_do}\n"
        f"Category: {profile.category} / {profile.subcategory}\n"
        f"Value prop: {profile.value_proposition}\nDifferentiation: {profile.differentiation}\n"
        f"Seller stage: {profile.stage}\nPricing: {profile.pricing_model}\n\n"
        f"Competitor discovery (real names from targeted search — prefer these, incl. early/seed):\n"
        f"{web or '(none)'}\n\n"
        f"Website excerpt:\n{site_text[:5000]}\n\n{_SCHEMA}"
    )
    raw = registry.complete(TASK, _SYSTEM, user, max_tokens=6144, want_json=True)
    data = extract_json(raw)
    comp = CompetitiveAnalysis(
        direct_competitors=[_competitor(c) for c in data.get("direct_competitors", []) if c.get("name")],
        indirect_alternatives=[_competitor(c) for c in data.get("indirect_alternatives", []) if c.get("name")],
        category_landscape=str(data.get("category_landscape", "")),
        market_crowdedness=str(data.get("market_crowdedness", "")),
        crowdedness_score=_to_int(data.get("crowdedness_score", 0)),
        our_unique_advantage=str(data.get("our_unique_advantage", "")),
        our_moat=str(data.get("our_moat", "")),
        whitespace=str(data.get("whitespace", "")),
        competitor_targets_vs_ours=str(data.get("competitor_targets_vs_ours", "")),
        pricing_landscape=str(data.get("pricing_landscape", "")),
        positioning=str(data.get("positioning", "")),
    )
    if verify:
        _verify(comp, settings)
    return comp
