"""Events agent. Finds recent/upcoming events the company's customers would attend (Luma /
Meetup / Eventbrite / conferences) — events are a strong GTM signal. Uses web search (Tavily)
to discover event pages, then an LLM to structure them. Attendee lists are captured only where
public (often empty — honest)."""
from __future__ import annotations

from typing import List

from pydantic import BaseModel

from warmgraph.agents.base import Agent
from warmgraph.agents.activities.icp import derive_icp
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.jsonutil import extract_json
from warmgraph.models import Event, EventsReport
from warmgraph.profile import derive_profile
from warmgraph.scraper import crawl_site
from warmgraph.search import tavily_search

_SYSTEM = (
    "You extract real, upcoming/recent (~3 months) events from web search results, then KEEP ONLY "
    "the ones whose ATTENDEES would be this company's ideal customers (its ICP / buyers) — events "
    "where the company could actually find prospects. DROP generic listicles, irrelevant-industry "
    "events, and anything where the audience isn't a fit. For each kept event give a one-line "
    "why_relevant tying it to the ICP. If none fit, return an empty list — do NOT pad."
)
_SCHEMA = """Return ONLY JSON:
{"events":[{"name":"","url":"","platform":"luma|meetup|eventbrite|conference","starts_at":"YYYY-MM-DD or ''","city":"","is_virtual":false,"description":"","why_relevant":""}]}"""


class EventsInput(BaseModel):
    url: str
    city: str = ""


class EventsAgent(Agent):
    name = "events"
    description = "Find recent/upcoming events whose attendees are the company's ICP (a GTM signal). Relevance-filtered, not generic."
    InputModel = EventsInput
    OutputModel = EventsReport

    def run(self, inp: EventsInput) -> EventsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        domain = domain_of(inp.url)
        text = crawl_site(s, inp.url)
        profile = derive_profile(reg, s, inp.url, text)
        cat = (profile.category or profile.name or "B2B SaaS").strip()
        icp = derive_icp(reg, s, profile, None)
        audience = ", ".join(p.role for p in icp.personas[:4]) or cat

        snippets: List[str] = []
        if s.has_tavily:
            qs = [f"conference for {audience} 2026", f"{cat} summit OR conference 2026",
                  f"lu.ma {cat} events", f"meetup {audience}"]
            if inp.city:
                qs.append(f"{cat} events {inp.city}")
            for q in qs:
                for r in tavily_search(q, s, max_results=5):
                    snippets.append(f"- {r.get('title')}: {r.get('content', '')[:160]} ({r.get('url')})")

        events: List[Event] = []
        if snippets and reg.has_llm:
            user = (
                f"Company: {profile.name} — {profile.what_they_do}\nCategory: {cat}\n"
                f"ICP (who must be in the audience): {audience}\n"
                f"ICP pains: {'; '.join(p for x in icp.personas for p in x.pains)[:300]}\n\n"
                f"Search results:\n" + "\n".join(snippets[:24]) + f"\n\n{_SCHEMA}"
            )
            d = extract_json(reg.complete("events_extract", _SYSTEM, user,
                                          max_tokens=1600, want_json=True)) or {}
            for e in d.get("events", []):
                if not e.get("name") or not e.get("why_relevant"):
                    continue
                events.append(Event(
                    subject_domain=domain, name=str(e.get("name", "")), url=str(e.get("url", "")),
                    platform=str(e.get("platform", "")), city=str(e.get("city", "")),
                    is_virtual=bool(e.get("is_virtual", False)),
                    description=str(e.get("why_relevant") or e.get("description", "")), raw=e,
                ))
        if store is not None and events:
            store.save_events(events)
            from warmgraph.storage import mirror
            mirror.dual_write("events", mirror.mirror_events, store, events)
        return EventsReport(subject_domain=domain, events=events)
