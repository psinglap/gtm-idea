"""`customer_list` — THE priority output: a prioritized list of real COMPANIES (accounts), ranked by
SIGNAL STACKING. It pulls the customer's relevant signals from the shared corpus (running the signal
agents if the corpus is thin), rolls them up by company, and ranks by how many signal TYPES stack on
the same company (2-3 stacked = the strongest buyers) + recency + context relevance.

Three signal types stack here:
  • fundraising — recently funded (fresh capital = buying budget)
  • hiring      — actively hiring a trigger role (building the need now)
  • team        — already employs a trigger role in-house (already doing the activity)

Each account carries a STRUCTURED signal feed (`signals`: source name + original link + date) so the
UI can render a per-company chat and filter by source / industry / location / funding stage / recency.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Dict, List

from pydantic import BaseModel

from warmgraph.agents.activities.corpus import retrieve_company_leads
from warmgraph.agents.activities.feedback import load_preferences
from warmgraph.agents.base import Agent
from warmgraph.dates import is_stale, parse_date
from warmgraph.llm.embeddings import cosine
from warmgraph.models import Account, AccountSignal, CustomerListReport

# accounts whose learned preference score is at/below this (look-alikes of things you rejected)
# are dropped once there's reject feedback to compare against.
_REJECT_LOOKALIKE = -0.25

# cosine floor for pulling a lead into a customer's list. 0.58 cleanly separates on-ICP signals
# (~0.60+) from cross-domain corpus noise (~0.50-0.55) — e.g. keeps DTC funding, drops "Technical
# Program Manager at an AI company" leaking in from another company's run.
_RELEVANCE_FLOOR = 0.58
_MIN_DT = datetime.min

_STAGE_RE = re.compile(
    r"\b(pre-?seed|seed|series\s+[a-f]|angel|growth|bridge|ipo)\b", re.IGNORECASE)


def _funding_stage(text: str) -> str:
    m = _STAGE_RE.search(text or "")
    if not m:
        return ""
    return m.group(1).title().replace("Series ", "Series ")


def _merge_into(dst: Account, src: Account) -> None:
    """Fold src's signals + counts into dst (same company under a different domain)."""
    dst.hiring_count += src.hiring_count
    dst.fundraising_count += src.fundraising_count
    dst.team_count += src.team_count
    dst.social_count += src.social_count
    for st in src.signal_types:
        if st not in dst.signal_types:
            dst.signal_types.append(st)
    dst.signals.extend(src.signals)
    dst.evidence.extend(src.evidence)
    dst.sources.extend(src.sources)
    dst.company_domain = dst.company_domain or src.company_domain
    dst.website = dst.website or src.website
    dst.location = dst.location or src.location
    dst.industry = dst.industry or src.industry
    dst.funding_stage = dst.funding_stage or src.funding_stage
    dst.role_present = dst.role_present or src.role_present
    if src.latest_signal_date > dst.latest_signal_date:
        dst.latest_signal_date = src.latest_signal_date
    dst.relevance = max(dst.relevance, src.relevance)


def _merge_by_name(accts: Dict[str, Account], embs: Dict[str, list]):
    """Merge accounts that share a normalized company name (dedup across domains). Returns new
    (accts, embs) dicts keyed by the surviving account's key."""
    out: Dict[str, Account] = {}
    out_embs: Dict[str, list] = {}
    name_to_key: Dict[str, str] = {}
    for key, a in accts.items():
        nm = (a.company or "").strip().lower()
        tgt = name_to_key.get(nm) if nm else None
        if tgt is None:
            out[key] = a
            out_embs[key] = embs.get(key, [])
            if nm:
                name_to_key[nm] = key
        else:
            _merge_into(out[tgt], a)
            if not out_embs.get(tgt) and embs.get(key):
                out_embs[tgt] = embs[key]
    return out, out_embs


class CustomerListInput(BaseModel):
    url: str
    limit: int = 60
    rebuild: bool = False   # skip the normalized serving read + recompute from the signal corpus


