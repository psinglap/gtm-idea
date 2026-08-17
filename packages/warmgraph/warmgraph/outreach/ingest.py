"""What the browser extension posts back, and the queue it pulls from.

The extension owns everything that needs a logged-in browser: enumerating Luma events,
registering for them, pulling guest lists, and reading LinkedIn profiles. The server owns the
durable state. These functions are the seam between the two.

Shapes here mirror Luma's own API responses (verified live), so the extension can forward what
it already has instead of reshaping it:
  • an event entry carries `event.{api_id,name,url,start_at,end_at,show_guest_list}`,
    `guest_info.{ticket_key,approval_status}`, `guest_count`, `ticket_info.{is_free,…}`
  • a guest carries `user.{api_id,name,avatar_url,bio_short,linkedin_handle,…}`

Guest-list access needs BOTH `approval_status == "approved"` AND `show_guest_list == true` —
anything else 403s, so `scannable()` encodes that rather than letting the worker find out.
"""
from __future__ import annotations

import re
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from warmgraph.dates import parse_datetime
from warmgraph.entities import (
    EventContact,
    EventRegistration,
    RawEvent,
    RawPerson,
    contact_key,
)
from warmgraph.outreach import profile_cache, template
from warmgraph.outreach.template import short_event_name, strip_decoration

LUMA = "luma"

# Luma's SF discovery feed, read off luma.com/sf's own __NEXT_DATA__ rather than guessed. A wrong
# id here does not error loudly — `/discover/get-paginated-events` just returns HTTP 400 and the
# stage quietly registers only for standing invitations, which is how a run covered 6 events
# instead of the ~90 step 1 had found. Override per city with WG_LUMA_DISCOVER_PLACE.
#
# To find another city's id: open luma.com/<city> and grep the page source for `discplace-`.
LUMA_DISCOVER_PLACE = os.getenv("WG_LUMA_DISCOVER_PLACE", "discplace-BDj7GNbGlsF7Cka")


def discover_url(place_api_id: str = "", limit: int = 50, cursor: str = "") -> str:
    """The discovery feed URL. Entries come back in the same shape as `/home/get-events`, so
    `event_from_luma` handles both — the only difference is `guest_info` is null when you have
    no relationship to the event yet."""
    from urllib.parse import urlencode
    params = {"discover_place_api_id": place_api_id or LUMA_DISCOVER_PLACE,
              "pagination_limit": limit}
    if cursor:
        params["pagination_cursor"] = cursor
    return "https://api.luma.com/discover/get-paginated-events?" + urlencode(params)

# How far ahead we bother registering.
#
# This was 7, chosen when every attendee needed a throttled LinkedIn read and the pipeline could
# only digest about one event a day. Apollo now answers ~86% of profiles over an API, so that
# constraint is largely gone — and 7 days was the binding limit on the funnel, not the reads:
# of 156 standing invitations only 7 fell inside the window, so 149 were ignored on every run.
# Registering earlier also wins more places, because good events sell out.
EVENT_HORIZON_DAYS = int(os.getenv("WG_EVENT_HORIZON_DAYS", "14") or 14)


def starts_within(event: RawEvent, days: int = EVENT_HORIZON_DAYS,
                  now: Optional[datetime] = None) -> bool:
    """True if the event starts between now and `days` from now."""
    now = now or datetime.now(timezone.utc)
    start = event.starts_at
    if start is None:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return now <= start <= now + timedelta(days=days)


# --------------------------------------------------------------------------- #
# events                                                                        #
# --------------------------------------------------------------------------- #
def linkedin_url(handle: str) -> str:
    """Luma stores a handle, sometimes a full URL. Normalise to a profile URL.

    Luma actually sends `/in/some-handle` — with a LEADING SLASH. The previous version only stripped
    the `in/` prefix when the string began with `in/`, which "/in/…" does not, so every profile
    came out as linkedin.com/in/**in**/some-handle. Harmless-looking, and it broke two things at
    once: the URL 404s, and Apollo enrichment keyed on it matches nobody. Strip the slashes
    FIRST, then the prefix.
    """
    h = (handle or "").strip()
    if not h:
        return ""
    h = h.split("?")[0].rstrip("/")
    if h.startswith("http"):
        return h
    h = h.replace("www.linkedin.com", "").replace("linkedin.com", "").lstrip("/")
    if h.startswith("in/"):
        h = h[3:]
    h = h.lstrip("/")
    return f"https://www.linkedin.com/in/{h}" if h else ""


