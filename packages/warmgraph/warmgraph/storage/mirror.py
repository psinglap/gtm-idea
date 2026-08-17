"""Dual-write bridge: mirror the legacy blob objects (Profile/Post/Event/CompanyLead/Account)
into the normalized relational tables (companies / company_intel / raw_* / signals / customers /
customer_list). Reads stay on the legacy tables until cutover — this just keeps the normalized
schema populated in parallel so we can verify parity, then flip reads over.

The legacy→normalized MAPPING lives here in ONE place, shared by the live agents (dual-write) and
`scripts/migrate_normalize.py` (backfill). Pure `build_*` functions do the mapping; `mirror_*`
functions build + persist. Agent call sites wrap these in `dual_write(...)` so a normalized-write
failure can NEVER break the live legacy pipeline during the transition."""
from __future__ import annotations

import logging
from typing import List, Optional

from warmgraph.entities import (
    Client,
    CompanyIntel,
    CustomerListRow,
    Prospect,
    RawEvent,
    RawSocialPost,
    SignalFact,
)
from warmgraph.models import Account, CompanyLead, Event, LeadFeedback, Post, Profile

log = logging.getLogger("warmgraph.mirror")

_DECISION_STATUS = {"approve": "approved", "reject": "rejected"}


def name_key(name: str) -> str:
    """Same normalization customer_list uses to dedup across domains."""
    return (name or "").strip().lower()


def _as_dict(x):
    """Legacy Profile stores typed sub-models (CompanyProfile/…); CompanyIntel wants plain dicts."""
    if x is None:
        return {}
    return x.model_dump(mode="json") if hasattr(x, "model_dump") else x


def dual_write(label: str, fn, *args, **kwargs):
    """Run a mirror write, swallowing+logging any error so the legacy path is never affected."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # pragma: no cover - defensive, transition-only
        log.warning("normalized dual-write failed (%s): %s", label, e)
        return None


# --------------------------------------------------------------------------- #
# Pure builders (no store) — shared by dual-write and the migration            #
# --------------------------------------------------------------------------- #
def build_client(p: Profile) -> Client:
    domain = (p.domain or "").lower()
    return Client(domain=domain, url=p.url, name=(domain.split(".")[0] if domain else ""),
                  industry=p.industry, relationship_id=p.relationship_id)


def build_company_intel(p: Profile, company_id: str) -> CompanyIntel:
    return CompanyIntel(company_id=company_id, domain=(p.domain or "").lower(),
                        profile=_as_dict(p.profile), competitive=_as_dict(p.competitive),
                        icp=_as_dict(p.icp), contexts=[_as_dict(c) for c in (p.contexts or [])],
                        refreshed_at=p.refreshed_at)


def prospect_from_lead(L: CompanyLead) -> Prospect:
    return Prospect(domain=(L.company_domain or "").lower(), name=L.company,
                    name_key=name_key(L.company), website=L.website, industry=L.industry,
                    location=L.location)


def prospect_from_account(a: Account) -> Prospect:
    return Prospect(domain=(a.company_domain or "").lower(), name=a.company,
                    name_key=name_key(a.company), website=a.website, industry=a.industry,
                    location=a.location, funding_stage=a.funding_stage)


def build_signal_fact(L: CompanyLead, customer_id: str) -> SignalFact:
    return SignalFact(id="sig-" + L.id, customer_id=customer_id, signal_type=L.signal_type,
                      source=L.source, source_url=L.source_url, signal_date=L.signal_date,
                      text=L.rationale, role=L.role, relevance=str(L.relevance),
                      industry=L.industry, embedding=L.embedding or [])


def build_raw_social_post(p: Post) -> RawSocialPost:
    return RawSocialPost(platform=p.platform, external_id=p.external_id or p.id, author=p.author,
                         author_handle=p.author_handle, text=p.text or p.title, url=p.url,
                         posted_at=p.posted_at, embedding=p.embedding or [],
                         raw=p.model_dump(mode="json"))


def build_raw_event(e: Event) -> RawEvent:
    return RawEvent(source=e.platform, url=e.url or e.id, title=e.name, description=e.description,
                    starts_at=e.starts_at, city=e.city, is_virtual=e.is_virtual,
                    raw=e.model_dump(mode="json"))


def feedback_status_map(feedback: Optional[List[LeadFeedback]]) -> dict:
    """company key (domain or name) → customer_list status, from approve/reject feedback."""
    out = {}
    for f in (feedback or []):
        key = (f.company_domain or "").lower() or name_key(f.company)
        if key:
            out[key] = _DECISION_STATUS.get(f.decision, "new")
    return out


# --------------------------------------------------------------------------- #
# Write helpers (build + persist) — used at agent persist points               #
# --------------------------------------------------------------------------- #
def client_id_for(store, domain: str, url: str = "", industry: str = "",
                  relationship_id=None) -> str:
    domain = (domain or "").lower()
    existing = store.get_company(domain)
    if existing:
        return existing.id
    return store.upsert_company(Client(domain=domain, url=url,
                                       name=(domain.split(".")[0] if domain else ""),
                                       industry=industry, relationship_id=relationship_id)).id


def mirror_profile(store, p: Profile) -> str:
    cid = client_id_for(store, p.domain, url=p.url, industry=p.industry,
                        relationship_id=p.relationship_id)
    store.save_company_intel(build_company_intel(p, cid))
    return cid


def mirror_company_leads(store, leads: List[CompanyLead]) -> int:
    n = 0
    for L in leads:
        prospect = store.upsert_customer(prospect_from_lead(L))
        store.save_signal_facts([build_signal_fact(L, prospect.id)])
        n += 1
    return n


def mirror_posts(store, posts: List[Post]) -> int:
    posts = list(posts)
    if not posts:
        return 0
    return store.save_raw_social_posts([build_raw_social_post(p) for p in posts])


def mirror_events(store, events: List[Event]) -> int:
    events = list(events)
    if not events:
        return 0
    return store.save_raw_events([build_raw_event(e) for e in events])


def mirror_accounts(store, subject_domain: str, accounts: List[Account],
                    feedback: Optional[List[LeadFeedback]] = None) -> int:
    cid = client_id_for(store, subject_domain)
    status = feedback_status_map(feedback)
    rows: List[CustomerListRow] = []
    for a in accounts:
        prospect = store.upsert_customer(prospect_from_account(a))
        key = (a.company_domain or "").lower() or name_key(a.company)
        rows.append(CustomerListRow(company_id=cid, customer_id=prospect.id,
                                    stack_score=a.stack_score, pref_score=a.pref_score,
                                    relevance=a.relevance, latest_signal_date=a.latest_signal_date,
                                    status=status.get(key, "new")))
    store.replace_customer_list(cid, rows)
    return len(rows)
