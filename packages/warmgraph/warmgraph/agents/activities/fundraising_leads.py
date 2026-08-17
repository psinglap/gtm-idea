"""Fundraising-signal lead engine (Jesse-format, no data wrapper).

From the customer's URL → derive the ICP segment (the kind of company that buys this product) →
news/web-search recent funding rounds in that segment → LLM extracts REAL companies grounded in the
actual article URLs → relevance-filter → company-level leads (company | website | location | source |
rationale e.g. "Raised $25M Series C" | relevance), like the Jesse PitchBook/TechCrunch rows.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List
from urllib.parse import urlparse

from pydantic import BaseModel

from warmgraph.agents.activities.corpus import embed_and_tag_leads, retrieve_company_leads
from warmgraph.agents.activities.feedback import feedback_prompt_block
from warmgraph.agents.activities.hiring_leads import _website_resolves
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.agents.base import Agent
from warmgraph.dates import cutoff_str, is_stale, today_str
from warmgraph.jsonutil import extract_json
from warmgraph.models import CompanyLead, LeadsReport
from warmgraph.scraper import crawl_site
from warmgraph.search import tavily_search

# Outlets that actually report funding rounds.
_NEWS = ["techcrunch.com", "axios.com", "businesswire.com", "prnewswire.com", "finsmes.com",
         "eu-startups.com", "tech.eu", "siliconangle.com", "venturebeat.com"]

_CTX_SYSTEM = (
    "A company sells a product. Identify the SEGMENT of companies that BUY it, and SHORT search "
    "keywords for the buyer COMPANY TYPE. Rules for search_terms: 2-4 words each, describe the "
    "company type only (e.g. 'DTC brand', 'consumer goods startup', 'ecommerce brand', 'fintech "
    "startup'). NO years, NO 'funding'/'Series' words, NO extra qualifiers — just the segment type."
)
_CTX_SCHEMA = ('Return ONLY JSON: {"product":"(1 line)","segment":"(the buyer company type)",'
               '"search_terms":["DTC brand", "consumer goods startup", "..."]}')

_EXTRACT_SYSTEM = (
    "You extract FUNDRAISING-signal company leads from news search results. Rules:\n"
    "- Use ONLY the results shown; 'i' is the result index. NEVER invent a company, URL, or round.\n"
    "- Keep only results announcing a REAL funding round by a company that is a RELEVANT prospect for "
    "our product (the right buyer segment). Prefer RECENT rounds. Skip our own competitors, investors, "
    "round-ups that name no single company, and unrelated industries.\n"
    "- rationale = the specific raise in words, e.g. 'Raised $25M Series B in March 2026'. Capture the "
    "company, inferred website domain, location and date if evident (else ''), relevance High/Med/Low. "
    "Dedupe by company."
)
_EXTRACT_SCHEMA = """Return ONLY JSON:
{"leads":[{"i":0,"company":"","website":"","location":"","amount":"","round":"","date":"","rationale":"","relevance":"High"}]}"""


def _derive_ctx(registry, text: str) -> Dict:
    if not registry.has_llm:
        return {"product": "", "segment": "", "search_terms": []}
    d = extract_json(registry.complete("fundraising_ctx", _CTX_SYSTEM,
                                       f"Company website:\n{text[:4000]}\n\n{_CTX_SCHEMA}",
                                       max_tokens=500, want_json=True)) or {}
    return {"product": str(d.get("product", "")), "segment": str(d.get("segment", "")),
            "search_terms": [str(t) for t in d.get("search_terms", []) if t][:3]}


class FundraisingLeadsInput(BaseModel):
    url: str
    limit: int = 25
    force: bool = False   # skip the corpus shortcut and scrape fresh (widen coverage)


class FundraisingLeadsAgent(Agent):
    name = "fundraising_leads"
    description = "Recently-funded companies in the ICP segment (fresh capital = buying signal) → company-level leads (Jesse format), grounded in real funding-news URLs."
    InputModel = FundraisingLeadsInput
    OutputModel = LeadsReport

    def run(self, inp: FundraisingLeadsInput) -> LeadsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        # read the STORED profile (built once) — no re-crawl
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        fc = profile.context("fundraising")
        terms = (fc.search_params.get("segment_terms") if fc else []) or profile.segments
        ctx = {"product": profile.profile.what_they_do,
               "segment": ", ".join(profile.segments) or profile.profile.category}
        if not terms or not s.has_tavily:
            return LeadsReport(subject_domain=domain, signal_type="fundraising", leads=[])

        # 1. RETRIEVE from the SHARED corpus first (semantic) — reuse across customers
        fc_emb = fc.embedding if fc else []
        retrieved = retrieve_company_leads(store, fc_emb, "fundraising", is_stale)
        if len(retrieved) >= 5 and not inp.force:
            return LeadsReport(subject_domain=domain, signal_type="fundraising",
                               leads=retrieved[: inp.limit])

        # 2. Otherwise Tavily NEWS topic + a 100-day window = only recent funding news (real recency).
        results: List[dict] = []
        seen = set()
        for term in terms[:5]:
            for q in (f"{term} raised funding round", f"{term} Series A OR Series B OR seed funding",
                      f"{term} startup raises million", f"{term} closes round"):
                for r in tavily_search(q, s, max_results=15, topic="news", days=100):
                    u = r.get("url", "")
                    if u and u not in seen:
                        seen.add(u)
                        results.append(r)

        leads = self._extract(reg, ctx, results, domain)
        # STRICT freshness: drop any round we can date to older than ~3 months.
        leads = [L for L in leads if not is_stale(L.signal_date)]

        def fix(L: CompanyLead) -> CompanyLead:
            if L.website and not _website_resolves(L.website):
                L.website = ""
            return L
        if leads:
            with ThreadPoolExecutor(max_workers=8) as ex:
                leads = list(ex.map(fix, leads))

        leads = leads[: inp.limit]
        # embed + tag, then add to the shared corpus
        leads = embed_and_tag_leads(s, leads, profile.industry, "fundraising", domain_of)
        if store is not None and leads:
            store.save_company_leads(leads)
            from warmgraph.storage import mirror
            mirror.dual_write("fundraising_leads", mirror.mirror_company_leads, store, leads)
        return LeadsReport(subject_domain=domain, signal_type="fundraising", leads=leads)

    def _extract(self, registry, ctx, results: List[dict], domain: str) -> List[CompanyLead]:
        if not results or not registry.has_llm:
            return []
        learned = feedback_prompt_block(self.ctx.store, domain)
        out: List[CompanyLead] = []
        by_company: set = set()
        for start in range(0, min(len(results), 132), 12):
            batch = results[start:start + 12]
            lines = [f"{i}. {(r.get('title') or '')[:90]} :: {(r.get('content') or '')[:150]} :: {r.get('url','')}"
                     for i, r in enumerate(batch)]
            user = (f"Our product: {ctx['product']}\nBuyer segment: {ctx['segment']}\n"
                    f"Today is {today_str()}. ONLY include funding rounds from the LAST 3 MONTHS "
                    f"(announced on/after {cutoff_str()}); EXCLUDE anything older. Always fill 'date'.\n\n"
                    f"News results:\n" + "\n".join(lines) + f"\n\n{_EXTRACT_SCHEMA}" + learned)
            d = extract_json(registry.complete("fundraising_extract", _EXTRACT_SYSTEM, user,
                                               max_tokens=2000, want_json=True)) or {}
            for item in d.get("leads", []):
                try:
                    src = batch[int(item.get("i"))]
                except (TypeError, ValueError, IndexError):
                    continue
                company = str(item.get("company", "")).strip()
                if not company or company.lower() in by_company:
                    continue
                by_company.add(company.lower())
                src_url = src.get("url", "")
                amount, rnd = str(item.get("amount", "")), str(item.get("round", ""))
                rationale = str(item.get("rationale", "")) or " ".join(x for x in [
                    "Raised", amount, rnd] if x).strip()
                out.append(CompanyLead(
                    subject_domain=domain, company=company, website=str(item.get("website", "")),
                    location=str(item.get("location", "")),
                    source=urlparse(src_url).netloc.replace("www.", "") if src_url else "news",
                    source_url=src_url, signal_type="fundraising", rationale=rationale,
                    relevance=str(item.get("relevance", "")), signal_date=str(item.get("date", "")),
                ))
        return out
