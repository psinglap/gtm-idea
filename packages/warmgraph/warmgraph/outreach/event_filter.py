"""Drop events people attend to relax rather than to work.

The SF Luma feed is full of run clubs, poker nights, pilates classes, poetry readings and
barbecues. They are real events with real attendees, but nobody there is thinking about
marketing budget, and a follow-up email about creator-led growth after a 5k is a bad look.

The distinction is NOT "social vs formal". A founder dinner, an operators' happy hour and a demo
night are all social, and all excellent. The test is what the attendee came for:

    would someone attend this to ADVANCE THEIR WORK, or to RELAX?

Run clubs and karaoke fail it. Founder dinners pass it, drinks and all.

Judged by an LLM on the title, because keywords cannot tell "Pitch & Run San Francisco" (a run)
from "Pitch Night" (an event), and cannot read 🥒🎾🏓 as pickleball. Emojis are passed through
for exactly that reason.
"""
from __future__ import annotations

import os
from typing import Dict, List

from warmgraph.jsonutil import extract_json

TASK = "event_leisure_filter"

_SYSTEM = (
    "You decide whether an event is a PROFESSIONAL event, where founders, operators, marketers "
    "and decision-makers gather to advance their work, or a LEISURE / personal activity.\n\n"
    "REJECT (keep=false):\n"
    "- sport and fitness: run clubs, 5ks, pickleball, tennis, soccer, cycling, pilates, yoga, "
    "hiking, climbing, surfing, gym\n"
    "- games and entertainment: poker, board games, karaoke, trivia, film screenings, concerts, "
    "club nights, comedy\n"
    "- arts and performance: poetry, improv, painting, ceramics, sewing, book clubs, radio or "
    "music collectives, art shows\n"
    "- purely social meals with no professional framing: barbecues, potlucks, picnics, "
    "sundaes, ice cream socials\n"
    "- vendor OFFICE HOURS and product support sessions\n"
    "- student-led events, university cohorts, MBA class trips, career fairs for students\n"
    "- wellness, dating, religious, parenting and hobby meetups\n"
    "- DEEP VERTICAL TECHNICAL COMMUNITIES outside consumer brands and marketing: "
    "cybersecurity practitioners, climate and clean energy, biotech and life sciences, "
    "academic or frontier AI research, hardware and semiconductors. The people are excellent "
    "and none of them buy influencer marketing.\n"
    "- personal finance, tax, wealth or investing seminars aimed at individuals\n"
    "- VAGUE titles with no stated professional purpose. If you cannot tell what the event is "
    "for from its name, reject it.\n\n"
    "KEEP (keep=true):\n"
    "- anything where the POINT is professional: founder dinners, operator happy hours, demo "
    "nights, pitch events, hackathons, workshops, summits, industry meetups, breakfasts and "
    "fireside chats\n"
    "- keep these EVEN IF the format is social. A founder dinner is a work event with food.\n\n"
    "CRITICAL: the ACTIVITY decides, not the audience. If the thing people physically DO is a "
    "sport, a game, or an art form, REJECT it however professional the crowd is. "
    "'Founders Run Club' is a run. 'Pitch & Run' is a run. 'Investor Pickleball' is pickleball. "
    "'VC Poker Night' is poker. The social-format exemption covers FOOD AND DRINK only — "
    "dinners, breakfasts, coffee, happy hours, mixers — never activities.\n\n"
    "The test: would someone attend this to advance their work, or to relax? When genuinely "
    "unsure, KEEP it — dropping a good room costs more than one wasted scan.\n"
    "Return STRICT JSON only."
)

