"""`company_icp` — builds + STORES the whole customer profile once per URL, reused by every agent.

profile = company + competitive intelligence + ICP/winning-category + the 3 signal contexts
(social/hiring/fundraising, each with its `search_params`). Stored keyed by domain; rebuilt only on
`refresh=true`. This is what stops every agent re-crawling the customer site."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel

from warmgraph.agents.activities.icp import derive_icp
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.agents.base import Agent
from warmgraph.competitive import analyze_competition
from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.embeddings import get_embedder
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import (
    CompanyProfile,
    CompetitiveAnalysis,
    IcpAnalysis,
    Profile,
    SignalContext,
)
from warmgraph.profile import derive_profile
from warmgraph.scraper import crawl_site
from warmgraph.search import web_search

_TTL_DAYS = 60                    # re-enrich a domain at most this often
_THIN_WORDS = 120                 # below this, supplement the crawl with free web search

# Per-domain in-flight dedup: concurrent requests for the SAME domain run ONE enrichment; the rest
# wait on the lock and then reuse the freshly stored profile (no duplicate fetch/LLM at scale).
_domain_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _domain_lock(domain: str) -> threading.Lock:
    with _locks_guard:
        lk = _domain_locks.get(domain)
        if lk is None:
            lk = threading.Lock()
            _domain_locks[domain] = lk
        return lk


def _is_fresh(p: Profile) -> bool:
    try:
        ts = p.refreshed_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts > datetime.now(timezone.utc) - timedelta(days=_TTL_DAYS)
    except Exception:
        return True  # if undated, treat as fresh rather than re-crawling endlessly

_CTX_SYSTEM = (
    "You build the SEARCH CONTEXT for finding GTM signals about a company's prospects. For each signal "
    "type, write a short 'context_text' (what makes a signal relevant) AND concrete search params:\n"
    "- social: what the ICP would POST about (pains/JTBD) + 4-6 SHORT keyword queries (2-4 words).\n"
    "- hiring: the trigger roles (companies hiring these = a need) + the buyer segment.\n"
    "- fundraising: the buyer segment/company-type (freshly-funded = a prospect) + 2-4 segment terms.\n"
    "Ground everything in the ICP/CI. Short, concrete, no years."
)
_CTX_SCHEMA = """Return ONLY JSON:
{"industry":"","segments":["..."],
 "social":{"context_text":"","queries":["short keyword","..."]},
 "hiring":{"context_text":"","trigger_roles":["role","..."],"segment":""},
 "fundraising":{"context_text":"","segment_terms":["DTC brand","..."]}}"""


def derive_contexts(registry: ModelRegistry, profile: CompanyProfile,
                    competitive: CompetitiveAnalysis, icp: IcpAnalysis
                    ) -> Tuple[str, List[str], List[SignalContext]]:
    if not registry.has_llm:
        return "", [], []
    competitors = ", ".join(c.name for c in competitive.direct_competitors[:8])
    pains = "; ".join(p for x in icp.personas for p in x.pains)[:400]
    roles = ", ".join(x.role for x in icp.personas[:5])
    user = (
        f"Company: {profile.name} — {profile.what_they_do}\nCategory: {profile.category}\n"
        f"Competitors: {competitors}\nICP roles: {roles}\nICP pains: {pains}\n"
        f"Winning category: {icp.winning_category}\nSegments: {', '.join(s.name for s in icp.segments[:4])}\n\n"
        f"{_CTX_SCHEMA}"
    )
    d = extract_json(registry.complete("signal_contexts", _CTX_SYSTEM, user,
                                       max_tokens=1500, want_json=True)) or {}
    soc, hir, fun = d.get("social", {}), d.get("hiring", {}), d.get("fundraising", {})
    contexts = [
        SignalContext(signal_type="social", context_text=str(soc.get("context_text", "")),
                      search_params={"queries": [str(q) for q in soc.get("queries", []) if q]}),
        SignalContext(signal_type="hiring", context_text=str(hir.get("context_text", "")),
                      search_params={"trigger_roles": [str(r) for r in hir.get("trigger_roles", []) if r],
                                     "segment": str(hir.get("segment", ""))}),
        SignalContext(signal_type="fundraising", context_text=str(fun.get("context_text", "")),
                      search_params={"segment_terms": [str(s) for s in fun.get("segment_terms", []) if s]}),
    ]
    return str(d.get("industry", "")), [str(s) for s in d.get("segments", []) if s], contexts


def build_profile(registry: ModelRegistry, settings: Settings, store, url: str,
                  refresh: bool = False, relationship_id: Optional[str] = None) -> Profile:
    domain = domain_of(url)

    def _cached() -> Optional[Profile]:
        if not refresh and store is not None:
            p = store.get_profile(domain)
            if p and _is_fresh(p):
                return p
        return None

    hit = _cached()
    if hit:
        return hit  # reuse — no crawl, no recompute

    # serialize concurrent builds for the SAME domain (in-flight dedup)
    with _domain_lock(domain):
        hit = _cached()  # another request may have just built it while we waited
        if hit:
            return hit
        return _build_and_store(registry, settings, store, url, domain, refresh, relationship_id)


def _build_and_store(registry: ModelRegistry, settings: Settings, store, url: str, domain: str,
                     refresh: bool, relationship_id: Optional[str]) -> Profile:
    text = crawl_site(settings, url)                       # free fetch (httpx + meta extraction)
    # supplement thin / JS-shell sites with FREE web search snippets about the company
    if len((text or "").split()) < _THIN_WORDS:
        try:
            hits = web_search(domain, settings, max_results=5)
            snip = "\n".join(f"- {h['title']}: {h['content']}"
                             for h in hits if h.get("content"))
            if snip:
                text = (text + "\n\nWEB SEARCH (about this company):\n" + snip)[:8000]
        except Exception:
            pass

    company = derive_profile(registry, settings, url, text)
    competitive = analyze_competition(registry, settings, company, text)
    icp = derive_icp(registry, settings, company, competitive)
    industry, segments, contexts = derive_contexts(registry, company, competitive, icp)

    # embed each context_text (the query vector for retrieval) — once, stored on the profile
    embedder = get_embedder(settings)
    if embedder:
        for c in contexts:
            try:
                c.embedding = embedder.embed_one(c.context_text)
            except Exception:
                pass

    if refresh and store is not None:                     # don't clobber user-locked params
        old = store.get_profile(domain)
        if old:
            for c in contexts:
                oc = old.context(c.signal_type)
                if oc and oc.params_locked:
                    c.search_params, c.params_locked = oc.search_params, True

    p = Profile(url=url, domain=domain, relationship_id=relationship_id, profile=company,
                competitive=competitive, icp=icp, industry=industry, segments=segments,
                contexts=contexts, source=registry.provider_name)
    if store is not None:
        store.save_profile(p)
        from warmgraph.storage import mirror
        mirror.dual_write("profile", mirror.mirror_profile, store, p)
    return p


class CompanyIcpInput(BaseModel):
    url: str
    refresh: bool = False
    relationship_id: Optional[str] = None


class CompanyIcpAgent(Agent):
    name = "company_icp"
    description = ("Builds + stores the company profile (company + competitive intelligence + ICP + "
                   "winning-category + the 3 signal contexts) — the foundation every other agent reads. "
                   "Cached by domain; pass refresh=true to rebuild.")
    InputModel = CompanyIcpInput
    OutputModel = Profile

    def run(self, inp: CompanyIcpInput) -> Profile:
        return build_profile(self.ctx.registry, self.ctx.settings, self.ctx.store, inp.url,
                             inp.refresh, inp.relationship_id)