def event_from_luma(entry: dict) -> RawEvent:
    """One `/home/get-events` (or `/discover/get-paginated-events`) entry -> a global RawEvent.

    ONLY public facts about the event go here — name, url, time, city, price. Anything about
    OUR relationship with it (ticket key, approval status, whether we scanned it) belongs on
    `EventRegistration`, because `raw_events` is deduped by url and therefore SHARED between
    clients who attend the same event.
    """
    ev = entry.get("event") or {}
    ticket = entry.get("ticket_info") or {}
    city = entry.get("featured_city") or {}
    name = (ev.get("name") or "").strip()
    url = (ev.get("url") or "").strip()
    if url and not url.startswith("http"):
        url = f"https://luma.com/{url.lstrip('/')}"

    return RawEvent(
        source=LUMA,
        url=url,
        title=name,
        description=(ev.get("description") or "")[:500],
        starts_at=parse_datetime(ev.get("start_at") or entry.get("start_at") or ""),
        city=(city.get("name") if isinstance(city, dict) else str(city or "")) or "",
        is_virtual=(ev.get("location_type") or "") == "virtual",
        raw={
            "luma_event_id": ev.get("api_id") or entry.get("api_id") or "",
            "end_at": ev.get("end_at") or "",
            "is_free": bool(ticket.get("is_free", True)),
            "price": ticket.get("price"),
            "require_approval": bool(ticket.get("require_approval")),
            "is_sold_out": bool(ticket.get("is_sold_out")),
            # What a person actually recalls about the event — see recall_name().
            "venue": ((ev.get("geo_address_info") or {}).get("address") or "").strip(),
            "calendar_name": ((entry.get("calendar") or {}).get("name") or "").strip(),
            "host_names": [h.get("name", "") for h in (entry.get("hosts") or []) if h.get("name")],
        },
    )


# A street address is not a memory. Rejects "760 Market St" and "1412 Market St" while keeping
# "AWS Builder Loft" and "JouJou".
_STREET_START = re.compile(r"^\d")
_STREET_END = re.compile(
    r"\b(st|street|ave|avenue|rd|road|blvd|boulevard|way|dr|drive|ln|lane|"
    r"floor|fl|suite|ste|#\d+)\.?$", re.I)
# Luma calendars people never renamed. "Events" tells the reader nothing.
_GENERIC_CALENDAR = re.compile(r"^(events?|calendar|my calendar|community|home)$", re.I)