# Real decisions from this account, kept as few-shot because the examples ARE the spec. Every
# rejected line below is one the model got wrong at least once without them — mostly cases
# where the crowd is exactly right and the activity is not.
_EXAMPLES = """
Worked examples (follow these exactly):
  REJECT  Radio Club                                  hobby club
  REJECT  Sewing Workshop #9                          craft class
  REJECT  Seasonal Mending Circle                     craft circle
  REJECT  Techno Retro Poetics w/ Ana Maria Caballero poetry reading
  REJECT  Post-Op Run Club 5k @ GGP                   run club
  REJECT  Founders Run Club SF x MongoDB              run club, founders or not
  REJECT  Builders who Run - Golden Gate Bridge 4M    run club
  REJECT  Pitch & Run San Francisco                   it is a run
  REJECT  Full Spectrum Improvisation - Free class    improv class
  REJECT  People Before Ideas: Afore Open             racquet sport
  REJECT  POKER. IS. BACK.                            poker
  REJECT  SF Founders & Investors Poker Night         poker, founders or not
  REJECT  Scrappy AI Founders Play Soccer             soccer, founders or not
  REJECT  YC S26 Kayaking Day                         kayaking
  REJECT  Female Founders Hike at Lands End           hike, founders or not
  REJECT  Founders & Fidos: Beach Walk + Connect      a walk
  REJECT  Femme Foundry - Walk and Talk               a walk
  REJECT  Pilates + Lattes by item                    fitness class
  REJECT  Office Hours w/ PostHog, Supabase, Linear   vendor office hours
  REJECT  Espresso Hours (like office hours)          vendor office hours
  REJECT  Austrian MBA students learning journey      student cohort
  REJECT  YC Startup Internship Expo                  student careers fair
  REJECT  DATA STRUCTURES & ALGORITHMS STUDY GROUP    study group
  REJECT  Academic Night                              academic, not industry
  REJECT  Summer Barbecue                             social barbecue
  REJECT  Founder BBQ Bash                            barbecue, founders or not
  REJECT  Karaoke at Bay Street                       karaoke
  REJECT  Screening: Ghost in the Shell (1995)        film screening
  REJECT  Service Design Book Club                    book club
  REJECT  Writing Club                                writing club
  REJECT  Tech & Vinyl Cafe @ Hedge Coffee            music listening
  REJECT  PRIVATE SUITE AT LEVI'S STADIUM: 49ers      sports game
  REJECT  Night Moves Community Bike Ride             bike ride
  REJECT  Weekday Hiking                              hiking
  REJECT  SIMMER ROOM                                 vague, no stated purpose
  REJECT  Lexi @ Philz                                vague, no stated purpose
  REJECT  Open Tab No 03                              vague, no stated purpose
  REJECT  Post-Black Hat Decompression: Cyber Prac..  cybersecurity practitioners
  REJECT  Methane Cluster Kickoff - Solving for Me..  climate tech
  REJECT  Biopunk 2050: S26 Final Showcase            biotech showcase
  REJECT  Recursively Self-Improving AI & AI Neofa..  frontier AI research
  REJECT  Silicon Valley tax and capital gains sem..  personal finance seminar
  KEEP    SF Founder Dinner: Summer Edition           founder dinner
  KEEP    AI GTM & Sales Circle: Happy Hour           operator happy hour
  KEEP    Product Management Dinner @ Dosa            professional dinner
  KEEP    Female Founder Night                        founder networking
  KEEP    SF AI Code And Coffee                       working session
  KEEP    Deep Tech and Hardware Happy Hour           industry happy hour
  KEEP    SF Enterprise HACKATHON                     hackathon
  KEEP    Marketing Measurement and Attribution       industry talk
  KEEP    Founders Sushi Masterclass                  founder networking over food
  KEEP    YC Founders Summer Mixer                    founder mixer
"""

_SCHEMA = ('Return ONLY JSON: {"results":[{"i":<index>,"keep":<bool>,"why":"<3-6 words>"}]} '
           "— one entry per event.")

BATCH = 25


def enabled() -> bool:
    """WG_EVENT_FILTER=0 turns the whole filter off — useful in tests, and for debugging a run
    without a model in the loop."""
    return os.getenv("WG_EVENT_FILTER", "1").strip() not in ("0", "false", "no", "")