def serve_customer_list(store, domain: str, limit: int = 60):
    """SERVE the ranked customer list from the NORMALIZED tables (customer_list → customers →
    signals), assembled back into the Account DTO the UI already renders. Returns None when this
    client has no normalized list yet (→ caller falls back to a fresh build = refresh-on-miss).
    Feedback is already baked into the stored list at build time (see eval loop), so serving it is
    consistent with 'learning applies on the next build'."""
    client = store.get_company(domain)
    if client is None:
        return None
    rows = store.get_customer_list(client.id, limit=limit)
    if not rows:
        return None
    out: List[Account] = []
    for r in rows:
        prospect = store.get_customer_by_id(r.customer_id)
        if prospect is None:
            continue
        dom = "" if (prospect.domain or "").startswith("name:") else prospect.domain
        a = Account(subject_domain=domain, company=prospect.name, company_domain=dom,
                    website=prospect.website, industry=prospect.industry,
                    location=prospect.location, funding_stage=prospect.funding_stage,
                    stack_score=r.stack_score, pref_score=r.pref_score, relevance=r.relevance,
                    latest_signal_date=r.latest_signal_date)
        for f in store.get_signal_facts(customer_id=r.customer_id, since_days=None, limit=200):
            st = f.signal_type
            a.signals.append(AccountSignal(
                signal_type=st, source=f.source or "web", source_url=f.source_url,
                date=f.signal_date, text=f.text, role=f.role, relevance=f.relevance))
            if st == "hiring":
                a.hiring_count += 1
            elif st == "fundraising":
                a.fundraising_count += 1
            elif st == "team":
                a.team_count += 1
            elif st == "social":
                a.social_count += 1
            if st not in a.signal_types:
                a.signal_types.append(st)
            if f.role and st in ("hiring", "team"):
                a.role_present = True
            if f.source_url:
                a.sources.append(f.source_url)
            if f.text:
                a.evidence.append(f"{st}: {f.text}"[:160])
        a.signals.sort(key=lambda s: (parse_date(s.date) or _MIN_DT), reverse=True)
        a.evidence = a.evidence[:6]
        a.sources = list(dict.fromkeys(a.sources))[:6]
        # recompute the stack from the freshly-assembled signals so it's self-consistent (the stored
        # stack_score can be stale — e.g. an arbitrary winner among backfilled historical duplicates).
        a.stack_score = len(a.signal_types)
        out.append(a)
    # rank exactly like the build path so serving and rebuilding agree on order.
    out.sort(key=lambda a: (a.stack_score + 2.0 * a.pref_score, a.relevance, a.latest_signal_date),
             reverse=True)
    return out


