"""Social-signal lead engine — company-level intent from social listening.

social_listening surfaces person/post-level discussion of the PROBLEM. This turns the subset that is
COMPANY-attributable into the same company-level lead shape as hiring/fundraising/team, so a company
publicly showing intent (a marketer at Brand X asking how to measure creator-marketing ROI, comparing
tools, or complaining about a competitor) STACKS in customer_list alongside the other signals.

We only emit a lead when a real company is identifiable from the post (author's employer, a company
account, or the text) AND it's a plausible buyer — anonymous community chatter stays person-level in
the Social tab. Grounded in the real post URL (never invented). signal_type='social'.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from pydantic import BaseModel

from warmgraph.agents.activities.corpus import embed_and_tag_leads, retrieve_company_leads
from warmgraph.agents.activities.feedback import feedback_prompt_block
from warmgraph.agents.activities.hiring_leads import _website_resolves
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.agents.base import Agent
from warmgraph.dates import is_stale
from warmgraph.jsonutil import extract_json
from warmgraph.models import CompanyLead, LeadsReport
from warmgraph.search import web_search

_EXTRACT_SYSTEM = (
    "You extract COMPANY-level SOCIAL buying signals from real social posts. The signal: a company is "
    "publicly showing intent for what our product does — someone AT a company (or a company account) "
    "expressing the pain, asking how to solve it, comparing tools, or unhappy with a competitor.\n"
    "Rules:\n"
    "- Use ONLY the posts shown; 'i' is the post index. NEVER invent a URL or quote.\n"
    "- Identify the author's EMPLOYER: from the post text, the company account, OR by recognizing the "
    "author handle (many are known founders/operators/marketers — use what you know to map the handle "
    "to the company they work at or founded). It's fine to name a company the text doesn't spell out, "
    "AS LONG AS you're confident the handle maps to it.\n"
    "- Emit a lead when the author is a real practitioner/founder at an identifiable company that is a "
    "plausible BUYER for our product (right segment). SKIP: fully anonymous posters you can't attribute, "
    "agencies/vendors/consultants selling this service, our own competitors, and pure news/press.\n"
    "- rationale = who + the intent + platform, e.g. 'Atomberg co-founder said influencer-marketing "
    "measurement is a uniquely hard problem (Twitter)'. Capture company, inferred website domain, "
    "location if evident (else ''), relevance High/Medium/Low. Dedupe by company."
)
_EXTRACT_SCHEMA = """Return ONLY JSON:
{"leads":[{"i":0,"company":"","website":"","location":"","rationale":"","relevance":"High"}]}"""


def _who(p: dict) -> str:
    name = p.get("author") or ""
    handle = p.get("author_handle") or ""
    return name or handle or ""


def _looks_attributable(p: dict) -> bool:
    """A post worth enriching: has a real author/handle (not an anonymous placeholder)."""
    who = _who(p).strip()
    return bool(who) and who not in ("?", "[deleted]", "deleted") and "/comments/" not in who


def enrich_authors(settings, posts: List[dict], cap: int = 12) -> Dict[int, str]:
    """For each attributable author, a quick FREE web search to resolve their EMPLOYER — the evidence
    that lets the model turn a social post into a company-level signal (e.g. @arindam___paul → Atomberg).
    Runs in parallel; best-effort (missing snippets just mean weaker attribution)."""
    idxs = [i for i, p in enumerate(posts) if _looks_attributable(p)][:cap]

    def lookup(i: int) -> tuple:
        p = posts[i]
        q = f"{_who(p)} {p.get('platform','')} founder OR marketing OR company"
        try:
            hits = web_search(q, settings, max_results=2)
            snip = " · ".join(f"{h.get('title','')}: {h.get('content','')}" for h in hits)[:280]
        except Exception:
            snip = ""
        return i, snip

    out: Dict[int, str] = {}
    if idxs:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for i, snip in ex.map(lookup, idxs):
                if snip:
                    out[i] = snip
    return out


def _post_line(i: int, p: dict, bio: str = "") -> str:
    name = p.get("author") or "?"
    handle = p.get("author_handle") or ""
    who = f"{name}" + (f" (@{handle})" if handle and handle != name else "")
    body = (p.get("text") or p.get("title") or "")[:180]
    theme = p.get("problem_theme") or ""
    line = f"{i}. [{p.get('platform','')}] {who} | pain: {theme or '—'} | {body} | {p.get('url','')}"
    if bio:
        line += f"\n     ↳ who is {who}: {bio}"
    return line


def _clean_author_from_title(title: str) -> str:
    """LinkedIn SERP titles read 'Firstname Lastname on LinkedIn: <post>'. Pull the name."""
    t = (title or "").strip()
    for sep in (" on LinkedIn", " | LinkedIn", " – LinkedIn", " - LinkedIn"):
        if sep in t:
            return t.split(sep)[0].strip()
    return ""


def linkedin_serp_posts(settings, profile, cap: int = 30) -> List[dict]:
    """A: PUBLIC LinkedIn posts via web search (no login, no proxy). LinkedIn post/article snippets
    routinely name the author's company, so these are strongly company-attributable. Returns
    pseudo-'post' dicts in the same shape the extractor consumes."""
    sc = profile.context("social")
    queries = (sc.search_params.get("queries") if sc else []) or []
    queries = [q for q in queries if q][:5] or [profile.profile.category]
    out: List[dict] = []
    seen: set = set()
    for q in queries:
        for r in web_search(q, settings, max_results=6, include_domains=["linkedin.com"]):
            u = r.get("url", "")
            # keep member POSTS/articles (intent), not job listings or company landing pages
            if not u or u in seen or "/jobs/" in u:
                continue
            if not any(seg in u for seg in ("/posts/", "/pulse/", "/feed/", "/in/")):
                continue
            seen.add(u)
            title = r.get("title", "")
            out.append({
                "platform": "linkedin", "author": _clean_author_from_title(title),
                "author_handle": "", "title": title, "text": r.get("content", ""),
                "url": u, "problem_theme": q, "posted_at": "",
            })
            if len(out) >= cap:
                return out
    return out


class SocialLeadsInput(BaseModel):
    url: str
    limit: int = 25
    force: bool = False   # skip the corpus shortcut and scrape fresh (widen coverage)


class SocialLeadsAgent(Agent):
    name = "social_leads"
    description = ("Company-level SOCIAL buying signals: companies publicly showing intent in social "
                   "posts (expressing the pain / seeking a solution / comparing competitors) → "
                   "company-level leads (Jesse format), grounded in the real post URL. signal_type='social'.")
    InputModel = SocialLeadsInput
    OutputModel = LeadsReport

    def run(self, inp: SocialLeadsInput) -> LeadsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        sc = profile.context("social")
        s_emb = sc.embedding if sc else []

        # 1. RETRIEVE company-level social leads from the SHARED corpus first
        retrieved = retrieve_company_leads(store, s_emb, "social", is_stale)
        if len(retrieved) >= 5 and not inp.force:
            return LeadsReport(subject_domain=domain, signal_type="social", leads=retrieved[: inp.limit])

        # 2. Otherwise gather posts from (a) public LinkedIn post search + (b) social_listening
        #    (which now includes Bluesky) — both free, no login/proxy. The LinkedIn/Bluesky posts
        #    carry real authors/companies, so they're the attributable ones.
        posts: List[dict] = list(linkedin_serp_posts(s, profile))   # A: public LinkedIn posts
        try:
            rep = reg.run("social_listening", {"url": inp.url, "since_days": 90, "limit": 50})
            for pl in rep.get("platforms", []):
                posts.extend(pl.get("posts", []))                   # incl. B: Bluesky
        except Exception:
            pass
        if not posts:
            return LeadsReport(subject_domain=domain, signal_type="social", leads=[])

        ctx = {"product": profile.profile.what_they_do,
               "segment": ", ".join(profile.segments) or profile.profile.category}
        # enrich identifiable authors -> employer (the evidence that makes attribution possible)
        bios = enrich_authors(s, posts)
        leads = self._extract(reg, ctx, posts, domain, bios)

        def fix(L: CompanyLead) -> CompanyLead:
            if L.website and not _website_resolves(L.website):
                L.website = ""
            return L
        if leads:
            with ThreadPoolExecutor(max_workers=8) as ex:
                leads = list(ex.map(fix, leads))

        leads = leads[: inp.limit]
        leads = embed_and_tag_leads(s, leads, profile.industry, "social", domain_of)
        if store is not None and leads:
            store.save_company_leads(leads)
            from warmgraph.storage import mirror
            mirror.dual_write("social_leads", mirror.mirror_company_leads, store, leads)
        return LeadsReport(subject_domain=domain, signal_type="social", leads=leads)

    def _extract(self, registry, ctx, posts: List[dict], domain: str,
                 bios: Optional[Dict[int, str]] = None) -> List[CompanyLead]:
        if not posts or not registry.has_llm:
            return []
        bios = bios or {}
        learned = feedback_prompt_block(self.ctx.store, domain)
        out: List[CompanyLead] = []
        by_company: set = set()
        chunk = 14
        for start in range(0, min(len(posts), 84), chunk):
            batch = posts[start:start + chunk]
            lines = [_post_line(i, p, bios.get(start + i, "")) for i, p in enumerate(batch)]
            user = (
                f"Our product: {ctx['product']}\nWe sell to: {ctx['segment']}\n\n"
                f"Social posts:\n" + "\n".join(lines) + f"\n\n{_EXTRACT_SCHEMA}" + learned
            )
            d = extract_json(registry.complete("social_leads_extract", _EXTRACT_SYSTEM, user,
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
                    source=src.get("platform", "") or "social", source_url=src_url,
                    signal_type="social", rationale=str(item.get("rationale", "")),
                    relevance=str(item.get("relevance", "")),
                    signal_date=str(src.get("posted_at", "") or "")[:10],
                ))
        return out