def classify(registry, titles: List[str]) -> List[Dict]:
    """[{title, keep, why}] in the order given.

    Fails OPEN: with no LLM, or on an error, everything is kept. A filter that silently drops
    events when the model is unavailable would quietly shrink the pipeline with no visible
    cause.
    """
    rows = [{"title": t, "keep": True, "why": ""} for t in titles]
    if not titles or not enabled() or not getattr(registry, "has_llm", False):
        return rows

    for start in range(0, len(titles), BATCH):
        chunk = titles[start:start + BATCH]
        listing = "\n".join(f"#{i}: {t}" for i, t in enumerate(chunk))
        try:
            raw = registry.complete(
                TASK, _SYSTEM + _EXAMPLES, f"Events:\n{listing}\n\n{_SCHEMA}",
                max_tokens=1400, want_json=True,
                # Classification must not wander between runs. The flip-flopping we saw
                # (poker night kept on one pass, rejected on the next) was sampling noise at
                # the default 0.2, not genuine ambiguity.
                temperature=0.0)
            results = (extract_json(raw) or {}).get("results", [])
        except Exception:
            continue                      # this batch stays kept
        for r in results:
            if not isinstance(r, dict):
                continue
            try:
                idx = int(r.get("i"))
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(chunk):
                rows[start + idx]["keep"] = bool(r.get("keep", True))
                rows[start + idx]["why"] = str(r.get("why", ""))[:60]
    return rows


CACHE_KEY = "event_filter_cache"
CACHE_MAX = 3000

# Bumped whenever the prompt or examples change. It is part of every cache key, so tightening
# the rules re-judges everything instead of leaving yesterday's verdicts frozen in place — the
# exact trap of caching a decision that depends on a prompt.
PROMPT_VERSION = 2


def _cache_key(title: str) -> str:
    return f"v{PROMPT_VERSION}:" + " ".join((title or "").lower().split())


def classify_cached(store, company_id: str, registry, titles: List[str]) -> List[Dict]:
    """classify(), but each distinct title is only ever sent to the model once.

    Two reasons this matters more than the saved tokens: the same events reappear in the feed
    every day, and a cached verdict cannot change its mind. Even at temperature 0 a model
    upgrade would silently reclassify everything mid-week.
    """
    client = store.get_company_by_id(company_id) if store else None
    cache = dict((client.data or {}).get(CACHE_KEY) or {}) if client else {}

    unknown = [t for t in dict.fromkeys(titles) if _cache_key(t) not in cache]
    for row in classify(registry, unknown):
        cache[_cache_key(row["title"])] = {"keep": row["keep"], "why": row["why"]}

    if client is not None and unknown:
        if len(cache) > CACHE_MAX:                    # keep the most recent, drop the rest
            cache = dict(list(cache.items())[-CACHE_MAX:])
        client.data = {**(client.data or {}), CACHE_KEY: cache}
        store.upsert_company(client)

    return [{"title": t, **cache.get(_cache_key(t), {"keep": True, "why": ""})} for t in titles]


def forget(store, company_id: str, titles: List[str]) -> int:
    """Drop cached verdicts so they are re-judged — used when you correct a decision."""
    client = store.get_company_by_id(company_id)
    if client is None:
        return 0
    cache = dict((client.data or {}).get(CACHE_KEY) or {})
    removed = sum(1 for t in titles if cache.pop(_cache_key(t), None) is not None)
    client.data = {**(client.data or {}), CACHE_KEY: cache}
    store.upsert_company(client)
    return removed


def split(registry, titles: List[str]):
    """(kept, rejected) as lists of {title, keep, why}. Uncached — prefer `split_cached`."""
    rows = classify(registry, titles)
    return [r for r in rows if r["keep"]], [r for r in rows if not r["keep"]]


def split_cached(store, company_id: str, registry, titles: List[str]):
    rows = classify_cached(store, company_id, registry, titles)
    return [r for r in rows if r["keep"]], [r for r in rows if not r["keep"]]
