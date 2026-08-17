"""Hiring-signal lead engine (Jesse-format, no data wrapper).

From the customer's URL → derive the "trigger roles" (roles whose hiring signals a company NEEDS
this product) → web-search job postings for those roles → LLM extracts REAL companies grounded in
the actual result URLs → relevance-filter vs the ICP → company-level leads
(company | website | employees | location | source | rationale | relevance), like the Jesse sample.

Correctness bar: every lead's source_url is a real scraped URL (LLM references result index, never
invents); inferred websites are HEAD-checked and blanked if they don't resolve.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from warmgraph.agents.activities.corpus import embed_and_tag_leads, retrieve_company_leads
from warmgraph.agents.activities.feedback import feedback_prompt_block
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.agents.base import Agent
from warmgraph.dates import is_stale
from warmgraph.jsonutil import extract_json
from warmgraph.models import CompanyLead, LeadsReport
from warmgraph.scraper import crawl_site
from warmgraph.search import tavily_search

_JOB_BOARDS = [
    "boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com",
    "apply.workable.com", "linkedin.com", "wellfound.com", "ycombinator.com", "builtin.com",
]

_ROLES_SYSTEM = (
    "You are a GTM strategist. A company sells a product. Identify the JOB ROLES that, when ANOTHER "
    "company is hiring for them, signal that company is a strong prospect (it has the need this "
    "product serves) — the buyer/user/owner roles. Be specific and realistic."
)
_ROLES_SCHEMA = ('Return ONLY JSON: {"product":"(1 line)","segment":"(who they sell to)",'
                 '"trigger_roles":["role title", "..."]}')

_EXTRACT_SYSTEM = (
    "You extract HIRING-signal company leads from web search results (job postings). Rules:\n"
    "- Use ONLY the results shown; 'i' is the result index. NEVER invent a company or URL.\n"
    "- Keep only results that are a REAL job posting for one of the trigger roles AND where the "
    "hiring company is a RELEVANT prospect for our product (right segment). Skip listicles, our own "
    "competitors, staffing agencies, and unrelated roles.\n"
    "- The hiring company is often in the result URL (e.g. boards.greenhouse.io/COMPANY/..., "
    "jobs.lever.co/COMPANY/...) or the title — use that to get the real company name.\n"
    "- For each: company name, the role, a one-line rationale = the specific posting (e.g. 'Active "
    "posting for a Head of Influencer Marketing'), website (infer the company's domain), employees "
    "and location ONLY if evident (else ''), relevance High/Medium/Low. Dedupe by company. "
    "Be generous: any company hiring a trigger role IS a relevant prospect."
)
_EXTRACT_SCHEMA = """Return ONLY JSON:
{"leads":[{"i":0,"company":"","website":"","employees":"","location":"","role":"","rationale":"","date":"","relevance":"High"}]}"""


def _derive_trigger_roles(registry, settings, text: str) -> Dict:
    if not registry.has_llm:
        return {"product": "", "segment": "", "trigger_roles": []}
    user = f"Company website:\n{text[:4000]}\n\n{_ROLES_SCHEMA}"
    d = extract_json(registry.complete("hiring_trigger_roles", _ROLES_SYSTEM, user,
                                       max_tokens=600, want_json=True)) or {}
    return {
        "product": str(d.get("product", "")), "segment": str(d.get("segment", "")),
        "trigger_roles": [str(r) for r in d.get("trigger_roles", []) if r][:5],
    }


def _website_resolves(url: str) -> bool:
    if not url:
        return False
    u = url if "://" in url else "https://" + url
    try:
        r = httpx.head(u, follow_redirects=True, timeout=6.0)
        return r.status_code < 500
    except Exception:
        try:
            r = httpx.get(u, follow_redirects=True, timeout=6.0)
            return r.status_code < 500
        except Exception:
            return False


class HiringLeadsInput(BaseModel):
    url: str
    limit: int = 25
    force: bool = False   # skip the corpus shortcut and scrape fresh (widen coverage)


class HiringLeadsAgent(Agent):
    name = "hiring_leads"
    description = "Companies hiring for roles that signal they need this product → company-level leads (Jesse format: company/website/employees/location/source/rationale/relevance), grounded in real job-posting URLs."
    InputModel = HiringLeadsInput
    OutputModel = LeadsReport

    def run(self, inp: HiringLeadsInput) -> LeadsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        # read the STORED profile (built once) — no re-crawl
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        hc = profile.context("hiring")
        roles = (hc.search_params.get("trigger_roles") if hc else []) or []
        ctx = {"product": profile.profile.what_they_do,
               "segment": (hc.search_params.get("segment", "") if hc else "") or ", ".join(profile.segments),
               "trigger_roles": roles}
        if not roles or not s.has_tavily:
            return LeadsReport(subject_domain=domain, signal_type="hiring", leads=[])

        # 1. RETRIEVE from the SHARED corpus first (semantic) — reuse across customers
        hc_emb = hc.embedding if hc else []
        retrieved = retrieve_company_leads(store, hc_emb, "hiring", is_stale)
        if len(retrieved) >= 5 and not inp.force:
            return LeadsReport(subject_domain=domain, signal_type="hiring",
                               leads=retrieved[: inp.limit])

        # 2. Otherwise SCRAPE the JOB BOARDS directly (real postings → real companies), not the open web
        # (which returns listicles). Each board URL usually encodes the hiring company.
        results: List[dict] = []
        seen = set()
        for role in roles[:6]:
            queries = [
                (role, _JOB_BOARDS),                         # actual postings on ATS boards
                (f"{role} {ctx['segment']}", _JOB_BOARDS),
                (f'"{role}" jobs', _JOB_BOARDS),             # more board coverage per role
                (f'hiring "{role}" {ctx["segment"]}', None),  # open web for extra coverage
            ]
            for q, domains in queries:
                for r in tavily_search(q, s, max_results=14, include_domains=domains):
                    u = r.get("url", "")
                    if u and u not in seen:
                        seen.add(u)
                        results.append(r)

        leads = self._extract(reg, ctx, results, domain)
        # drop any posting we can date to older than ~3 months (active postings are undated → kept)
        leads = [L for L in leads if not is_stale(L.signal_date)]

        # correctness: HEAD-check inferred websites in parallel; blank ones that don't resolve
        def fix(L: CompanyLead) -> CompanyLead:
            if L.website and not _website_resolves(L.website):
                L.website = ""
            return L
        if leads:
            with ThreadPoolExecutor(max_workers=8) as ex:
                leads = list(ex.map(fix, leads))

        leads = leads[: inp.limit]
        # embed + tag, then add to the shared corpus
        leads = embed_and_tag_leads(s, leads, profile.industry, "hiring", domain_of)
        if store is not None and leads:
            store.save_company_leads(leads)
            from warmgraph.storage import mirror
            mirror.dual_write("hiring_leads", mirror.mirror_company_leads, store, leads)
        return LeadsReport(subject_domain=domain, signal_type="hiring", leads=leads)

    def _extract(self, registry, ctx, results: List[dict], domain: str) -> List[CompanyLead]:
        if not results or not registry.has_llm:
            return []
        learned = feedback_prompt_block(self.ctx.store, domain)
        out: List[CompanyLead] = []
        by_company: set = set()
        chunk = 12
        for start in range(0, min(len(results), 132), chunk):
            batch = results[start:start + chunk]
            lines = [f"{i}. {(r.get('title') or '')[:90]} :: {(r.get('content') or '')[:140]} :: {r.get('url','')}"
                     for i, r in enumerate(batch)]
            user = (
                f"Our product: {ctx['product']}\nWe sell to: {ctx['segment']}\n"
                f"Trigger roles: {', '.join(ctx['trigger_roles'])}\n\n"
                f"Search results:\n" + "\n".join(lines) + f"\n\n{_EXTRACT_SCHEMA}" + learned
            )
            d = extract_json(registry.complete("hiring_leads_extract", _EXTRACT_SYSTEM, user,
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
                out.append(CompanyLead(
                    subject_domain=domain, company=company, website=str(item.get("website", "")),
                    employees=str(item.get("employees", "")), location=str(item.get("location", "")),
                    source=urlparse(src_url).netloc.replace("www.", "") if src_url else "web",
                    source_url=src_url, signal_type="hiring", role=str(item.get("role", "")),
                    rationale=str(item.get("rationale", "")), relevance=str(item.get("relevance", "")),
                    signal_date=str(item.get("date", "")),
                ))
        return out