def _is_street_address(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and (bool(_STREET_START.match(v)) or bool(_STREET_END.search(v)))


def recall_name(event: RawEvent) -> tuple:
    """(name, source) — what to call this event so the recipient actually places it.

    Nobody remembers "Frontier Signals #01". They remember being at AWS Builder Loft. Measured
    over 304 real events: a usable venue name exists 45% of the time, and the Luma calendar
    (the community brand) is meaningful for 99.7%. Neither alone is enough — and the two fall
    through in opposite directions, which is why this is a chain:
      • Frontier Signals #01 -> venue "AWS Builder Loft"  (calendar is the generic "Events")
      • Physical AI Showcase -> calendar "Angel Launch"   (venue is the street "760 Market St")
    """
    # TITLE FIRST, then venue, then calendar.
    #
    # This used to lead with the venue, on the reasoning that people remember the room rather
    # than the billing. That is true for LOCATING an event and wrong for NAMING one: an attendee
    # of "Wild AI SF #1" got "I was at Frontier Tower", which names the building and not the
    # thing they came to. The venue is still valuable, so it now goes in the body sentence
    # alongside the name — see template.event_place().
    title = strip_decoration(short_event_name(event.title))
    if title:
        return title, "title"
    raw = event.raw or {}
    venue = strip_decoration((raw.get("venue") or "").strip())
    if venue and not _is_street_address(venue):
        return venue, "venue"
    calendar = strip_decoration((raw.get("calendar_name") or "").strip())
    if calendar and not _GENERIC_CALENDAR.match(calendar):
        return calendar, "calendar"
    return "", "none"


def registration_from_luma(entry: dict, company_id: str, event: RawEvent) -> EventRegistration:
    """The personal half: this client's ticket key, approval status and guest-list visibility."""
    ev = entry.get("event") or {}
    guest_info = entry.get("guest_info") or {}
    return EventRegistration(
        company_id=company_id,
        event_id=event.id,
        ticket_key=guest_info.get("ticket_key") or "",
        approval_status=guest_info.get("approval_status") or "",
        show_guest_list=bool(ev.get("show_guest_list")),
        guest_count=entry.get("guest_count") or 0,
        short_name=recall_name(event)[0],
        short_name_source=recall_name(event)[1],
    )


def has_ended(event: RawEvent, now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    end = parse_datetime(str((event.raw or {}).get("end_at") or "")) or event.starts_at
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return end < now


def registerable(event: RawEvent, reg: Optional[EventRegistration]) -> bool:
    """The auto-registration rules, in one place: free, not sold out, not already registered.

    Paid events are excluded unconditionally — spending money is never something this decides.
    """
    raw = event.raw or {}
    if not raw.get("is_free", True) or raw.get("price"):
        return False
    if raw.get("is_sold_out"):
        return False
    status = reg.approval_status if reg else ""
    return status not in ("approved", "pending_approval")


def ingest_events(store, company_id: str, entries: List[dict]) -> Dict[str, int]:
    """Upsert what the extension enumerated: the shared event, plus THIS client's registration."""
    out = {"seen": 0, "scannable": 0, "registerable": 0}
    for entry in entries or []:
        event = event_from_luma(entry)
        if not (event.url or event.title):
            continue
        # Indexed lookup by url: the previous shape re-read the entire events table for every
        # entry in the payload, so a 100-event sync meant 100 full table loads.
        existing = store.get_raw_event_by_url(event.url or event.id)
        if existing is not None:
            event.id = existing.id
        store.upsert_raw_event(event)

        reg = registration_from_luma(entry, company_id, event)
        prior = store.get_event_registration(company_id, event.id)
        if prior is not None:
            reg.id = prior.id
            reg.created_at = prior.created_at
            reg.scanned_at = prior.scanned_at          # never re-scan an event we already did
            reg.registered_at = prior.registered_at
            if prior.short_name_edited:                # a user-edited subject line sticks
                reg.short_name = prior.short_name
                reg.short_name_edited = True
        store.upsert_event_registration(reg)

        out["seen"] += 1
        out["scannable"] += 1 if reg.scannable else 0
        out["registerable"] += 1 if registerable(event, reg) else 0
    return out


def mark_scanned(store, company_id: str, event_id: str) -> Optional[EventRegistration]:
    reg = store.get_event_registration(company_id, event_id)
    if reg is None:
        return None
    reg.scanned_at = datetime.now(timezone.utc).isoformat()
    return store.upsert_event_registration(reg)


# How far back to look for events that have ended.
#
# This must match the age at which outreach is still allowed, and it did not: the window was 3
# days while template.MAX_EVENT_AGE_DAYS lets us write about an event for 14. Everything that
# ended between those two numbers was never scanned and never could be — the queue simply
# returned nothing, which reads identically to "there is nothing to do". Measured on the live
# database: 74 past events with visible guest lists had gone unscanned, holding 19,537 guests.
#
# Tied to the send rule rather than restated, so the two cannot drift apart again. Scanning
# further back than we are willing to email would only queue people who are then suppressed as
# event_too_old; scanning less far loses people we could still legitimately write to.
#
# It is NOT the trigger; the trigger is `has_ended`, a live comparison against the clock, so an
# event becomes eligible the minute it finishes.
SCAN_LOOKBACK_DAYS = int(os.getenv("WG_SCAN_LOOKBACK_DAYS", "") or template.MAX_EVENT_AGE_DAYS)


def pending_scans(store, company_id: str,
                  lookback_days: int = SCAN_LOOKBACK_DAYS) -> List[tuple]:
    """[(event, registration)] for events that ended, this client is approved for, and that
    haven't been scanned yet. No cap: scanning is one API call per page, and the LinkedIn budget
    downstream is the real governor.

    The window bands on the event's OWN end time, not on when we happened to save the row.
    Filtering by `created_at` (which is what `get_raw_events(since_days=…)` does) quietly gets
    both cases wrong once this has been running a while: an event ingested today but held months
    ago becomes a candidate, and an event first saved four days ago that ended last night drops
    out of a three-day window entirely. Neither is visible in a summary — the queue just comes
    back short.
    """
    regs = {r.event_id: r for r in store.get_event_registrations(company_id, limit=2000)}
    now = datetime.now(timezone.utc)
    floor = now - timedelta(days=max(1, lookback_days))
    out = []
    # Read generously by created_at, then band precisely by end_at. The rows are cheap; getting
    # the boundary right is not.
    for e in store.get_raw_events(since_days=max(30, lookback_days * 4), limit=1000):
        if e.source != LUMA:
            continue
        reg = regs.get(e.id)
        if reg is None or reg.scanned_at or not reg.scannable:
            continue
        if not has_ended(e, now):
            continue
        ended = parse_datetime(str((e.raw or {}).get("end_at") or "")) or e.starts_at
        if ended is None or ended < floor:
            continue
        out.append((e, reg))
    return out


# --------------------------------------------------------------------------- #
# guests                                                                        #
# --------------------------------------------------------------------------- #
def guest_priority(event: RawEvent, guest: dict, now: Optional[datetime] = None) -> int:
    """Who gets read first inside the day's LinkedIn budget. Recent events first, and within
    them people who wrote a bio — everyone is still read eventually, this is only ordering."""
    now = now or datetime.now(timezone.utc)
    starts = event.starts_at
    if starts is not None and starts.tzinfo is None:
        starts = starts.replace(tzinfo=timezone.utc)
    days_ago = (now - starts).days if starts else 60
    recency = max(0, 60 - min(days_ago, 60))
    return recency * 2 + (5 if (guest.get("bio_short") or "").strip() else 0)


def ingest_guests(store, company_id: str, event: RawEvent, guests: List[dict],
                  now: Optional[datetime] = None,
                  self_keys: Optional[set] = None) -> Dict[str, int]:
    """Guest list -> raw_people (bronze) + event_contacts (the queue).

    Attendees with no LinkedIn handle are recorded as `no_linkedin` and never queued: the hard
    gate means they can never be judged, so queuing them would clog the budget forever.
    """
    out = {"guests": 0, "queued": 0, "no_linkedin": 0, "new": 0,
           "known_profile": 0, "known_verdict": 0, "self": 0}
    rows: List[EventContact] = []
    raws: List[RawPerson] = []

    for g in guests or []:
        user = g.get("user") or g
        name = (user.get("name") or " ".join(
            x for x in [user.get("first_name"), user.get("last_name")] if x)).strip()
        if not name:
            continue
        out["guests"] += 1
        li = linkedin_url(user.get("linkedin_handle") or "")
        luma_id = (user.get("api_id") or "").strip()
        key = contact_key(li, luma_id)
        if not key:
            continue
        # You are on your own guest list. Drop yourself here rather than spending a LinkedIn
        # read and an Apollo credit before the own-domain check catches it at send time.
        if self_keys and key in self_keys:
            out["self"] += 1
            continue

        rows.append(EventContact(
            company_id=company_id, event_id=event.id, contact_key=key,
            luma_user_id=luma_id, name=name,
            linkedin_url=li, luma_bio=(user.get("bio_short") or "").strip(),
            avatar_url=(user.get("avatar_url") or "").strip(),
            status="queued" if li else "no_linkedin",
            priority=guest_priority(event, user, now),
        ))
        if li:
            out["queued"] += 1
            raws.append(RawPerson(
                source="luma-guest", person=name, linkedin_url=li,
                where_active=["luma"], event_refs=[event.id],
                raw={"luma": user, "event_url": event.url}))
        else:
            out["no_linkedin"] += 1

    # Reuse what we already know about these people. The SF circuit repeats: a profile read
    # 15-20s ago this month should not be paid for again, and a verdict we already reached for
    # this client should not cost another LLM call.
    for row in rows:
        if row.status != "queued":
            continue
        _row, hit = profile_cache.apply_cache(store, company_id, row)
        if hit == "profile":
            out["known_profile"] += 1
            out["queued"] -= 1
        elif hit == "verdict":
            out["known_verdict"] += 1
            out["queued"] -= 1

    if raws:
        store.save_raw_people(raws)
    if rows:
        out["new"] = store.save_event_contacts(rows)
    return out


# --------------------------------------------------------------------------- #
# the LinkedIn work queue                                                       #
# --------------------------------------------------------------------------- #
def record_linkedin(store, company_id: str, contact_id: str, headline: str = "",
                    profile_text: str = "", gated: bool = False,
                    max_attempts: int = 3) -> Optional[EventContact]:
    """One profile, checkpointed the moment the extension reads it.

    A gated or empty read is retried on a later day rather than trusted — LinkedIn serves an
    auth wall intermittently, and treating that as "no data about this person" would silently
    drop real targets. After `max_attempts` the row is retired as `unreadable`, because the hard
    gate means there is no way to judge them.
    """
    contact = store.get_event_contact(contact_id)
    if contact is None or contact.company_id != company_id:
        return None

    text = (profile_text or "").strip()
    if gated or not (headline or text):
        if contact.attempts >= max_attempts:
            contact.status = "unreadable"
            contact.last_error = "LinkedIn profile could not be read (gated or empty)"
        else:
            contact.status = "queued"
            contact.last_error = "gated read, will retry"
        contact.leased_by, contact.lease_expires_at = "", None
        store.update_event_contacts([contact])
        return contact

    contact.linkedin_headline = (headline or "").strip()[:400]
    contact.linkedin_text = text[:4000]
    contact.status = "profiled"
    contact.leased_by, contact.lease_expires_at = "", None
    contact.last_error = ""
    store.update_event_contacts([contact])
    # Cache it globally so the next event this person attends costs no read at all.
    person = profile_cache.remember_profile(store, contact)
    if person is not None and not contact.person_id:
        contact.person_id = person.id
        store.update_event_contacts([contact])
    return contact