class CustomerListAgent(Agent):
    name = "customer_list"
    description = ("THE priority output: a prioritized list of real companies (accounts), ranked by "
                   "SIGNAL STACKING (same company across fundraising + hiring + team) + recency + "
                   "context relevance. Each account carries a structured signal feed (source + link + "
                   "date). Pulls from the shared corpus; runs the signal agents if thin.")
    InputModel = CustomerListInput
    OutputModel = CustomerListReport

    def run(self, inp: CustomerListInput) -> CustomerListReport:
        store, reg = self.ctx.store, self.ctx.registry
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        # CUTOVER: serve the ranked list from the normalized tables (fast path). Refresh-on-miss:
        # an empty/absent normalized list, or an explicit rebuild, falls through to a fresh build
        # (which re-ranks from the signal corpus and dual-writes the normalized list back).
        if store is not None and not inp.rebuild:
            served = serve_customer_list(store, domain, inp.limit)
            if served:
                return CustomerListReport(subject_domain=domain, accounts=served)
        hc, fc, sc = profile.context("hiring"), profile.context("fundraising"), profile.context("social")
        h_emb = hc.embedding if hc else []
        f_emb = fc.embedding if fc else []
        s_emb = sc.embedding if sc else []
        # team signals embed like hiring (roles), so query them with the hiring vector.

        def _pull():
            hiring = retrieve_company_leads(store, h_emb, "hiring", is_stale, _RELEVANCE_FLOOR, k=80)
            funding = retrieve_company_leads(store, f_emb, "fundraising", is_stale, _RELEVANCE_FLOOR, k=80)
            team = retrieve_company_leads(store, h_emb, "team", is_stale, _RELEVANCE_FLOOR, k=80)
            social = retrieve_company_leads(store, s_emb, "social", is_stale, _RELEVANCE_FLOOR, k=80)
            return hiring, funding, team, social

        # 1. retrieve relevant signals from the SHARED corpus
        hiring, funding, team, social = _pull()

        # 2. run any signal type that is THIN for THIS customer (per-type, not total) so ALL FOUR
        #    signals are actually attempted for this ICP, then re-retrieve. Each agent no-ops fast if
        #    the shared corpus already has enough on-ICP leads.
        thin = {"fundraising_leads": len(funding) < 3, "hiring_leads": len(hiring) < 3,
                "team_signal": len(team) < 3, "social_leads": len(social) < 3}
        if any(thin.values()):
            for name, needed in thin.items():
                if needed:
                    try:
                        reg.run(name, {"url": inp.url, "limit": 40})
                    except Exception:
                        pass
            hiring, funding, team, social = _pull()

        # 3. roll up by company -> accounts with a structured signal feed + signal stacking
        accts: Dict[str, Account] = {}
        embs: Dict[str, list] = {}   # representative embedding per account (for preference scoring)
        tagged = ([(L, h_emb) for L in hiring] + [(L, f_emb) for L in funding]
                  + [(L, h_emb) for L in team] + [(L, s_emb) for L in social])
        for lead, q in tagged:
            key = (lead.company_domain or lead.company or "").strip().lower()
            if not key:
                continue
            a = accts.get(key)
            if a is None:
                a = Account(subject_domain=domain, company=lead.company,
                            company_domain=lead.company_domain, website=lead.website,
                            industry=lead.industry)
                accts[key] = a

            st = lead.signal_type
            if st == "hiring":
                a.hiring_count += 1
            elif st == "fundraising":
                a.fundraising_count += 1
            elif st == "team":
                a.team_count += 1
            elif st == "social":
                a.social_count += 1
            if st not in a.signal_types:
                a.signal_types.append(st)

            # structured signal (the chat message) — carries its own source name + link + date
            a.signals.append(AccountSignal(
                signal_type=st, source=lead.source or "web", source_url=lead.source_url,
                date=lead.signal_date, text=lead.rationale, role=lead.role,
                relevance=lead.relevance,
            ))

            # facets for filtering
            if lead.location and not a.location:
                a.location = lead.location
            if st == "fundraising" and not a.funding_stage:
                a.funding_stage = _funding_stage(lead.rationale)
            if lead.role and st in ("hiring", "team"):
                a.role_present = True

            # legacy flat views (kept for back-compat)
            if lead.rationale:
                a.evidence.append(f"{st}: {lead.rationale}"[:160])
            if lead.source_url:
                a.sources.append(lead.source_url)

            if lead.signal_date and lead.signal_date > a.latest_signal_date:
                a.latest_signal_date = lead.signal_date
            if lead.embedding and q:
                c = cosine(q, lead.embedding)
                if c > a.relevance:
                    a.relevance = c
                    embs[key] = lead.embedding   # keep the most-relevant lead's vector

        # 3b. DEDUP: the same company can arrive under two domains (e.g. bloomnutrition.com AND
        #     bloomnu.com). The roll-up keys on domain-or-name, so merge accounts that share a
        #     normalized company name into one before scoring.
        accts, embs = _merge_by_name(accts, embs)

        for a in accts.values():
            a.stack_score = len(a.signal_types)
            # newest signal first in the feed (undated 'team' signals sort last within a stack)
            a.signals.sort(key=lambda s: (parse_date(s.date) or _MIN_DT), reverse=True)
            a.evidence = a.evidence[:6]
            a.sources = list(dict.fromkeys(a.sources))[:6]

        # 4. LEARN from this customer's approve/reject feedback: SUPPRESS rejects, drop reject
        #    look-alikes, RERANK by preference (taste can outweigh one stack level), and PIN approved.
        pref = load_preferences(store, domain)
        approved_accts: List[Account] = []
        rest: List[Account] = []
        for key, a in accts.items():
            if pref.is_approved(a.company, a.company_domain):
                # a company the user explicitly approved is NEVER dropped and is pinned to the top —
                # not by suppression, not by the look-alike filter, not by the top-`limit` truncation.
                a.pref_score = round(pref.score(embs.get(key, [])), 4)
                approved_accts.append(a)
                continue
            if pref.is_suppressed(a.company, a.company_domain):
                continue
            a.pref_score = round(pref.score(embs.get(key, [])), 4)
            if pref.rejected_centroid and a.pref_score <= _REJECT_LOOKALIKE:
                continue
            rest.append(a)

        rank_key = lambda a: (a.stack_score + 2.0 * a.pref_score, a.relevance, a.latest_signal_date)
        approved_accts.sort(key=rank_key, reverse=True)
        rest.sort(key=rank_key, reverse=True)
        # approved always survive in full (pinned top); the rest fill up to `limit`.
        ranked = approved_accts + rest[: max(0, inp.limit - len(approved_accts))]

        if store is not None and ranked:
            store.save_accounts(ranked)
            from warmgraph.storage import mirror
            mirror.dual_write("accounts", mirror.mirror_accounts, store, domain, ranked,
                              store.get_feedback(domain))
        return CustomerListReport(subject_domain=domain, accounts=ranked)
