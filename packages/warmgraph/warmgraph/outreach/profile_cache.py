"""Remember profiles we have already read, and verdicts we have already reached.

The SF event circuit is small: the same people turn up at event after event. Reading a LinkedIn
profile costs 15-20 seconds of throttled browser time and carries real account risk, and judging
costs an LLM call. Doing either twice for the same person is pure waste.

Both live on the existing global `people` row, keyed by LinkedIn URL (an indexed lookup):

  people.data["linkedin"]          -> {headline, text, read_at}       GLOBAL, shared by everyone
  people.data["icp"][company_id]   -> {verdict, score, reason, at}    PER CLIENT

The split matters. Someone's LinkedIn profile is the same fact for every client, but whether
they match an ICP is a judgement one client makes and another would make differently.

Freshness: a profile older than PROFILE_TTL_DAYS is re-read, because people change jobs and the
whole judgement turns on their CURRENT role. A verdict is only trusted as long as the profile it
was based on — re-reading a profile invalidates the verdict with it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from warmgraph.entities import EventContact, Person

PROFILE_TTL_DAYS = 120


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _fresh(ts: str, max_age_days: int) -> bool:
    read_at = _parse(ts)
    return read_at is not None and (_now() - read_at) < timedelta(days=max_age_days)


def cached_profile(person: Optional[Person], max_age_days: int = PROFILE_TTL_DAYS) -> Optional[dict]:
    """The stored LinkedIn read, if we have one and it is still fresh."""
    if person is None:
        return None
    li = (person.data or {}).get("linkedin") or {}
    if not (li.get("headline") or li.get("text")):
        return None
    return li if _fresh(li.get("read_at", ""), max_age_days) else None


def cached_verdict(person: Optional[Person], company_id: str,
                   max_age_days: int = PROFILE_TTL_DAYS) -> Optional[dict]:
    """This client's previous judgement of this person, if the profile behind it is still fresh."""
    if person is None or not company_id:
        return None
    verdict = ((person.data or {}).get("icp") or {}).get(company_id)
    if not verdict or not verdict.get("verdict"):
        return None
    return verdict if _fresh(verdict.get("at", ""), max_age_days) else None


def remember_profile(store, contact: EventContact) -> Optional[Person]:
    """Persist a LinkedIn read against the global person, creating the row if needed."""
    url = (contact.linkedin_url or "").strip()
    if not url or not (contact.linkedin_headline or contact.linkedin_text):
        return None
    person = store.get_person_by_linkedin(url) or Person(
        person=contact.name, linkedin_url=url, where_active=["linkedin", "luma"])
    person.data = {**(person.data or {}), "linkedin": {
        "headline": contact.linkedin_headline,
        "text": contact.linkedin_text,
        "read_at": _now().isoformat(),
    }}
    if contact.name and not person.person:
        person.person = contact.name
    person.updated_at = _now()
    store.save_people([person])
    return person


def remember_verdict(store, company_id: str, contact: EventContact) -> Optional[Person]:
    """Persist this client's verdict so the same person is not re-judged at the next event."""
    url = (contact.linkedin_url or "").strip()
    if not url or not contact.verdict:
        return None
    person = store.get_person_by_linkedin(url)
    if person is None:
        return None
    icp = {**((person.data or {}).get("icp") or {})}
    icp[company_id] = {
        "verdict": contact.verdict, "score": contact.score, "reason": contact.reason,
        "judged_by": contact.judged_by, "at": _now().isoformat(),
    }
    person.data = {**(person.data or {}), "icp": icp}
    person.updated_at = _now()
    store.save_people([person])
    return person


def apply_cache(store, company_id: str, contact: EventContact,
                max_age_days: int = PROFILE_TTL_DAYS) -> Tuple[EventContact, str]:
    """Pre-fill a freshly scanned attendee from what we already know.

    Returns (contact, hit) where hit is "" | "profile" | "verdict". A verdict hit skips both the
    LinkedIn read and the LLM call; a profile hit skips only the read.
    """
    url = (contact.linkedin_url or "").strip()
    if not url:
        return contact, ""
    person = store.get_person_by_linkedin(url)
    profile = cached_profile(person, max_age_days)
    if not profile:
        return contact, ""

    contact.linkedin_headline = profile.get("headline", "")
    contact.linkedin_text = profile.get("text", "")
    contact.person_id = person.id
    contact.status = "profiled"

    verdict = cached_verdict(person, company_id, max_age_days)
    if not verdict:
        return contact, "profile"

    contact.verdict = verdict["verdict"]
    contact.score = float(verdict.get("score") or 0)
    contact.reason = verdict.get("reason", "")
    contact.judged_by = (verdict.get("judged_by") or "") + " (cached)"
    contact.status = "judged" if contact.verdict == "target" else "rejected"
    return contact, "verdict"
