"""Answering Luma registration questionnaires, and remembering the answers.

Most Luma events ask something before letting you in: your company, your role, what you are
building, how you heard about it. Skipping those events would quietly discard most of the list,
so instead we keep an answer bank and fill what we can.

Two rules shape this:

  • **Never invent an answer.** A wrong answer to "what are you building" is worse than not
    registering, because it goes to a real host who reads it. Anything we cannot answer from the
    bank is reported back for a human, and the event waits.
  • **Every answer is learned.** Once you answer "what's your company size" for one event, the
    same question at the next event is filled automatically. The bank only grows.

Matching is deliberately dumb and predictable: normalise the question text, then look for a
known intent (name / email / company / role / …) by keyword. No model decides what a question
means, because a confident wrong guess is exactly the failure mode we cannot see.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Intents we can answer from a profile without anyone typing anything. Order matters: the first
# pattern that matches wins, so the more specific ones come first.
INTENTS: List[Tuple[str, "re.Pattern"]] = [
    ("linkedin", re.compile(r"linked\s*-?\s*in", re.I)),
    ("twitter", re.compile(r"\b(twitter|x\.com|handle)\b", re.I)),
    # Hackathons ask for this constantly and it blocked a registration outright.
    ("github", re.compile(r"\b(github|git hub)\b", re.I)),
    # "domain" belongs here, not under `company`: "What is your company domain?" contains the
    # word "company", matched the company intent and answered "Acme" on a live form. Both this
    # and `building` sit above `company` for the same reason — the word appearing is not the
    # same as the field being asked for.
    ("website", re.compile(r"\b(website|url|site|homepage|domain)\b", re.I)),
    ("email", re.compile(r"\b(e-?mail|contact address)\b", re.I)),
    ("phone", re.compile(r"\b(phone|mobile|cell|whatsapp)\b", re.I)),
    # Before "company", because "In one sentence, what does your startup/company do?" contains
    # the word "company" and was answered "Acme" — the org's NAME offered as a description of
    # what it does. Asking what an organisation does is the `building` blurb, never its name.
    ("building", re.compile(r"\b(what (do|does) (your |the )?(company|startup|business|team|"
                            r"organi[sz]ation) do|in one sentence)\b", re.I)),
    ("company", re.compile(r"\b(company|organi[sz]ation|startup|employer|where do you work)\b", re.I)),
    ("role", re.compile(r"\b(role|title|position|job|what do you do)\b", re.I)),
    ("building", re.compile(r"\b(what are you (building|working on)|about (you|your)|"
                            r"tell us|describe|bio|one[- ]liner|elevator)\b", re.I)),
    # "How did you hear" was too literal: hosts write "Where did you hear about our event?",
    # "How did you find out about this?", "How did you learn about us?". Each variant escalated a
    # question whose answer was already stored, and a question the user has to answer twice is a
    # question they stop answering.
    ("referral", re.compile(
        r"\b((how|where|when) did you (hear|find|learn|come across|discover)"
        r"|how.{0,12}find out|referr?al|who invited|what brought you)\b", re.I)),
    ("dietary", re.compile(r"\b(dietary|allerg|food preference|vegetarian|vegan)\b", re.I)),
    ("first_name", re.compile(r"\bfirst\s*name\b", re.I)),
    ("last_name", re.compile(r"\b(last|sur)\s*name\b", re.I)),
    ("name", re.compile(r"\bname\b", re.I)),
]

# Deliberately EMPTY, and it must stay that way.
#
# This bank is what gets typed into a real event host's registration form. Shipping any default
# here means the tool submits somebody else's name and email to a stranger, on the first run,
# before the user has entered anything. There is no placeholder safe enough for that: a plausible
# value gets sent, and an implausible one gets sent too.
#
# So the answers start empty and are filled in by YOU, in the app (Event Outreach -> Answers) or
# through /outreach/questions. Anything unanswered is reported as an open question and the event
# is skipped rather than guessed at. See merge_defaults below: stored answers always win, so
# nothing is lost by starting from nothing.
DEFAULT_ANSWERS: Dict[str, str] = {}


def normalise(question: str) -> str:
    """Question text reduced to a stable key, so the same question phrased slightly differently
    at two events still hits the same stored answer."""
    q = (question or "").strip().lower()
    q = re.sub(r"\(.*?\)", " ", q)              # "(optional)", "(max 200 chars)"
    q = re.sub(r"[^a-z0-9\s]", " ", q)          # punctuation, asterisks marking required
    q = re.sub(r"\b(please|kindly|your|the|a|an|is|are|do|you|we|us)\b", " ", q)
    return re.sub(r"\s+", " ", q).strip()


# A question can MENTION a field without ASKING for it. "How many employees does your company
# have?" contains "company" but wants a number, and answering "Acme" would be a confident wrong
# answer sent to a real host. These disqualify keyword matching entirely, so the question is
# reported for a human instead.
_QUANTITATIVE = re.compile(
    r"\b(how many|how much|how long|how big|number of|size of|what size|headcount|"
    r"revenue|arr|budget|stage|raised|funding round|when did|which year)\b", re.I)
_YES_NO = re.compile(r"^\s*(are|do|did|have|has|will|would|can|is)\b.*\?\s*$", re.I)
# Open-ended questions that merely MENTION a field. "What's the biggest GTM challenge you
# encounter for your company?" contains "company" and was answered "Acme" on a real form — a
# host would have read that. The field word appearing is not the same as the field being asked
# for, and an opinion question is never answerable from a profile.
_OPEN_ENDED = re.compile(
    r"\b(biggest|hardest|main|top|favou?rite|challenge|challenges|problem|problems|pain|"
    r"goal|goals|hope|hoping|expect|looking for|interested in|why|opinion|thoughts|"
    r"describe|explain|share|excited)\b", re.I)


def intent_of(question: str, closed: bool = False) -> Optional[str]:
    """Which known field this question is asking for, or None if we don't recognise it.

    Returns None generously. An unrecognised question costs one prompt to the user, once, and
    is then remembered forever. A misrecognised one sends nonsense to a host.

    `closed=True` for a question with a fixed option list. The guards below exist because free
    text is ambiguous — "How to describe your position?" trips `describe`, and rightly so when
    the answer is a sentence someone will read. But that question appears on a live form as a
    dropdown offering exactly ["Founder", "Investor", "Engineer", ...], and the option list
    removes the ambiguity the guards protect against. The answer still has to equal one of the
    options exactly (`match_option`), so nothing can be invented — a guess that isn't on the
    list is simply dropped.
    """
    q = (question or "").strip()
    if not q:
        return None
    if not closed and (_QUANTITATIVE.search(q) or _YES_NO.match(q) or _OPEN_ENDED.search(q)):
        return None
    for intent, pattern in INTENTS:
        if pattern.search(q):
            return intent
    return None


def answer_for(question: str, answers: Dict[str, str],
               options: Optional[List[str]] = None) -> Optional[str]:
    """Best stored answer for a question.

    Exact normalised text first (something the user answered before, verbatim), then the field
    intent. Returns None rather than guessing — an unanswered question is reported, never faked.
    """
    key = normalise(question)
    if key and answers.get(key):
        return answers[key]
    intent = intent_of(question, closed=bool(options))
    if intent and answers.get(intent):
        return answers[intent]
    return None


def match_option(value: Optional[str], options: List[str]) -> Optional[str]:
    """The option a stored answer corresponds to, or None.

    Luma's choice fields are custom widgets: you cannot type into them, you can only click one of
    the offered rows. So an answer that does not correspond to an offered option is not an answer
    — "Founder" against `Yes -- I manage product people` / `No -- I do not have direct management
    responsibility` is a question for the user, not something to resolve by picking the closest
    string. Matching is exact on normalised text for that reason: a fuzzy match here would be a
    claim made about the user, to a host who reads it.
    """
    if not value or not options:
        return None
    target = normalise(value)
    for opt in options:
        if normalise(opt) == target:
            return opt
    return None


def plan_answers(questions: List[dict], answers: Dict[str, str]) -> Tuple[Dict[str, str], List[dict]]:
    """(filled, unanswered) for one event's form.

    `questions` are `{id, label, type, required, options}` as scraped from the page. Optional
    questions we cannot answer are left blank rather than blocking the registration; only a
    REQUIRED question we cannot answer stops it.
    """
    filled: Dict[str, str] = {}
    unanswered: List[dict] = []
    for q in questions or []:
        label = q.get("label") or ""
        options = q.get("options") or []
        value = answer_for(label, answers, options)
        # A choice question is only answered if the answer is one of the offered options.
        if value and (q.get("type") == "choice" or options):
            value = match_option(value, options)
        if value:
            filled[q.get("id") or label] = value
            continue
        if q.get("required"):
            unanswered.append({
                "id": q.get("id") or label,
                "label": label,
                "type": q.get("type") or "text",
                "options": options,
                "key": normalise(label),
                "intent": intent_of(label) or "",
            })
    return filled, unanswered


# --------------------------------------------------------------------------- #
# Which ticket to take                                                          #
# --------------------------------------------------------------------------- #
# A Luma event can offer several tiers at once. A real example from the SF feed:
#
#   Complimentary - with Approval   free    SOLD OUT
#   Suggested Donation              $25
#   Volunteer                       free    3 left
#   SPONSOR TABLE (3-6 FT)          $500
#   Livestream                      free
#
# The event-level `ticket_info` reported `is_free: true, is_sold_out: false`, which is true of
# the event and useless for deciding what to click. Anything that just presses the button could
# land on the $500 tier.
_PAID = "fiat-price"
# Free, but not a plain attendee ticket. Volunteering means working the event; sponsor, speaker
# and press tiers claim a role that isn't ours to claim.
_ROLE_TIER = re.compile(r"\b(volunteer|sponsor|vendor|exhibitor|speaker|press|media|staff|crew|"
                        r"organi[sz]er|mentor|judge)\b", re.I)
_REMOTE_TIER = re.compile(r"\b(livestream|live stream|virtual|online|remote|zoom|webinar)\b", re.I)


def pick_ticket(tiers: List[dict]) -> Tuple[Optional[dict], str]:
    """(tier, reason) — the tier to register with, or (None, why not).

    Order: free in-person, then free livestream. Never anything with a price, a role
    obligation, or no spots left.
    """
    # No tiers at all is the COMMON case: a plain free RSVP with a single Register button.
    # Reading it as "no free tier" skipped 47 of 122 real events — the majority — because the
    # absence of ticketing looked identical to the absence of a free option.
    if not tiers:
        return {"name": "", "cents": None, "type": "free"}, "in-person"

    usable = []
    for t in tiers or []:
        if t.get("is_disabled") or t.get("is_hidden"):
            continue
        # Two independent checks on purpose: `type` is the field Luma sets, `cents` is the money.
        # Either one being wrong should not be enough to spend the user's money.
        if t.get("type") == _PAID or t.get("cents") or t.get("min_cents"):
            continue
        if t.get("spots_remaining") == 0:
            continue
        name = t.get("name") or ""
        if _ROLE_TIER.search(name):
            continue
        usable.append((1 if _REMOTE_TIER.search(name) else 0, t))

    if not usable:
        if any((t.get("type") != _PAID and not t.get("cents")) for t in tiers or []):
            return None, "free tier sold out or role-only"
        return None, "no free tier"

    usable.sort(key=lambda pair: pair[0])       # in-person (0) before livestream (1)
    remote, tier = usable[0]
    return tier, "livestream" if remote else "in-person"


def merge_defaults(stored: Dict[str, str]) -> Dict[str, str]:
    """Stored answers win; defaults only fill gaps."""
    return {**DEFAULT_ANSWERS, **{k: v for k, v in (stored or {}).items() if v}}
