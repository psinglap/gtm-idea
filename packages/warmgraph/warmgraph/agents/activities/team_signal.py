"""Team-signal lead engine — the "company already DOES this internally" buying signal.

If another company already employs someone in one of the trigger roles (e.g. a brand with a
"Creator Marketing Manager" on staff), that company is DOING the activity this product serves —
a strong prospect. We detect this at the COMPANY level (not the individual): search LinkedIn /in
profiles + the open web for the trigger-role titles within the buyer segment, then the LLM extracts
the REAL company each title sits at (grounded in the result URL — never invented).

Same shape as hiring_leads/fundraising_leads (company | website | location | source | rationale |
relevance), stored in the SHARED corpus under signal_type='team' so it stacks in customer_list.
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
from warmgraph.dates import is_stale
from warmgraph.jsonutil import extract_json
from warmgraph.models import CompanyLead, LeadsReport
from warmgraph.search import tavily_search

# Where current-role people are publicly visible (profiles + team/about pages).
_PEOPLE_SITES = ["linkedin.com"]

_EXTRACT_SYSTEM = (
    "You extract TEAM-signal company leads from search results (LinkedIn profiles / team pages). The "
    "signal: a company ALREADY EMPLOYS someone in one of the trigger roles, so it already does the "
    "activity our product serves — a strong prospect. Rules:\n"
    "- Use ONLY the results shown; 'i' is the result index. NEVER invent a company or URL.\n"
    "- A LinkedIn result usually reads 'Name - <Role> - <Company> | LinkedIn' — extract the COMPANY "
    "(the employer), NOT the person's name. We only care WHICH company has the role in-house.\n"
    "- Keep only results where the title genuinely matches a trigger role AND the employer is a "
    "RELEVANT prospect for our product (right segment). Skip recruiters, staffing agencies, our own "
    "competitors, job posts (that's a different signal), and unrelated companies.\n"
    "- rationale = the in-house role in words, e.g. 'Has a Creator Marketing Manager on staff'. Capture "
    "the company, inferred website domain, location if evident (else ''), the matched role, relevance "
    "High/Medium/Low. Dedupe by company."
)
_EXTRACT_SCHEMA = """Return ONLY JSON:
{"leads":[{"i":0,"company":"","website":"","location":"","role":"","rationale":"","relevance":"High"}]}"""


class TeamSignalInput(BaseModel):
    url: str
    limit: int = 25
    force: bool = False   # skip the corpus shortcut and scrape fresh (widen coverage)


class TeamSignalAgent(Agent):
    name = "team_signal"
    description = ("Companies that ALREADY EMPLOY the trigger roles in-house (e.g. a Creator Marketing "
                   "Manager on staff) = they already do what this product serves → company-level leads "
                   "(Jesse format), grounded in real LinkedIn/team-page URLs. signal_type='team'.")
    InputModel = TeamSignalInput
    OutputModel = LeadsReport

    def run(self, inp: TeamSignalInput) -> LeadsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        # read the STORED profile (built once) — no re-crawl. Team roles == the hiring trigger roles.
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        hc = profile.context("hiring")
        roles = (hc.search_params.get("trigger_roles") if hc else []) or []
        segment = (hc.search_params.get("segment", "") if hc else "") or ", ".join(profile.segments)
        ctx = {"product": profile.profile.what_they_do, "segment": segment, "trigger_roles": roles}
        if not roles or not (s.has_tavily or True):  # web_search always has a free DDG fallback
            return LeadsReport(subject_domain=domain, signal_type="team", leads=[])

        # 1. RETRIEVE from the SHARED corpus first (semantic, reuse across customers) — team roles
        #    embed like hiring, so reuse the hiring context vector as the query.
        hc_emb = hc.embedding if hc else []
        retrieved = retrieve_company_leads(store, hc_emb, "team", is_stale)
        if len(retrieved) >= 5 and not inp.force:
            return LeadsReport(subject_domain=domain, signal_type="team", leads=retrieved[: inp.limit])

        # 2. Otherwise search PEOPLE (LinkedIn profiles) for current holders of the trigger roles.
        results: List[dict] = []
        seen = set()
        for role in roles[:6]:
            queries = [
                (f'"{role}"', _PEOPLE_SITES),                    # profiles holding the exact title
                (f'"{role}" {segment}', _PEOPLE_SITES),
                (f'"{role}" at', _PEOPLE_SITES),                 # "<role> at <company>" profiles
                (f'"{role}" {segment} team', None),              # open web (team/about pages)
            ]
            for q, domains in queries:
                for r in tavily_search(q, s, max_results=14, include_domains=domains):
                    u = r.get("url", "")
                    if u and u not in seen:
                        seen.add(u)
                        results.append(r)

        leads = self._extract(reg, ctx, results, domain)

        # correctness: HEAD-check inferred websites in parallel; blank ones that don't resolve
        def fix(L: CompanyLead) -> CompanyLead:
            if L.website and not _website_resolves(L.website):
                L.website = ""
            return L
        if leads:
            with ThreadPoolExecutor(max_workers=8) as ex:
                leads = list(ex.map(fix, leads))

        leads = leads[: inp.limit]
        # embed + tag, then add to the shared corpus (embed key mirrors hiring: role + rationale)
        leads = embed_and_tag_leads(s, leads, profile.industry, "hiring", domain_of)
        for L in leads:
            L.signal_type = "team"  # embed_and_tag used the 'hiring' embed key; the TYPE is 'team'
        if store is not None and leads:
            store.save_company_leads(leads)
            from warmgraph.storage import mirror
            mirror.dual_write("team_signal", mirror.mirror_company_leads, store, leads)
        return LeadsReport(subject_domain=domain, signal_type="team", leads=leads)

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
                f"Trigger roles (in-house = a prospect): {', '.join(ctx['trigger_roles'])}\n\n"
                f"Search results:\n" + "\n".join(lines) + f"\n\n{_EXTRACT_SCHEMA}" + learned
            )
            d = extract_json(registry.complete("team_signal_extract", _EXTRACT_SYSTEM, user,
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
                    location=str(item.get("location", "")),
                    source="LinkedIn" if "linkedin.com" in src_url else (
                        urlparse(src_url).netloc.replace("www.", "") if src_url else "web"),
                    source_url=src_url, signal_type="team", role=str(item.get("role", "")),
                    rationale=str(item.get("rationale", "")), relevance=str(item.get("relevance", "")),
                    signal_date="",  # in-house role = current/ongoing (no date → treated as fresh)
                ))
        return out
