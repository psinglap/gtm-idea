"""Engagement agent — turn each identified contact into concrete TOUCHPOINTS: the specific openings
to reach them, computed by matching the person across ALL the raw corpora we already hold.

For a contact (a `person` at a prospect, for a client) it looks across:
  • raw_social_posts they authored        → "reply to their post" (their own words = the warm opener)
  • raw_events they're attending           → "meet them at <event>"
  • raw_job_postings / raw_funding_news    → their company is hiring / just raised = the timely angle
Each match becomes a `Touchpoint` with a suggested_action + the evidence it's grounded in. Pure
`build_touchpoints(...)` (no store / no network) so the matching is unit-tested offline.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel

from warmgraph.agents.activities.people import _name_parts, norm_url
from warmgraph.agents.base import Agent
from warmgraph.entities import (
    Person,
    RawEvent,
    RawFundingNews,
    RawJobPosting,
    RawSocialPost,
    Touchpoint,
)
from warmgraph.storage import mirror


def _authored_by(post: RawSocialPost, first: str, last: str) -> bool:
    who = (post.author + " " + post.author_handle).lower()
    if last and last in who and (not first or first[:1] in who):
        return True
    return False


def _brand(domain: str) -> str:
    return (domain or "").lower().replace("www.", "").split(".")[0]


def _mentions_company(text: str, brand: str) -> bool:
    return bool(brand) and len(brand) >= 3 and brand in (text or "").lower()


def build_touchpoints(company_id: str, customer_id: str, person: Person,
                      posts: List[RawSocialPost], events: List[RawEvent],
                      jobs: List[RawJobPosting], funding: List[RawFundingNews]) -> List[Touchpoint]:
    first, last = _name_parts(person.person)
    brand = _brand(person.company_domain)
    refs = {norm_url(r) for r in person.touchpoint_refs} | set(person.touchpoint_refs)
    out: List[Touchpoint] = []

    def tp(**kw):
        out.append(Touchpoint(company_id=company_id, customer_id=customer_id, person_id=person.id, **kw))

    # 1) their own posts — the warmest opener
    for p in posts:
        if _authored_by(p, first, last):
            tp(type="post-comment", source=p.platform, url=p.url,
               date=(p.posted_at.isoformat() if p.posted_at else ""),
               title=(p.text or "")[:120],
               suggested_action=f"Reply to {person.person}'s {p.platform} post",
               evidence=(p.text or "")[:280])

    # 2) events they attend
    for e in events:
        if norm_url(e.url) in refs or e.id in refs:
            tp(type="event", source=e.source, url=e.url,
               date=(e.starts_at.isoformat() if e.starts_at else ""), title=e.title,
               suggested_action=f"Meet {person.person} at {e.title or 'the event'}",
               evidence=(e.description or e.title)[:280])

    # 3) their company's hiring / funding = the timely angle
    for j in jobs:
        if _mentions_company(f"{j.company_hint} {j.title} {j.content}", brand):
            tp(type="company-signal", source=j.source or "job-board", url=j.url, title=j.title,
               suggested_action=f"Open with {person.company_domain}'s hiring ({j.title or 'a role'})",
               evidence=(j.title or "")[:280])
            break   # one hiring angle per person is enough
    for f in funding:
        if _mentions_company(f"{f.title} {f.content}", brand):
            tp(type="company-signal", source=f.source or "news", url=f.url, date=f.published_at,
               title=f.title,
               suggested_action=f"Congratulate {person.person} on {person.company_domain}'s raise",
               evidence=(f.title or "")[:280])
            break

    return out


class EngagementInput(BaseModel):
    url: str
    limit: int = 50     # max contacts to compute touchpoints for


class EngagementReport(BaseModel):
    subject_domain: str
    touchpoints: List[Touchpoint] = []


class EngagementAgent(Agent):
    name = "engagement"
    description = ("For each identified contact, compute concrete TOUCHPOINTS (reply to their post, "
                   "meet at their event, open with their company's hiring/funding) by matching the "
                   "person across all raw corpora — each with a suggested action + grounded evidence.")
    InputModel = EngagementInput
    OutputModel = EngagementReport

    def run(self, inp: EngagementInput) -> EngagementReport:
        store = self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        if store is None:
            return EngagementReport(subject_domain=domain)
        cid = mirror.client_id_for(store, domain)

        posts = store.get_raw_social_posts(since_days=120, limit=2000)
        events = store.get_raw_events(since_days=180, limit=500)
        jobs = store.get_raw_job_postings(since_days=120, limit=2000)
        funding = store.get_raw_funding_news(since_days=120, limit=2000)

        contacts = store.get_customer_contacts(cid, limit=inp.limit)
        all_tps: List[Touchpoint] = []
        for cc in contacts:
            person = store.get_person(cc.person_id)
            if person is None:
                continue
            tps = build_touchpoints(cid, cc.customer_id, person, posts, events, jobs, funding)
            if tps:
                store.save_touchpoints(tps)
                all_tps.extend(tps)
        return EngagementReport(subject_domain=domain, touchpoints=all_tps)
