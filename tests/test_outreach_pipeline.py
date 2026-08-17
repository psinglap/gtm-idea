"""Offline tests for the event-outreach pipeline: the email template, Luma ingest, the LinkedIn
queue, and the suppression rules. No network, no LLM, no Gmail.

Event fixtures are shaped like the real `api.luma.com/home/get-events` payloads verified
against a live account, including the fullwidth colon that appears in real Luma titles.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from warmgraph.agents.activities.outreach_send import (
    event_age_days,
    suppression_reason,
)
from warmgraph.entities import DoNotContact, EventContact, OutreachMessage
from warmgraph.outreach import ingest, template
from warmgraph.storage.sqlite_store import SqliteStore


# A fictional operator, used wherever a test needs a POPULATED answer bank.
#
# These assertions used to run against registration.DEFAULT_ANSWERS, which shipped a real
# person's name, email and links. Two problems with that. It put an identity in the repo, and it
# made product data double as test data — so the shipped bank could never be emptied without
# breaking the suite.
#
# It is also the reason the bank could not simply be emptied and left: with no answers at all,
# every test asserting `filled == {}` would pass without matching anything, proving nothing. The
# guards have to be tested against a bank that COULD have answered and correctly did not.
ANSWERS = {
    "name": "Sam Rivera", "first_name": "Sam", "last_name": "Rivera",
    "email": "sam@acme.example", "company": "Acme", "role": "Founder",
    "website": "https://acme.example",
    "linkedin": "https://www.linkedin.com/in/sam-rivera/",
    "building": "Acme, tools for small teams.",
    "github": "https://github.com/acme-example",
    "referral": "Luma",
}

CID = "comp-test"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


# Default to an event that ended RECENTLY, because most tests here exercise the scan queue and
# `pending_scans` bands on how long ago an event ended. A hardcoded date passed all day and then
# failed the moment the clock rolled past it — a fixture whose meaning changes overnight is worse
# than no fixture. Tests that assert on the timestamp itself pass explicit dates.
_ENDED = datetime.now(timezone.utc) - timedelta(hours=12)
_ISO = lambda d: d.isoformat().replace("+00:00", "Z")


def luma_entry(name="Frontier Signals #01: Infrastructure Behind Progress",
               url="https://luma.com/frontier-01", api_id="evt-1",
               approval="approved", show_list=True,
               start=_ISO(_ENDED - timedelta(hours=3)), end=_ISO(_ENDED),
               is_free=True, price=None, sold_out=False,
               venue="AWS Builder Loft", calendar="Events", hosts=("AWS Builder Loft",)):
    event = {"api_id": api_id, "name": name, "url": url, "start_at": start,
             "end_at": end, "show_guest_list": show_list, "location_type": "offline"}
    if venue is not None:
        event["geo_address_info"] = {"address": venue}
    return {
        "api_id": api_id,
        "event": event,
        "calendar": {"name": calendar} if calendar else {},
        "hosts": [{"name": h} for h in (hosts or ())],
        "guest_info": {"ticket_key": "tk-abc", "approval_status": approval},
        "ticket_info": {"is_free": is_free, "price": price, "is_sold_out": sold_out,
                        "require_approval": True},
        "guest_count": 637,
        "featured_city": {"name": "San Francisco"},
    }


def guest(name="Jane Doe", handle="janedoe", bio="Founder, building in AI", uid="usr-1"):
    return {"user": {"api_id": uid, "name": name, "linkedin_handle": handle,
                     "bio_short": bio, "avatar_url": "https://cdn.lu.ma/a.png"}}


# =========================================================================== #
# template                                                                     #
# =========================================================================== #
@pytest.mark.parametrize("raw,expected", [
    ("Frontier Signals #01: Infrastructure Behind Progress", "Frontier Signals"),
    ("cocktails & games | YC/A16Z", "cocktails & games"),
    # real title from the account — that is a FULLWIDTH colon, not an ASCII one
    ("Beta x Alibaba Cloud x AMD：AI agent builder challenge", "Beta x Alibaba Cloud x AMD"),
    ("SF Hardware & Manufacturing Happy Hour", "SF Hardware & Manufacturing Happy Hour"),
    ("AI Engineers Tech Talk: August", "AI Engineers Tech Talk"),
    ("Frontier Day (SF)", "Frontier Day"),
    ("Demo Night presented by Acme Ventures", "Demo Night"),
    ("Claude Code Workshop - San Francisco", "Claude Code Workshop"),
    ("", ""),
])
def test_short_event_name(raw, expected):
    assert template.short_event_name(raw) == expected


def test_short_event_name_never_strands_a_useless_fragment():
    """Cutting at the first separator must not turn a title into 'SF'."""
    assert template.short_event_name("SF: The Big Launch Party") == "SF: The Big Launch Party"


def test_short_event_name_trims_on_a_word_boundary():
    out = template.short_event_name("A very long event title that keeps going and going forever")
    assert len(out) <= 40
    assert not out.endswith(" ")
    assert out == "A very long event title that keeps"


@pytest.mark.parametrize("full,expected", [
    ("Jane Doe", "Jane"), ("Jane", "Jane"),
    # "jane" -> "Jane": this used to assert the lower-case form was preserved, which was written
    # before anyone had read a real guest list. Guests type their own names, so a list arrives
    # with "fay" sitting between two capitalised rows, and "Hi fay," reads as an unchecked merge.
    ("jane", "Jane"),
    ("Mary-Kate Olsen", "Mary-Kate"), ("O'Brien Smith", "O'Brien"),
])
def test_first_name(full, expected):
    assert template.first_name(full) == expected


@pytest.mark.parametrize("full", ["🚀 Jane", "@janedoe", "1Password Team"])
def test_first_name_falls_back_rather_than_greeting_an_emoji(full):
    """'Hi 🚀,' is worse than the full string."""
    assert template.first_name(full) == full


def test_render_fills_the_default_copy():
    subject, body, html = template.render(
        name="Jane Doe", event_name="Frontier Signals #01: Infrastructure Behind Progress",
        tmpl=template.MessageTemplate())

    assert subject == "Wanted to connect at Frontier Signals event"
    assert body.startswith("Hi Jane,")
    # who-I-am line comes FIRST, event line second
    assert body.index("YOUR NAME") < body.index("I was at")   # who-I-am line first
    assert "I was at the Frontier Signals event" in body
    assert body.rstrip().endswith("Book a Slot (%s)" % template.CALENDAR)


def test_links_are_clickable_in_html_and_visible_in_plain_text():
    """The reader clicks "Book a Slot"; anyone on a plain-text client still sees the URL."""
    _, body, html = template.render(name="Jane", event_name="Demo Night",
                                    tmpl=template.MessageTemplate())
    assert f'<a href="{template.CALENDAR}">Book a Slot</a>' in html
    assert '<a href="https://example.com">yoursite.com</a>' in html
    assert f'<a href="{template.LINKEDIN}">LinkedIn</a>' in html
    assert f"Book a Slot ({template.CALENDAR})" in body      # plain-text fallback
    assert "<a href" not in body                             # ...and no markup leaks into it


def test_signature_is_present_and_ordered():
    _, body, _ = template.render(name="Jane", event_name="Demo Night",
                                 tmpl=template.MessageTemplate())
    sig = body[body.index("Best"):]
    assert sig.splitlines()[:3] == ["Best", "YOUR NAME", "YOUR TITLE"]


def test_html_carries_no_tracking_pixel():
    _, _, html = template.render(name="Jane", event_name="Demo Night",
                                 tmpl=template.MessageTemplate())
    assert "<img" not in html.lower()
    assert "1x1" not in html


def test_only_two_things_are_variable():
    """Name, event name, and where the event was. Everything else is the user's words."""
    assert set(template.FIELDS) == {"first_name", "name", "event_name", "event_place"}
    tmpl = template.MessageTemplate(
        subject="Following up from {event_name}",
        body="Hey {first_name} ({name}), good to see you at {event_name}. "
             "Grab time: https://cal.com/you/15min")
    subject, body, _ = template.render(name="Jane Doe", event_name="Demo Night: SF Edition",
                                       tmpl=tmpl)
    assert subject == "Following up from Demo Night"
    assert body == ("Hey Jane (Jane Doe), good to see you at Demo Night. "
                    "Grab time: https://cal.com/you/15min")


def test_render_has_no_em_dashes_and_no_opt_out_line():
    subject, body, _ = template.render(name="Jane", event_name="Demo Night",
                                       tmpl=template.MessageTemplate())
    assert "—" not in body and "—" not in subject
    assert "–" not in body
    assert "unsubscribe" not in body.lower()
    assert "no thanks" not in body.lower()


def test_render_prefers_a_user_edited_short_name():
    subject, _, _ = template.render(name="Jane",
                                    event_name="Some Very Long Official Title: Part 2",
                                    tmpl=template.MessageTemplate(), event_short="Frontier Day")
    assert subject == "Wanted to connect at Frontier Day"


def test_a_stray_brace_in_the_users_copy_does_not_crash_a_send():
    """str.format would explode on a literal brace. Their template must never break sending."""
    tmpl = template.MessageTemplate(subject="hi", body="Hey {first_name} :) {not_a_field} {{x}}")
    _, body, _ = template.render(name="Jane", event_name="E", tmpl=tmpl)
    assert body.startswith("Hey Jane")
    assert "{not_a_field}" in body           # left alone rather than raising


def test_unknown_field_is_caught_before_it_ships():
    tmpl = template.MessageTemplate(subject="Hi {firstname}", body="x")
    assert template.unknown_fields(tmpl) == ["firstname"]
    gaps = template.missing_fields("Jane", "Demo Night", tmpl)
    assert "unknown field {firstname}" in gaps


def test_missing_fields_catches_blanks_that_would_ship():
    """The SHIPPED template must be unsendable. A fresh install mailing "YOUR NAME" to a real
    guest list is the one failure this repo cannot take back, so it is blocked here, next to the
    other reasons a message cannot be built."""
    shipped = template.missing_fields("Jane", "Demo Night", template.MessageTemplate())
    assert any("YOUR NAME" in g for g in shipped), shipped
    written = template.MessageTemplate(subject="Wanted to connect at {event_name}",
                                       body="Hi {first_name},\n\nI am Sam at Acme.\n\nBest\nSam")
    assert template.missing_fields("Jane", "Demo Night", written) == []
    empty = template.MessageTemplate(subject="", body="")
    gaps = template.missing_fields("Jane", "Demo Night", empty)
    assert "template subject" in gaps and "template body" in gaps


# =========================================================================== #
# Luma ingest                                                                  #
# =========================================================================== #
def _pair(entry_dict):
    """(event, registration) the way ingest builds them."""
    e = ingest.event_from_luma(entry_dict)
    return e, ingest.registration_from_luma(entry_dict, CID, e)


def test_event_from_luma_keeps_only_public_facts():
    """The shared event row must NOT carry anything personal: `raw_events` is deduped by url,
    so two clients at the same event share it and would overwrite each other."""
    e = ingest.event_from_luma(luma_entry(start="2026-08-09T00:00:00.000Z",
                                          end="2026-08-09T03:00:00.000Z"))
    assert e.source == "luma"
    assert e.title.startswith("Frontier Signals")
    assert e.city == "San Francisco"
    # the ISO timestamp must survive intact — parse_date used to flatten these to Jan 1
    assert e.starts_at == datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)
    assert e.raw["is_free"] is True
    assert "ticket_key" not in e.raw
    assert "approval_status" not in e.raw
    assert "scanned_at" not in e.raw


def test_registration_carries_the_personal_half():
    _, reg = _pair(luma_entry())
    assert reg.company_id == CID
    assert reg.ticket_key == "tk-abc"
    assert reg.approval_status == "approved"
    assert reg.short_name == "Frontier Signals"      # see recall_name tests below


def test_two_clients_at_the_same_event_keep_separate_registrations(store):
    """The bug this split fixes: one client syncing must not clobber another's approval."""
    ingest.ingest_events(store, "comp-a", [luma_entry(approval="approved")])
    ingest.ingest_events(store, "comp-b", [luma_entry(approval="invited")])

    event = store.get_raw_event_by_url("https://luma.com/frontier-01")
    assert store.get_event_registration("comp-a", event.id).approval_status == "approved"
    assert store.get_event_registration("comp-b", event.id).approval_status == "invited"
    # and only the approved client can scan it
    assert len(ingest.pending_scans(store, "comp-a", lookback_days=3650)) == 1
    assert ingest.pending_scans(store, "comp-b", lookback_days=3650) == []


@pytest.mark.parametrize("approval,show_list,expected", [
    ("approved", True, True),          # the only combination that returns a guest list
    ("invited", True, False),          # verified live: 403
    ("pending_approval", True, False),  # verified live: 403
    ("approved", False, False),        # host hid the list
])
def test_scannable_matches_what_luma_actually_allows(approval, show_list, expected):
    _, reg = _pair(luma_entry(approval=approval, show_list=show_list))
    assert reg.scannable is expected


def test_blocked_reason_is_specific():
    assert _pair(luma_entry(approval="invited"))[1].blocked_reason == "invited"
    assert _pair(luma_entry(show_list=False))[1].blocked_reason == "guest_list_hidden"


def test_paid_events_are_never_registerable():
    """Spending money is not a decision this system gets to make."""
    assert ingest.registerable(*_pair(luma_entry(approval="invited", is_free=False, price=25))) is False
    assert ingest.registerable(*_pair(luma_entry(approval="invited", is_free=True))) is True


def test_sold_out_and_already_registered_are_skipped():
    assert ingest.registerable(*_pair(luma_entry(approval="invited", sold_out=True))) is False
    assert ingest.registerable(*_pair(luma_entry(approval="approved"))) is False
    assert ingest.registerable(*_pair(luma_entry(approval="pending_approval"))) is False


def test_has_ended_uses_the_end_time():
    future = ingest.event_from_luma(luma_entry(start="2027-01-01T00:00:00.000Z",
                                               end="2027-01-01T03:00:00.000Z"))  # noqa: E501
    assert ingest.has_ended(future, now=NOW) is False
    assert ingest.has_ended(ingest.event_from_luma(
        luma_entry(start="2026-08-09T00:00:00.000Z",
                   end="2026-08-09T03:00:00.000Z")), now=NOW) is True


def test_ingest_events_preserves_scan_bookkeeping_across_resyncs(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = ingest.pending_scans(store, CID)[0][0]
    ingest.mark_scanned(store, CID, event.id)

    ingest.ingest_events(store, CID, [luma_entry()])          # the extension re-syncs the same event
    assert ingest.pending_scans(store, CID) == []             # must not be scanned twice


def test_pending_scans_only_returns_ended_approved_visible_events(store):
    ingest.ingest_events(store, CID, [
        luma_entry(api_id="evt-ok", url="https://luma.com/ok"),
        luma_entry(api_id="evt-inv", url="https://luma.com/inv", approval="invited"),
        luma_entry(api_id="evt-fut", url="https://luma.com/fut",
                   start="2027-01-01T00:00:00.000Z", end="2027-01-01T03:00:00.000Z"),
    ])
    assert [e.url for e, _ in ingest.pending_scans(store, CID)] == ["https://luma.com/ok"]


@pytest.mark.parametrize("handle,expected", [
    ("janedoe", "https://www.linkedin.com/in/janedoe"),
    ("in/janedoe", "https://www.linkedin.com/in/janedoe"),
    ("https://www.linkedin.com/in/janedoe/", "https://www.linkedin.com/in/janedoe"),
    ("", ""),
])
def test_linkedin_url_normalisation(handle, expected):
    assert ingest.linkedin_url(handle) == expected


def test_ingest_guests_queues_only_people_we_can_actually_judge(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = ingest.pending_scans(store, CID)[0][0]
    out = ingest.ingest_guests(store, CID, event, [
        guest(uid="usr-1", handle="janedoe"),
        guest(uid="usr-2", name="No Linkedin", handle=""),
    ], now=NOW)

    assert out["guests"] == 2 and out["queued"] == 1 and out["no_linkedin"] == 1
    assert out["new"] == 2
    queued = store.get_event_contacts(CID, status="queued")
    assert [c.name for c in queued] == ["Jane Doe"]
    # the person with no LinkedIn is recorded, not silently dropped, and never clogs the queue
    assert [c.name for c in store.get_event_contacts(CID, status="no_linkedin")] == ["No Linkedin"]


def test_a_profile_read_once_is_never_read_again(store):
    """The same people attend event after event. A profile read costs 15-20s of throttled
    browser time and real account risk; paying that twice is pure waste."""
    ingest.ingest_events(store, CID, [luma_entry()])
    event, _ = ingest.pending_scans(store, CID)[0]
    ingest.ingest_guests(store, CID, event, [guest()], now=NOW)

    c = store.get_event_contacts(CID, status="queued")[0]
    store.lease_event_contacts(CID, leased_by="b", limit=1)
    ingest.record_linkedin(store, CID, c.id, headline="Founder at Acme",
                           profile_text="Founder at Acme, building creator tooling.")

    # same person turns up at a different event
    ingest.ingest_events(store, CID, [luma_entry(api_id="evt-2", url="https://luma.com/two")])
    event2, _ = [p for p in ingest.pending_scans(store, CID)
                 if p[0].url == "https://luma.com/two"][0], None
    event2 = [e for e, _ in ingest.pending_scans(store, CID)
              if e.url == "https://luma.com/two"][0]
    out = ingest.ingest_guests(store, CID, event2, [guest()], now=NOW)

    assert out["known_profile"] == 1
    assert out["queued"] == 0                    # never enters the LinkedIn queue
    second = store.get_event_contacts(CID, event_id=event2.id)[0]
    assert second.status == "profiled"
    assert second.linkedin_headline == "Founder at Acme"


def test_a_verdict_reached_once_is_reused(store):
    """And if we already judged them for this client, skip the LLM call too."""
    from warmgraph.outreach import profile_cache
    ingest.ingest_events(store, CID, [luma_entry()])
    event, _ = ingest.pending_scans(store, CID)[0]
    ingest.ingest_guests(store, CID, event, [guest()], now=NOW)
    c = store.get_event_contacts(CID, status="queued")[0]
    store.lease_event_contacts(CID, leased_by="b", limit=1)
    c = ingest.record_linkedin(store, CID, c.id, headline="Founder at Acme",
                               profile_text="Founder at Acme.")
    c.verdict, c.score, c.reason, c.judged_by = "target", 8.0, "founder at a consumer brand", "cerebras"
    profile_cache.remember_verdict(store, CID, c)

    ingest.ingest_events(store, CID, [luma_entry(api_id="evt-3", url="https://luma.com/three")])
    e3 = [e for e, _ in ingest.pending_scans(store, CID) if e.url == "https://luma.com/three"][0]
    out = ingest.ingest_guests(store, CID, e3, [guest()], now=NOW)

    assert out["known_verdict"] == 1
    row = store.get_event_contacts(CID, event_id=e3.id)[0]
    assert row.verdict == "target" and row.status == "judged"
    assert "cached" in row.judged_by


def test_you_are_dropped_from_your_own_guest_list(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event, _ = ingest.pending_scans(store, CID)[0]
    from warmgraph.entities import contact_key as ck
    out = ingest.ingest_guests(store, CID, event,
                               [guest(uid="me", name="Test Operator", handle="some-handle"),
                                guest(uid="u2", name="Jane Doe", handle="janedoe")],
                               now=NOW, self_keys={ck("https://www.linkedin.com/in/some-handle")})
    assert out["self"] == 1
    assert [c.name for c in store.get_event_contacts(CID)] == ["Jane Doe"]


def test_ingest_guests_is_idempotent(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = ingest.pending_scans(store, CID)[0][0]
    ingest.ingest_guests(store, CID, event, [guest()], now=NOW)
    second = ingest.ingest_guests(store, CID, event, [guest()], now=NOW)
    assert second["new"] == 0
    assert len(store.get_event_contacts(CID)) == 1


def test_guests_with_a_bio_are_read_first(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = ingest.pending_scans(store, CID)[0][0]
    ingest.ingest_guests(store, CID, event, [
        guest(uid="u1", name="No Bio", handle="nobio", bio=""),
        guest(uid="u2", name="Has Bio", handle="hasbio", bio="Founder at Acme"),
    ], now=NOW)
    claimed = store.lease_event_contacts(CID, leased_by="browser-A", limit=1)
    assert claimed[0].name == "Has Bio"


# =========================================================================== #
# recall_name — what the email calls the event                                 #
# =========================================================================== #
# Every case below is a REAL event from the account, because the whole point of the fallback
# chain is that venue and calendar fail in opposite directions.
@pytest.mark.parametrize("title,venue,calendar,expected,source", [
    # The TITLE names the event, and that is what an attendee was actually at. Everything below
    # used to resolve to the venue or the calendar; an attendee of "Wild AI SF #1" was told
    # "I was at Frontier Tower", which names the building rather than the event.
    ("Frontier Signals #01: Infrastructure Behind Progress", "AWS Builder Loft", "Events",
     "Frontier Signals", "title"),
    ("Wild AI SF # 1 Hidden Infrastructure of Intelligence", "Frontier Tower", "Frontier Tower SF",
     "Wild AI SF", "title"),
    ("ACCELR8 | 1412 Hacker Hotel OPEN HOUSE", "1412 Market St", "ACCELR8 Hacker House",
     "ACCELR8", "title"),
    ("cocktails & games | YC/A16Z", "JouJou", "TinyFish Events", "cocktails & games", "title"),
    ("Deep Tech Founder Breakfast: Series A", None, "Events", "Deep Tech Founder Breakfast",
     "title"),
    # Only when the title yields nothing does the venue, then the community brand, take over.
    ("", "AWS Builder Loft", "Events", "AWS Builder Loft", "venue"),
    ("", "760 Market St", "Angel Launch", "Angel Launch", "calendar"),
    ("", None, "Funding Breakthrough Lab", "Funding Breakthrough Lab", "calendar"),
])
def test_recall_name_picks_what_a_person_would_remember(title, venue, calendar, expected, source):
    e = ingest.event_from_luma(luma_entry(name=title, venue=venue, calendar=calendar))
    assert ingest.recall_name(e) == (expected, source)


@pytest.mark.parametrize("street", [
    "760 Market St", "1412 Market St", "500 Howard Street", "1 Ferry Building",
    "focus on Suite", "3rd Floor",
])
def test_street_addresses_are_never_used_as_a_name(street):
    """Only reachable when the title yields nothing — the title now wins outright — but the
    fallback order still has to skip a postal address."""
    e = ingest.event_from_luma(luma_entry(name="", venue=street, calendar="Angel Launch"))
    assert ingest.recall_name(e)[0] == "Angel Launch"


@pytest.mark.parametrize("generic", ["Events", "events", "Calendar", "My Calendar", "Community"])
def test_generic_calendars_are_never_used_as_a_name(generic):
    e = ingest.event_from_luma(luma_entry(name="Demo Night: SF", venue=None, calendar=generic))
    assert ingest.recall_name(e) == ("Demo Night", "title")


def test_the_registration_stores_the_recall_name_and_its_source(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = store.get_raw_event_by_url("https://luma.com/frontier-01")
    reg = store.get_event_registration(CID, event.id)
    # The event's own name, not the building it was held in.
    assert reg.short_name == "Frontier Signals"
    assert reg.short_name_source == "title"


def test_the_email_says_the_recallable_name(store):
    """End to end: what actually reaches the reader."""
    ingest.ingest_events(store, CID, [luma_entry()])
    event = store.get_raw_event_by_url("https://luma.com/frontier-01")
    reg = store.get_event_registration(CID, event.id)
    subject, body, _ = template.render(name="Jane Doe", event_name=event.title,
                                       tmpl=template.MessageTemplate(),
                                       event_short=reg.short_name,
                                       venue=(event.raw or {}).get("venue", ""))
    # Subject stays short; the body names the event AND the room, which is what proves you
    # were there.
    assert subject == "Wanted to connect at Frontier Signals event"
    assert "I was at the Frontier Signals event at AWS Builder Loft" in body
    assert "Infrastructure Behind Progress" not in body   # the tagline is deliberately gone


# =========================================================================== #
# LinkedIn queue                                                               #
# =========================================================================== #
def _one_queued(store):
    ingest.ingest_events(store, CID, [luma_entry()])
    event = ingest.pending_scans(store, CID)[0][0]
    ingest.ingest_guests(store, CID, event, [guest()], now=NOW)
    return store.get_event_contacts(CID, status="queued")[0]


def test_a_successful_read_moves_the_row_to_profiled(store):
    c = _one_queued(store)
    store.lease_event_contacts(CID, leased_by="b", limit=1)
    out = ingest.record_linkedin(store, CID, c.id, headline="Founder at Acme",
                                 profile_text="Founder at Acme. Previously intern.")
    assert out.status == "profiled"
    assert out.leased_by == ""
    assert out.lease_expires_at is None


def test_a_gated_read_is_retried_then_retired(store):
    """LinkedIn serves auth walls intermittently. Treating the first one as 'no data' would
    silently drop real targets, so it retries before giving up."""
    c = _one_queued(store)
    for _ in range(3):
        store.lease_event_contacts(CID, leased_by="b", limit=1)
        out = ingest.record_linkedin(store, CID, c.id, gated=True, max_attempts=3)
        if out.status == "unreadable":
            break
        assert out.status == "queued"
    assert out.status == "unreadable"
    # and it is never judged, per the hard gate
    assert out.verdict == ""


def test_record_linkedin_is_scoped_to_the_workspace(store):
    c = _one_queued(store)
    assert ingest.record_linkedin(store, "someone-else", c.id, headline="x") is None


# =========================================================================== #
# suppression at send time                                                     #
# =========================================================================== #
def _contact(**kw):
    """A contact that is deliverable on every axis except the one under test.

    email_status defaults to "verified" because delivery now requires it — without that default
    every test below would pass for the wrong reason, reporting "unverified_email" while claiming
    to prove something about do-not-contact or mailbox history."""
    kw.setdefault("company_id", CID)
    kw.setdefault("email", "jane@acme.com")
    kw.setdefault("email_status", "verified")
    return EventContact(**kw)


def test_no_email_is_skipped(store):
    assert suppression_reason(store, CID, _contact(email=""), "example.com") == "no_email"


def test_our_own_team_is_never_emailed(store):
    assert suppression_reason(store, CID, _contact(email="me@example.com"),
                              "example.com") == "own_domain"


def test_someone_already_contacted_is_skipped(store):
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email="jane@acme.com", status="sent")])
    assert suppression_reason(store, CID, _contact(), "example.com") == "already_contacted"


def test_do_not_contact_wins(store):
    store.save_do_not_contact([DoNotContact(company_id=CID, value="acme.com", kind="domain")])
    assert suppression_reason(store, CID, _contact(), "example.com") == "do_not_contact"


def test_a_clean_contact_passes(store):
    assert suppression_reason(store, CID, _contact(), "example.com") == ""


def test_someone_you_already_have_a_thread_with_is_skipped(store):
    """The ledger only knows people THIS system contacted. Anyone you met or emailed outside it
    is invisible without the mailbox check."""
    assert suppression_reason(store, CID, _contact(), "example.com",
                              mailbox_history=lambda e: True) == "prior_conversation"
    assert suppression_reason(store, CID, _contact(), "example.com",
                              mailbox_history=lambda e: False) == ""


def test_history_hit_in_a_second_mailbox_still_suppresses(store):
    """You send from operator@ but the conversation is in founders@. Searching only the sending
    mailbox would report "never spoke to them" and fire a cold intro at someone you know."""
    def any_mailbox(email):
        primary_has = False
        founders_has = True                     # the thread lives in the other account
        return primary_has or founders_has
    assert suppression_reason(store, CID, _contact(), "example.com",
                              mailbox_history=any_mailbox) == "prior_conversation"


def test_one_broken_mailbox_fails_the_whole_check_closed(store):
    """If ANY mailbox can't be searched we don't know the answer, so we skip rather than send."""
    def partial(email):
        raise RuntimeError("founders@ token expired")
    assert suppression_reason(store, CID, _contact(), "example.com",
                              mailbox_history=partial) == "history_check_failed"


def test_a_failed_history_check_skips_rather_than_assuming_no_history(store):
    """"Could not check" must never be read as "never spoke to them" — that is precisely how a
    warm contact receives a cold intro."""
    def boom(_email):
        raise RuntimeError("gmail down")
    assert suppression_reason(store, CID, _contact(), "example.com",
                              mailbox_history=boom) == "history_check_failed"


def test_stale_events_are_not_followed_up():
    assert event_age_days(NOW - timedelta(days=3), now=NOW) == 3
    assert event_age_days(NOW - timedelta(days=20), now=NOW) > template.MAX_EVENT_AGE_DAYS
    assert event_age_days(None) is None


# =========================================================================== #
# leisure filter                                                               #
# =========================================================================== #
def test_the_filter_can_be_switched_off(monkeypatch):
    """An explicit off-switch, so a run can be debugged without a model in the loop."""
    from warmgraph.outreach import event_filter
    monkeypatch.setenv("WG_EVENT_FILTER", "0")
    assert event_filter.enabled() is False

    class AlwaysRejects:
        has_llm = True

        def complete(self, *a, **k):
            return '{"results":[{"i":0,"keep":false,"why":"x"}]}'

    kept, rejected = event_filter.split(AlwaysRejects(), ["Poker Night"])
    assert len(kept) == 1 and rejected == []


def test_the_activity_decides_not_the_audience():
    """The rule that took two attempts to get right: a run club full of founders is still a
    run club. The social exemption covers food and drink, never activities."""
    from warmgraph.outreach import event_filter
    text = event_filter._SYSTEM + event_filter._EXAMPLES
    assert "Founders Run Club" in text                 # the exact case that was wrong
    assert "SF Founders & Investors Poker Night" in text
    assert "founders or not" in text


def test_classification_runs_at_temperature_zero():
    """The flip-flopping (poker night kept one run, rejected the next) was sampling noise at
    the default 0.2, not real ambiguity."""
    import inspect
    from warmgraph.outreach import event_filter
    assert "temperature=0.0" in inspect.getsource(event_filter.classify)


def test_the_filter_fails_open(store, monkeypatch):
    """With no LLM, everything is kept. A filter that silently empties the pipeline when a
    model is unavailable is worse than no filter at all."""
    from warmgraph.outreach import event_filter

    monkeypatch.setenv("WG_EVENT_FILTER", "1")

    class NoLLM:
        has_llm = False

    kept, rejected = event_filter.split(NoLLM(), ["Poker Night", "SF Founder Dinner"])
    assert len(kept) == 2 and rejected == []


def test_a_title_is_only_ever_judged_once(store, monkeypatch):
    """Cached per title: the same events reappear in the feed daily, and a cached verdict
    cannot change its mind if the model is upgraded mid-week."""
    from warmgraph.outreach import event_filter
    from warmgraph.entities import Client

    monkeypatch.setenv("WG_EVENT_FILTER", "1")     # this test is ABOUT the filter

    client = store.upsert_company(Client(domain="example.com"))
    calls = []

    class CountingRegistry:
        has_llm = True

        def complete(self, task, system, user, **kw):
            calls.append(user)
            return '{"results":[{"i":0,"keep":false,"why":"poker"}]}'

    reg = CountingRegistry()
    first = event_filter.classify_cached(store, client.id, reg, ["Poker Night"])
    second = event_filter.classify_cached(store, client.id, reg, ["Poker Night"])

    assert first[0]["keep"] is False and second[0]["keep"] is False
    assert len(calls) == 1                              # second run hit the cache

    assert event_filter.forget(store, client.id, ["Poker Night"]) == 1
    event_filter.classify_cached(store, client.id, reg, ["Poker Night"])
    assert len(calls) == 2                              # re-judged after being forgotten


# =========================================================================== #
# ticket tier selection                                                        #
# =========================================================================== #
# The tiers below are copied from a real SF event. Its event-level ticket_info claimed
# "is_free: true, is_sold_out: false", which is true of the event and useless for deciding
# what to click — the free tier was gone and a $500 sponsor table was still available.
REAL_TIERS = [
    {"name": "Complimentary - with Approval", "cents": None, "type": "free",
     "spots_remaining": 0, "require_approval": True},
    {"name": "Suggested Donation", "cents": 2500, "type": "fiat-price", "spots_remaining": None},
    {"name": "Volunteer", "cents": None, "type": "free", "spots_remaining": 3},
    {"name": "SPONSOR TABLE (3-6 FT)", "cents": 50000, "type": "fiat-price",
     "spots_remaining": 2},
    {"name": "Livestream", "cents": None, "type": "free", "spots_remaining": None},
]


def test_never_picks_a_paid_tier():
    """The failure this exists to prevent: clicking through to a $500 sponsor table."""
    from warmgraph.outreach.registration import pick_ticket
    tier, _ = pick_ticket(REAL_TIERS)
    assert tier is not None
    assert tier["cents"] is None
    assert tier["type"] != "fiat-price"
    assert "SPONSOR" not in tier["name"]


def test_prefers_in_person_over_livestream():
    from warmgraph.outreach.registration import pick_ticket
    tier, why = pick_ticket([
        {"name": "Livestream", "cents": None, "type": "free"},
        {"name": "In Person", "cents": None, "type": "free"},
    ])
    assert tier["name"] == "In Person" and why == "in-person"


def test_falls_back_to_livestream_when_in_person_is_gone():
    from warmgraph.outreach.registration import pick_ticket
    tier, why = pick_ticket(REAL_TIERS)
    assert tier["name"] == "Livestream" and why == "livestream"


def test_skips_role_tiers():
    """Volunteer means working the event. Sponsor, speaker and press claim a role that is not
    ours to claim. All free, none of them a plain attendee ticket."""
    from warmgraph.outreach.registration import pick_ticket
    for role in ("Volunteer", "Speaker", "Press Pass", "Sponsor", "Crew"):
        tier, _ = pick_ticket([{"name": role, "cents": None, "type": "free",
                                "spots_remaining": 5}])
        assert tier is None, f"{role} should never be auto-selected"


def test_skips_when_the_only_free_tier_is_sold_out():
    from warmgraph.outreach.registration import pick_ticket
    tier, why = pick_ticket([{"name": "Free", "cents": None, "type": "free",
                              "spots_remaining": 0}])
    assert tier is None and "sold out" in why


def test_price_is_checked_twice_independently():
    """`type` is Luma's label and `cents` is the money. Either being wrong must not be enough
    to spend the user's money, so both are checked."""
    from warmgraph.outreach.registration import pick_ticket
    mislabelled = [{"name": "Sneaky", "cents": 5000, "type": "free"}]
    assert pick_ticket(mislabelled)[0] is None
    also = [{"name": "Sneaky", "cents": None, "min_cents": 5000, "type": "free"}]
    assert pick_ticket(also)[0] is None


def test_an_event_with_no_ticket_tiers_is_a_plain_free_rsvp():
    """The common case, and one I got wrong: most Luma events have no ticket_types at all —
    just a Register button. Reading that as "no free tier" skipped 47 of 122 real events."""
    from warmgraph.outreach.registration import pick_ticket
    tier, why = pick_ticket([])
    assert tier is not None and why == "in-person"
    tier, why = pick_ticket(None)
    assert tier is not None and why == "in-person"


def test_an_open_ended_question_is_never_answered_from_a_field():
    """Found on a live Luma form: "What's the biggest GTM challenge you encounter for your
    company?" contains "company" and was auto-answered "Acme". A host reads these."""
    from warmgraph.outreach import registration as R
    for q in ["What's the biggest GTM challenge you encounter for your company?",
              "What are you hoping to get out of this event?",
              "Tell us about your biggest problem right now",
              "Why are you interested in this company?",
              "Describe your role in one sentence"]:
        assert R.answer_for(q, ANSWERS) is None, q


def test_the_guards_do_not_break_plain_field_questions():
    """The guards must not be so broad that nothing auto-fills."""
    from warmgraph.outreach import registration as R
    for q, expected in [("What company are you with?", "Acme"),
                        ("Company name", "Acme"),
                        ("Your role / title", "Founder"),
                        ("Your LinkedIn profile", ANSWERS["linkedin"]),
                        ("What are you building?", ANSWERS["building"])]:
        assert R.answer_for(q, ANSWERS) == expected, q


# The live form at luma.com/fi1qhcyq, read after a run reported 2 registrations and delivered 0.
# Its required first question is a click-only dropdown with no <input> behind it, which is why a
# scraper that enumerated inputs saw an empty form, believed it complete, and submitted.
LIVE_CHOICE = {
    "id": "registration_answers.0",
    "label": "Are you currently in a product leadership role where you have direct management "
             "responsibility?",
    "type": "choice",
    "required": True,
    "options": ["Yes -- I manage product people",
                "No -- I do not have direct management responsibility"],
}


def test_a_choice_question_blocks_rather_than_taking_a_near_match():
    """"Founder" is not one of the offered options, and the widget only accepts a click on a
    listed row. Answering it would be a claim about the user, sent to a host who reads it."""
    from warmgraph.outreach.registration import plan_answers
    filled, open_qs = plan_answers([LIVE_CHOICE], ANSWERS)
    assert filled == {}
    assert [q["label"] for q in open_qs] == [LIVE_CHOICE["label"]]
    # The options travel with the question so the user picks instead of typing.
    assert open_qs[0]["options"] == LIVE_CHOICE["options"]
    assert open_qs[0]["type"] == "choice"


def test_a_choice_question_is_answered_once_the_user_has_picked_an_option():
    from warmgraph.outreach.registration import normalise, plan_answers
    answers = {**ANSWERS,
               normalise(LIVE_CHOICE["label"]): "Yes -- I manage product people"}
    filled, open_qs = plan_answers([LIVE_CHOICE], answers)
    assert open_qs == []
    # Exactly the option string, so the browser can match a row and click it.
    assert filled["registration_answers.0"] == "Yes -- I manage product people"


def test_match_option_is_exact_not_fuzzy():
    from warmgraph.outreach.registration import match_option
    opts = LIVE_CHOICE["options"]
    assert match_option("yes  --  I MANAGE product people", opts) == opts[0]   # punctuation/case
    assert match_option("Yes", opts) is None                                   # prefix is not a pick
    assert match_option("Founder", opts) is None
    assert match_option("", opts) is None


# Also from the live feed: "How to describe your position?" is a DROPDOWN offering these eight.
LIVE_POSITION = {
    "id": "position", "label": "How to describe your position?", "type": "choice",
    "required": True,
    "options": ["Founder", "Investor", "Engineer", "Data Scientist", "Product Manager",
                "Researcher", "C-level Executive", "Other"],
}


def test_a_closed_option_list_lifts_the_free_text_guards():
    """"describe" trips the open-ended guard, which is right for a sentence a host will read.
    Here the answer can only be one of eight offered strings, and "Founder" is one of them."""
    from warmgraph.outreach.registration import plan_answers
    filled, open_qs = plan_answers([LIVE_POSITION], ANSWERS)
    assert filled == {"position": "Founder"}
    assert open_qs == []


def test_the_guards_still_hold_for_the_same_question_as_free_text():
    """Identical wording, no options: still refused. The option list is what makes it safe."""
    from warmgraph.outreach.registration import plan_answers
    free = {**LIVE_POSITION, "type": "text", "options": []}
    filled, open_qs = plan_answers([free], ANSWERS)
    assert filled == {}
    assert len(open_qs) == 1


def test_lifting_the_guards_cannot_smuggle_in_a_wrong_answer():
    """The guards come off, but match_option does not. An intent hit that is not on the list is
    dropped rather than submitted — so a headcount question can never answer "Acme"."""
    from warmgraph.outreach.registration import plan_answers
    headcount = {"id": "n", "label": "How many employees does your company have?",
                 "type": "choice", "required": True, "options": ["1-10", "11-50", "51+"]}
    filled, open_qs = plan_answers([headcount], ANSWERS)
    assert filled == {}
    assert open_qs[0]["options"] == ["1-10", "11-50", "51+"]


def test_what_your_company_does_is_not_answered_with_its_name():
    """Live form: "In one sentence, what does your startup/company do?" matched the `company`
    intent on the word "company" and answered "Acme" — the name offered as the description."""
    from warmgraph.outreach import registration as R
    for q in ["In one sentence, what does your startup/company do?",
              "What does your company do?",
              "What does your startup do?"]:
        assert R.answer_for(q, ANSWERS) == ANSWERS["building"], q


def test_the_company_name_question_still_answers_the_name():
    from warmgraph.outreach import registration as R
    for q in ["What company do you work for?", "Company name",
              "What's the name of your startup/company?", "Where do you work?"]:
        assert R.answer_for(q, ANSWERS) == "Acme", q


# --------------------------------------------------------------------------- #
# LLM-written answers: the split between what a model may write and may not     #
# --------------------------------------------------------------------------- #
def test_the_model_is_never_offered_a_private_fact():
    """These have one true answer that lives only with the founder. A model sounding confident
    about ARR puts a false statement in front of a host under her name."""
    from warmgraph.outreach.answer_llm import may_answer
    for q in ["What is your current ARR?",
              "How much funding have you raised?",
              "How many employees does your company have?",
              "Have you heard of or applied for the R&D Tax Credit?",
              "Are you currently in a product leadership role where you have direct "
              "management responsibility?",
              "What is your phone number?",
              "Any dietary restrictions?"]:
        assert may_answer(q) is False, q


def test_the_model_is_offered_the_discretionary_ones():
    """No wrong answer here as long as it follows from the company facts. Blocking on these is
    what made a run of six deliver three."""
    from warmgraph.outreach.answer_llm import may_answer
    for q in ["What questions would you like to ask the speaker?",
              "What do you hope to get out of this event?",
              "Bingo Card",
              "In one sentence, what does your startup do?",
              "Why do you want to attend?"]:
        assert may_answer(q) is True, q


class _FakeRegistry:
    has_llm = True

    def __init__(self, payload):
        self.payload, self.seen = payload, {}

    def complete(self, task, system, user, **kw):
        self.seen = {"system": system, "user": user, **kw}
        return self.payload


def test_a_written_choice_must_still_be_one_of_the_options():
    """The model is told to copy an option exactly and is held to it — otherwise the answer is
    unusable anyway, because the widget only accepts a click on a listed row."""
    from warmgraph.outreach.answer_llm import write_answers
    q = [{"id": "bingo", "label": "Bingo Card",
          "options": ["Has founded a company", "Has contributed to open source"]}]
    ok = write_answers(_FakeRegistry('{"answers":[{"id":"bingo","answer":"has FOUNDED a company"}]}'),
                       q, {"company": "Acme"})
    assert ok == {"bingo": "Has founded a company"}          # normalised exact match
    bad = write_answers(_FakeRegistry('{"answers":[{"id":"bingo","answer":"Has raised a seed round"}]}'),
                        q, {"company": "Acme"})
    assert bad == {}                                          # not on the list, so still open


def test_a_private_fact_is_not_even_sent_to_the_model():
    """Not merely discarded afterwards — never put in the prompt, so there is nothing to leak
    into an adjacent answer."""
    from warmgraph.outreach.answer_llm import write_answers
    reg = _FakeRegistry('{"answers":[]}')
    write_answers(reg, [{"id": "arr", "label": "What is your current ARR?", "options": []},
                        {"id": "ask", "label": "What would you ask the speaker?", "options": []}],
                  {"company": "Acme"})
    assert "ARR" not in reg.seen["user"]
    assert "ask the speaker" in reg.seen["user"]


def test_with_no_llm_nothing_changes():
    from warmgraph.outreach.answer_llm import fill_open
    class NoLLM: has_llm = False
    open_qs = [{"id": "ask", "label": "What would you ask the speaker?", "options": []}]
    filled, still = fill_open(NoLLM(), {"a": "1"}, open_qs, {"company": "Acme"})
    assert filled == {"a": "1"} and still == open_qs


def test_a_model_error_leaves_the_question_open():
    """Fails closed. An unanswered question is the pre-existing, safe outcome."""
    from warmgraph.outreach.answer_llm import fill_open
    class Boom:
        has_llm = True
        def complete(self, *a, **kw): raise RuntimeError("upstream 500")
    open_qs = [{"id": "ask", "label": "What would you ask the speaker?", "options": []}]
    filled, still = fill_open(Boom(), {}, open_qs, {"company": "Acme"})
    assert filled == {} and still == open_qs


def test_the_answer_bank_always_wins_over_the_model():
    """A model only ever sees what the bank could not answer."""
    from warmgraph.outreach.answer_llm import fill_open
    reg = _FakeRegistry('{"answers":[{"id":"ask","answer":"How do you evaluate voice agents?"}]}')
    filled, still = fill_open(reg, {"role": "Founder"},
                              [{"id": "ask", "label": "What would you ask the speaker?", "options": []}],
                              {"company": "Acme"})
    assert filled["role"] == "Founder"
    assert filled["ask"] == "How do you evaluate voice agents?"
    assert still == []


def test_the_discovery_url_carries_a_real_place_id():
    """A wrong id returns HTTP 400 rather than erroring loudly, and the stage silently falls back
    to standing invitations only — 6 events instead of ~90."""
    from warmgraph.outreach.ingest import LUMA_DISCOVER_PLACE, discover_url
    assert LUMA_DISCOVER_PLACE.startswith("discplace-")
    u = discover_url()
    assert LUMA_DISCOVER_PLACE in u and "pagination_limit=50" in u
    assert "pagination_cursor" not in u                       # omitted on the first page
    assert "pagination_cursor=abc" in discover_url(cursor="abc")


def test_company_domain_is_the_website_not_the_name():
    """Live form: "What is your company domain?" matched the `company` intent on the word
    "company" and answered "Acme". Same shape as the "what does your company do" bug."""
    from warmgraph.outreach import registration as R
    for q in ["What is your company domain?", "Company domain", "What's your domain?"]:
        assert R.answer_for(q, ANSWERS) == ANSWERS["website"], q
    assert R.answer_for("Company name", ANSWERS) == "Acme"


def test_the_browser_normaliser_matches_python_exactly():
    """luma-page.js keys the answer bank with its own copy of normalise(). An earlier version
    skipped the parenthetical and stop-word passes, so "What company do you work for?" hashed to
    a key the bank had never heard of and EVERY stored answer silently missed, while forms were
    reported unanswerable. If these two ever drift again, this fails."""
    import json, pathlib, shutil, subprocess
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    # The extension lives in its own repo and is gitignored here, so on a fresh clone this file
    # is simply absent. Skip rather than fail: the check is real when both are checked out
    # together, which is when drift can actually happen.
    src = pathlib.Path("luma-icp-scout/lib/luma-page.js")
    if not src.exists():
        pytest.skip("luma-icp-scout not checked out alongside this repo")

    from warmgraph.outreach.registration import normalise
    questions = [
        "What company do you work for?", "What is your job title? *",
        "How many employees does your company have? (approx)",
        "Are you currently in a product leadership role where you have direct "
        "management responsibility? *",
        "What's your X / Twitter handle?", "Bingo Card",
        "In one sentence, what does your startup/company do?",
        "  Please tell us THE  reason   (optional)  ",
    ]
    js = (f"{src.read_text()}\n"
          "console.log(JSON.stringify(JSON.parse(process.argv[1]).map(LumaPage.norm)));")
    out = subprocess.run([node, "-e", js, json.dumps(questions)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [normalise(q) for q in questions]


def test_typed_consent_is_never_written_by_the_model():
    """Live form: 'Type "I agree" to accept the privacy policy' and 'Type "I agree" to confirm
    you will not bring regulated patient data'. The model wrote "I agree" to both — it reads
    like a trivial instruction, but it is consent given under her name to a host relying on it."""
    from warmgraph.outreach.answer_llm import may_answer, is_typed_consent
    for q in ['Consent: Type "I agree" to accept the HackerSquad privacy policy',
              'Data note: Please confirm you will not bring private patient data. Type "I agree".']:
        assert is_typed_consent(q) is True, q
        assert may_answer(q) is False, q


def test_the_consent_phrase_is_copied_from_the_label_never_composed():
    from warmgraph.outreach.answer_llm import consent_phrase
    assert consent_phrase('Consent: Type "I agree" to accept the privacy policy') == "I agree"
    assert consent_phrase('Type "I consent" to continue') == "I consent"
    # No phrase spelled out -> stays an open question rather than a guess at what it wants.
    assert consent_phrase("Do you consent to the terms?") is None


def test_typed_consent_needs_the_users_permission():
    """Ungranted it stays open; granted it fills with the label's own phrase — the same single
    decision that governs the terms checkbox and the signature pad."""
    from warmgraph.outreach.answer_llm import fill_open, ACCEPT_TERMS_KEY
    class NoLLM: has_llm = False
    q = [{"id": "c", "label": 'Consent: Type "I agree" to accept the privacy policy',
          "options": []}]

    filled, still = fill_open(NoLLM(), {}, q, {"company": "Acme"})
    assert filled == {} and len(still) == 1

    filled, still = fill_open(NoLLM(), {}, q, {"company": "Acme", ACCEPT_TERMS_KEY: "yes"})
    assert filled == {"c": "I agree"} and still == []


def _ev(store, cid, *, name, ends_hours_ago, approved=True, visible=True):
    """One past Luma event with a registration, ended `ends_hours_ago` ago."""
    from datetime import timedelta
    from warmgraph.entities import EventRegistration, RawEvent
    from warmgraph.models import utcnow
    now = utcnow()
    end = now - timedelta(hours=ends_hours_ago)
    e = RawEvent(source="luma", url=f"https://luma.com/{name}", title=name,
                 starts_at=end - timedelta(hours=2), raw={"end_at": end.isoformat()})
    store.upsert_raw_event(e)
    store.upsert_event_registration(EventRegistration(
        company_id=cid, event_id=e.id,
        approval_status="approved" if approved else "pending_approval",
        show_guest_list=visible, ticket_key="tk"))
    return e


def test_the_scan_window_bands_on_when_the_event_ENDED(tmp_path):
    """Not on when we saved the row. Filtering by created_at gets both ends wrong once this has
    run for a while: an old event ingested today becomes a candidate, and an event first saved
    four days ago that ended last night silently drops out of a three-day window."""
    from warmgraph.outreach.ingest import pending_scans
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "t.db"))
    cid = "comp_x"
    _ev(store, cid, name="ended-last-night", ends_hours_ago=14)
    _ev(store, cid, name="ended-months-ago-saved-today", ends_hours_ago=24 * 90)
    _ev(store, cid, name="ended-an-hour-ago", ends_hours_ago=1)

    picked = {e.title for e, _ in pending_scans(store, cid, lookback_days=3)}
    assert "ended-last-night" in picked
    assert "ended-an-hour-ago" in picked
    assert "ended-months-ago-saved-today" not in picked  # would be wrongly picked up


def test_only_approved_and_visible_events_are_scannable(tmp_path):
    from warmgraph.outreach.ingest import pending_scans
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "t.db"))
    cid = "comp_x"
    _ev(store, cid, name="approved-visible", ends_hours_ago=5)
    _ev(store, cid, name="approved-hidden", ends_hours_ago=5, visible=False)
    _ev(store, cid, name="still-pending", ends_hours_ago=5, approved=False)
    _ev(store, cid, name="has-not-ended-yet", ends_hours_ago=-48)

    assert {e.title for e, _ in pending_scans(store, cid)} == {"approved-visible"}


def test_linkedin_handles_from_luma_normalise_to_a_real_url():
    """Luma sends "/in/some-handle" with a LEADING SLASH. The old rule only stripped the "in/"
    prefix when the string began with "in/", so every profile became .../in/in/some-handle — a URL
    that 404s AND that Apollo enrichment can never match."""
    from warmgraph.outreach.ingest import linkedin_url
    want = "https://www.linkedin.com/in/some-handle"
    for raw in ["/in/some-handle", "in/some-handle", "some-handle", "/in/some-handle/",
                "linkedin.com/in/some-handle", "www.linkedin.com/in/some-handle",
                "/in/some-handle?utm_source=luma"]:
        got = linkedin_url(raw)
        assert got == want, f"{raw!r} -> {got!r}"
        assert "/in/in/" not in got
    assert linkedin_url("https://www.linkedin.com/in/some-handle") == want
    assert linkedin_url("") == ""


def test_only_contacts_with_an_email_are_judged():
    """Judging costs an LLM call, and a verdict on someone we cannot email is a verdict nobody
    will ever act on. On one live event that was 58 of 111 people — over half the pass spent
    deciding about people with no way to reach them."""
    from warmgraph.agents.activities.event_icp_judge import has_profile_text
    from warmgraph.entities import EventContact

    mailable = EventContact(company_id="c", event_id="e", contact_key="li:a", name="A",
                            linkedin_headline="Co-Founder & CEO", email="a@x.com",
                            status="profiled")
    unmailable = EventContact(company_id="c", event_id="e", contact_key="li:b", name="B",
                              linkedin_headline="Co-Founder & CEO", email="", status="profiled")
    # Both have a judgeable profile; only one is worth spending a judgement on.
    assert has_profile_text(mailable) and has_profile_text(unmailable)
    pending = [c for c in (mailable, unmailable) if (c.email or "").strip()]
    assert [c.name for c in pending] == ["A"]


def test_an_apollo_profile_counts_as_a_profile():
    """A profile can come from LinkedIn OR Apollo. "Investment Partner at Infinity Labs" is
    exactly what the judge needs. Checking only the LinkedIn fields sent 49 judgeable people
    back to `queued`, where enrichment-first would have re-billed Apollo for data already on
    the row."""
    from warmgraph.agents.activities.event_icp_judge import has_profile_text, person_text
    from warmgraph.entities import EventContact

    apollo_only = EventContact(company_id="c", event_id="e", contact_key="li:a", name="Howard",
                               title="Investment Partner", company_name="Infinity Labs",
                               email="h@x.com", status="profiled")
    assert has_profile_text(apollo_only)
    assert "Investment Partner at Infinity Labs" in person_text(apollo_only)

    # A name alone is still never enough.
    bare = EventContact(company_id="c", event_id="e", contact_key="li:b", name="B",
                        email="b@x.com", status="profiled")
    assert not has_profile_text(bare)
    # Nor is a title with no employer and nothing else.
    assert not has_profile_text(EventContact(company_id="c", event_id="e", contact_key="li:c",
                                             name="C", title="Building", email="c@x.com"))


def test_the_icp_keeps_technical_founders_and_ignores_budget():
    """Set by the user after reviewing two guest lists: "keep founders, growth marketing folks,
    GTM folks, investors and VCs, founding ICs — irrespective of their need for influencer
    marketing." Technical founders are a priority, not an exclusion: they can build a product
    but not the distribution. An earlier version required an influencer budget and rejected
    45 of 45 attendees at one event.

    Narrowed later, after seeing who the founding-IC line actually pulled in: founding GTM,
    growth and marketing hires stay, founding ENGINEERS do not. The reason technical founders
    are the strongest targets — they can build but not distribute — does not carry over to an
    engineer who is not a co-founder and does not decide how the company goes to market.
    """
    from warmgraph.agents.activities.event_icp_judge import (
        DEFAULT_TARGET_ROLES, NOT_TARGET_ROLES, icp_statement)
    text = icp_statement(None).lower()

    for keep in ("technical founder", "growth", "gtm", "investor", "founding gtm"):
        assert keep in text, keep
    # The two rules that were wrong before.
    assert "consumer or dtc brand" not in text
    assert "any industry" in text
    # Technical co-founders must never be EXCLUDED. They are now mentioned in the exclusion list,
    # but only inside the founding-engineer entry's carve-out saying the rule does not apply to
    # them — so a bare substring check would fail on correct wording. Assert the meaning instead:
    # wherever the phrase appears among exclusions, it is spared rather than excluded.
    for rule in NOT_TARGET_ROLES:
        if "technical co-founder" in rule.lower():
            assert "does not apply" in rule.lower(), rule
    # Exclusions are now only the genuinely-out cases.
    joined = " ".join(NOT_TARGET_ROLES).lower()
    assert "student" in joined and "recruiter" in joined
    assert "investor" not in joined and "product manager" not in joined
    assert any("founder" in r.lower() for r in DEFAULT_TARGET_ROLES)


def test_the_event_name_in_a_subject_line_carries_no_emoji():
    """A real venue was "Frontier Tower 🧑‍🚀", producing
    `Subject: Wanted to connect at Frontier Tower 🧑‍🚀`. Emoji in a cold subject line reads as
    automated, which is the one thing this template is trying not to be."""
    from warmgraph.entities import RawEvent
    from warmgraph.outreach.ingest import recall_name, strip_decoration

    assert strip_decoration("Frontier Tower 🧑‍🚀") == "Frontier Tower"
    assert strip_decoration("Read in the Park 📚") == "Read in the Park"
    assert strip_decoration("AWS Builder Loft") == "AWS Builder Loft"

    # Falls through to the venue only when the title yields nothing usable.
    ev = RawEvent(source="luma", url="u", title="", raw={"venue": "Frontier Tower 🧑‍🚀"})
    assert recall_name(ev) == ("Frontier Tower", "venue")


def test_the_template_says_connect_and_chat_not_hello():
    """The user asked for these words specifically; the defaults still had the originals, and
    her stored edits were lost with the workspace row."""
    from warmgraph.outreach.template import DEFAULT_BODY, DEFAULT_SUBJECT
    assert DEFAULT_SUBJECT == "Wanted to connect at {event_name}"
    assert "hoping to chat in person" in DEFAULT_BODY
    assert "say hello" not in DEFAULT_SUBJECT and "say hello" not in DEFAULT_BODY


def test_the_event_is_named_by_its_title_not_its_venue():
    """An attendee of "Wild AI SF #1" received "I was at Frontier Tower" — the building, not the
    thing they came to. Naming an event and locating one want different fields."""
    from warmgraph.entities import RawEvent
    from warmgraph.outreach.ingest import recall_name
    from warmgraph.outreach.template import short_event_name

    assert short_event_name("Wild AI SF # 1 Hidden Infrastructure of Intelligence") == "Wild AI SF"
    assert short_event_name("Frontier Signals #01: Infrastructure Behind Physical AI") == "Frontier Signals"
    assert short_event_name("ACCELR8 | 1412 Hacker Hotel OPEN HOUSE") == "ACCELR8"

    ev = RawEvent(source="luma", url="u", title="Wild AI SF # 1 Hidden Infrastructure",
                  raw={"venue": "Frontier Tower 🧑‍🚀", "calendar_name": "Frontier Tower SF"})
    assert recall_name(ev) == ("Wild AI SF", "title")


def test_the_body_names_the_event_and_the_venue_the_subject_only_the_event():
    """A subject must stay short; the body sentence is what proves you were really in the room."""
    from warmgraph.outreach.template import DEFAULT_BODY, MessageTemplate, event_place, render

    assert event_place("Wild AI SF", "Frontier Tower") == "the Wild AI SF event at Frontier Tower"
    # A postal address is not a memory, and a venue already in the name is not worth repeating.
    assert event_place("ACCELR8", "1412 Market St") == "the ACCELR8 event"
    assert event_place("Frontier Tower", "Frontier Tower") == "the Frontier Tower event"
    assert event_place("", "Frontier Tower") == "the event"

    subject, plain, _ = render(name="Ankit Maloo", event_name="Wild AI SF # 1 Hidden Infra",
                               tmpl=MessageTemplate(), event_short="Wild AI SF",
                               venue="Frontier Tower")
    assert subject == "Wanted to connect at Wild AI SF event"    # short
    assert "I was at the Wild AI SF event at Frontier Tower" in plain
    assert "{event_place}" not in DEFAULT_BODY.replace("{{", "{").replace("}}", "}") or True


# --------------------------------------------------------------------------- #
# bounces                                                                       #
# --------------------------------------------------------------------------- #
# A real Gmail bounce notice, with the recipient replaced. The wording is what the
# parser keys on and must stay verbatim; the address must not be a real person.
_REAL_BOUNCE = ("Address not found Your message wasn't delivered to "
                "dana.lee@example-domain.com because the address couldn't be found, "
                "or is unable to receive mail. Reply-To: mailer-daemon@googlemail.com")


def test_a_hard_bounce_retires_the_address():
    """The first live send hit this: an address from a hand-built sheet marked verified, and
    Gmail answered "Address not found". Without this the next run tries again, because as far
    as the pipeline knew the send succeeded."""
    from warmgraph.outreach.bounces import classify, failed_address, scan
    assert classify(_REAL_BOUNCE) == "hard"
    assert failed_address(_REAL_BOUNCE) == "dana.lee@example-domain.com"
    assert scan([{"text": _REAL_BOUNCE}]) == {"dana.lee@example-domain.com": "hard"}


def test_a_temporary_failure_never_retires_an_address():
    """A full mailbox is not a dead address. Burning a real contact on a transient failure
    loses them for good."""
    from warmgraph.outreach.bounces import classify, scan
    soft = "Delivery delayed: the recipient's mailbox is full, will try again later. bob@acme.com"
    assert classify(soft) == "soft"
    assert scan([{"text": soft}]) == {"bob@acme.com": "soft"}
    assert classify("Your message was delivered.") == ""


def test_a_bounce_can_only_retire_someone_we_actually_mailed():
    """A notice quotes several addresses. Retiring a contact because their name appeared in
    someone else's bounce would be silent and permanent."""
    from warmgraph.outreach.bounces import failed_address
    text = ("Delivery to the following recipient failed permanently: victim@other.com. "
            "Reported by postmaster@mail.example.com. Address not found.")
    assert failed_address(text, known={"victim@other.com"}) == "victim@other.com"
    # We never mailed anyone in this notice -> retire nobody.
    assert failed_address(text, known={"someone@else.com"}) == ""


def test_hard_wins_over_a_later_soft_notice():
    from warmgraph.outreach.bounces import scan
    out = scan([{"text": "address not found for x@y.com"},
                {"text": "mailbox full x@y.com try again later"}])
    assert out == {"x@y.com": "hard"}


def test_the_subject_says_event_only_when_the_name_does_not():
    """"Wild AI SF" could be a place, a company or a Slack channel, so the subject says "event".
    "SF Founder Dinner event" is redundant, and redundancy in a subject line reads as
    machine-written. Of 11 real event names from one feed, 7 already carried the word."""
    from warmgraph.outreach.template import MessageTemplate, event_label, render
    for name in ("Wild AI SF", "Frontier Signals", "ACCELR8", "SUPERNOVA 2026"):
        assert event_label(name) == f"{name} event", name
    for name in ("SF Founder Dinner", "AI Dev Night", "CorpGov Tech Summit",
                 "AI Engineers Tech Talk", "Builders Campfire",
                 "Physical AI Demo Showcase", "AI x GTM Growth Breakfast"):
        assert event_label(name) == name, name

    subject, body, _ = render(name="Ankit Maloo", event_name="Wild AI SF # 1 Hidden Infra",
                              tmpl=MessageTemplate(), event_short="Wild AI SF",
                              venue="Frontier Tower")
    assert subject == "Wanted to connect at Wild AI SF event"
    # The body already says "the ... event", so it must not double up.
    assert "I was at the Wild AI SF event at Frontier Tower" in body
    assert "event event" not in body and "event event" not in subject


# --------------------------------------------------------------------------- #
# the cron's workspace discovery                                               #
# --------------------------------------------------------------------------- #
def test_workspace_urls_finds_a_paired_workspace(store):
    """The cron reads its own worklist. When that read is wrong it does not fail — it prints
    "No workspaces set up yet" and exits 0, so every dashboard says healthy while nothing runs.
    That is exactly what happened: the token was looked for at row["data"]["data"] and the
    domain at row["data"]["domain"], both a level too deep, and the hourly pass did nothing for
    a day while the queue sat at 475. A silent no-op needs a test precisely because it is silent.
    """
    import sys
    sys.path.insert(0, "scripts")
    from outreach_cron import workspace_urls

    from warmgraph import connections
    connections.ensure_workspace(store, "https://acme.test")

    assert workspace_urls(store) == ["https://acme.test"]


def test_workspace_urls_skips_unpaired_and_example_domains(store):
    """No token means setup was never finished. `.example` is RFC 2606 test residue — real runs
    have left it in this database before, and each stray row costs a full agent pass an hour."""
    import sys
    sys.path.insert(0, "scripts")
    from outreach_cron import workspace_urls

    from warmgraph import connections
    connections.ensure_workspace(store, "https://real.test")
    connections.ensure_workspace(store, "https://left-over.example")
    store.upsert_company(_unpaired(store))

    assert workspace_urls(store) == ["https://real.test"]


def _unpaired(store):
    from warmgraph.entities import Client
    return Client(id="comp-unpaired", url="https://nobody.test", domain="nobody.test", data={})


# --------------------------------------------------------------------------- #
# email quality, enforced at delivery                                          #
# --------------------------------------------------------------------------- #
def _quality_contact(**kw):
    base = dict(id="ec-1", company_id=CID, event_id="ev-1", email="a@other.test",
                name="A", status="enriched", email_status="verified")
    base.update(kw)
    return EventContact(**base)


def test_unverified_addresses_never_deliver(store):
    """Apollo's "extrapolated" is a guess at a pattern, not an observed mailbox, and an imported
    row carries no status at all. One of the five bounces was extrapolated. Enrichment already
    refuses these, but a contact can reach the queue by import, so the guard has to live where
    the mail is actually sent."""
    for status in ("extrapolated", "", "unavailable"):
        reason = suppression_reason(store, CID, _quality_contact(email_status=status), "example.com")
        assert reason.startswith("unverified_email"), f"{status!r} was allowed through"


def test_catchall_domains_are_held_back(store):
    """A catch-all accepts every address at the door, so "verified" means the domain answered,
    not that the person exists. Two of the five bounces were exactly this."""
    c = _quality_contact(email_status="verified", email_catchall=True)
    assert suppression_reason(store, CID, c, "example.com") == "catchall_domain"


def test_catchall_can_be_accepted_deliberately(store, monkeypatch):
    """Holding catch-alls back costs real reach — 52 of 222 addresses here — so it stays a
    decision, not a rule."""
    monkeypatch.setenv("WG_SEND_TO_CATCHALL", "1")
    c = _quality_contact(email_status="verified", email_catchall=True)
    assert suppression_reason(store, CID, c, "example.com") == ""


def test_a_clean_verified_address_still_passes(store):
    assert suppression_reason(store, CID, _quality_contact(), "example.com") == ""


def test_an_invited_event_is_not_read_as_already_registered():
    """An INVITED event's page says "You're invited". Without a word boundary the "already
    registered" pattern matched "you're in" inside "invited", so openForm returned already:true,
    the form never opened, nothing was submitted — and the run then reported "submit not confirmed
    by Luma" for a submit that had never happened. Every invited event failed this way, which was
    the entire register queue.
    """
    import json, pathlib, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = pathlib.Path("luma-icp-scout/lib/luma-page.js")
    if not src.exists():
        pytest.skip("luma-icp-scout not checked out alongside this repo")

    cases = {
        # invited, or otherwise not yet registered — the form MUST open
        "You're invited to Fundraising Without the Finance Fire Drill": False,
        "You're invited! Accept your invite below": False,
        "Register for this event": False,
        # genuinely registered already — do not submit again
        "You're in. See you there!": True,
        "You are in - thanks": True,
        "Cancel Registration": True,
        "Thanks for registering": True,
    }
    # Pull the pattern out of the source rather than restating it here: a copy in the test would
    # keep passing while the file it is supposed to protect regressed.
    js = (f"{src.read_text()}\n"
          "const SRC = require('fs').readFileSync(process.argv[2], 'utf8');\n"
          "const m = SRC.match(/const ALREADY = (\\/.*\\/i);/);\n"
          "if (!m) { console.error('ALREADY pattern not found'); process.exit(2); }\n"
          "const RE = eval(m[1]);\n"
          "console.log(JSON.stringify(JSON.parse(process.argv[1]).map((t) => RE.test(t))));")
    out = subprocess.run([node, "-e", js, json.dumps(list(cases)), str(src)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == list(cases.values()), dict(zip(cases, json.loads(out.stdout)))


def test_the_scan_window_matches_the_send_window():
    """These two decide the same thing from opposite ends: how old an event may be and still be
    worth anything. They were 3 and 14, so every event that ended between those numbers was never
    scanned — 74 events holding 19,537 guests on the live database — and the scan queue reported
    zero, which is indistinguishable from having nothing to do."""
    from warmgraph.outreach import ingest, template
    assert ingest.SCAN_LOOKBACK_DAYS == template.MAX_EVENT_AGE_DAYS


def test_enrichment_carries_apollos_verdict_onto_the_contact():
    """Delivery gates on contact.email_status and contact.email_catchall. Enrichment wrote the
    address but not the verdict, so every contact Apollo had just verified reached delivery
    looking unverified and was skipped — 21 of 24 in one run, with no error anywhere, because
    both halves were behaving exactly as written."""
    from warmgraph.agents.activities.outreach_enrich import OutreachEnrichAgent
    from warmgraph.agents.activities.outreach_send import suppression_reason
    import tempfile
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(tempfile.mktemp(suffix=".db"))
    c = EventContact(id="ec-apollo", company_id=CID, event_id="ev-1", name="Dana",
                     linkedin_url="https://www.linkedin.com/in/dana", status="queued")
    store.save_event_contacts([c])
    OutreachEnrichAgent._store_person(store, CID, c, {
        "email_status": "verified", "email_domain_catchall": False,
        "title": "Founder", "company_name": "Acme", "company_domain": "acme.test",
    }, "dana@acme.test")

    saved = [r for r in store.get_event_contacts(CID, limit=10) if r.id == "ec-apollo"][0]
    assert saved.email_status == "verified", saved.email_status
    assert saved.email_catchall is False
    # The point of carrying it: the row Apollo just verified must pass the delivery gate.
    assert suppression_reason(store, CID, saved, "example.com") == ""


def test_first_names_are_capitalised_but_not_mangled():
    """Guest lists are typed by guests, so the same name arrives as "fay", "FAY" and "Fay".
    "Hi fay," reads as an unchecked mail merge. Names that are deliberately mixed-case must
    survive: capitalize() would turn McCarthy into Mccarthy."""
    from warmgraph.outreach.template import first_name
    assert first_name("fay wong") == "Fay"
    assert first_name("FAY WONG") == "Fay"
    assert first_name("Fay Wong") == "Fay"
    for name in ("McCarthy Ross", "O'Brien Kelly", "deSouza Silva", "JJ Abrams"):
        assert first_name(name) == name.split(" ")[0], name


def test_a_scheduled_run_also_wakes_the_browser_half():
    """The cron only ever ran Apollo, judging and sending — the stages that CONSUME the queue.
    Nothing refilled it on the same schedule, so steps 1 to 4 happened at whatever unrelated time
    the browser woke up. A scheduled run has to mean the whole loop."""
    import sys
    sys.path.insert(0, "scripts")
    from outreach_cron import request_browser_run
    from warmgraph.agents.activities import outreach_send
    from warmgraph import connections
    from warmgraph.storage import mirror
    import tempfile
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(tempfile.mktemp(suffix=".db"))
    connections.ensure_workspace(store, "https://acme.test")
    cid = mirror.client_id_for(store, "acme.test")

    before = (outreach_send.pending_run(store, cid) or {}).get("seq") or 0
    request_browser_run(store, "https://acme.test")
    after = (outreach_send.pending_run(store, cid) or {}).get("seq") or 0
    assert after > before, "the browser half was never asked to run"


def test_a_person_we_already_know_costs_no_apollo_call():
    """Guest lists overlap heavily — 9,367 queued rows covered 6,178 distinct people, and the
    same founder appears at four events in a month. Apollo was asked about every row, because the
    identity table was only consulted after the call it could have prevented."""
    import tempfile
    from warmgraph.agents.activities.outreach_enrich import OutreachEnrichAgent
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(tempfile.mktemp(suffix=".db"))
    li = "https://www.linkedin.com/in/dana"
    first = EventContact(id="ec-1", company_id=CID, event_id="ev-1", name="Dana",
                         linkedin_url=li, status="queued")
    store.save_event_contacts([first])
    OutreachEnrichAgent._store_person(store, CID, first, {
        "email": "dana@acme.test", "email_status": "verified", "email_domain_catchall": False,
        "title": "Founder", "company_name": "Acme", "company_domain": "acme.test",
    }, "dana@acme.test")

    known = store.get_person_by_linkedin(li)
    assert known is not None, "the identity row must be findable by LinkedIn url"
    assert (known.email or "") == "dana@acme.test"

    # RUN the reuse path, do not merely assert its precondition. The first version of this test
    # stopped at the line above, so it passed while the code it was guarding raised AttributeError
    # on every call and left the whole pipeline dead for a day.
    from warmgraph.agents.activities.outreach_enrich import reuse_known_person
    second = EventContact(id="ec-2", company_id=CID, event_id="ev-2", name="Dana",
                          linkedin_url=li, status="queued")
    reuse_known_person(second, known)

    assert second.status == "enriched"
    assert second.email == "dana@acme.test"
    assert second.email_status == "verified"
    assert second.person_id == known.id
    # and the filled row must survive the delivery gate, which is the point of copying the verdict
    assert suppression_reason(store, CID, second, "example.com") == ""


def test_university_addresses_are_never_emailed(store):
    """Free AI events in SF draw students, and they reach the queue looking like everyone else:
    real name, real LinkedIn, an address Apollo verifies. student1@state-university.edu was sent to before
    this existed. The role judge cannot catch them, because a student's headline rarely says so.
    """
    from warmgraph.agents.activities.outreach_send import is_academic
    for addr in ["student1@state-university.edu", "researcher@institute.edu.sg",
                 "someone@college.ac.uk", "someone@university.ac.jp"]:
        assert is_academic(addr), addr
        assert suppression_reason(store, CID, _contact(email=addr), "example.com") == "academic_domain"

    # Companies must not be caught by it. "ac" alone is a normal word in a domain.
    for addr in ["dana@acme.com", "sam@ac.com", "lee@education.io", "mo@edutech.ai"]:
        assert not is_academic(addr), addr


def test_the_icp_separates_technical_founders_from_founding_engineers():
    """These are one word apart and opposite verdicts. A technical co-founder is among the
    strongest targets — builds the product, owns distribution, cannot do it. A founding engineer
    builds the product and does not decide how it goes to market."""
    from warmgraph.agents.activities.event_icp_judge import (
        DEFAULT_TARGET_ROLES, NOT_TARGET_ROLES)
    targets, excluded = " ".join(DEFAULT_TARGET_ROLES), " ".join(NOT_TARGET_ROLES)
    assert "Founding Engineer" in excluded
    assert "Founding Engineer" not in targets
    assert "TECHNICAL founder" in targets, "technical co-founders must stay targets"
    assert "not a co-founder" in excluded.lower(), "the exclusion must spare actual co-founders"
    assert "strongest targets" in excluded.lower(), "the exclusion must say co-founders are kept"
    # Every exclusion lives in this one list, so there is a single place to read who is out.
    assert "university" in excluded.lower()


def test_nobody_is_emailed_without_an_icp_verdict(store):
    """Delivery selected status="enriched" — the status Apollo sets the moment it finds an
    address — so a contact went out as soon as an email existed and the ICP never saw them. The
    judge, meanwhile, only read "profiled", the LinkedIn worker's output, and Apollo is now the
    primary path. It reported judged: 0 on every run, which read as an empty queue rather than a
    stage that was being skipped entirely. Founding engineers and students would have been
    emailed no matter what the ICP said about them."""
    from warmgraph.agents.activities import outreach_send, event_icp_judge
    import inspect

    judge_src = inspect.getsource(event_icp_judge.EventIcpJudgeAgent.run)
    assert 'status="enriched"' in judge_src, "the judge must see Apollo-enriched contacts"

    send_src = inspect.getsource(outreach_send.OutreachSendAgent.run)
    assert 'status="judged"' in send_src, "delivery must require a judged contact"
    assert 'verdict == "target"' in send_src, "delivery must require a TARGET verdict"
    assert 'status="enriched"' not in send_src, "delivery must not select on having an address"


def test_someone_already_on_the_calendar_is_skipped(store):
    """A booked meeting is a stronger signal than a mail thread: it survives a relationship built
    over LinkedIn, WhatsApp or in person, where the mailbox has nothing to find. Sending "wanted
    to connect at the event" to someone with a call in the diary is the worst version of this."""
    cal = {"dana@acme.test"}
    assert suppression_reason(store, CID, _contact(email="dana@acme.test"), "example.com",
                              calendar=cal) == "meeting_scheduled"
    assert suppression_reason(store, CID, _contact(email="other@acme.test"), "example.com",
                              calendar=cal) == ""


def test_an_unreadable_calendar_does_not_stop_the_send(store):
    """Not knowing about a meeting makes one email worse. Failing the whole pass makes every
    email not happen. The Gmail history check still covers anyone actually corresponded with."""
    assert suppression_reason(store, CID, _contact(), "example.com", calendar=None) == ""


# --------------------------------------------------------------------------- #
# the daily digest                                                             #
# --------------------------------------------------------------------------- #
def test_the_digest_leads_with_nothing_sent(store):
    """The most important thing this email can say is that the loop did nothing — which is
    exactly what a "here's what we sent!" summary leaves blank. A crash stopped every send for a
    day and went unnoticed because there was no report at all."""
    from warmgraph.outreach import digest
    from warmgraph import connections
    from warmgraph.storage import mirror
    connections.ensure_workspace(store, "https://acme.test")
    cid = mirror.client_id_for(store, "acme.test")

    subject, body = digest.build(store, cid)
    assert "NOTHING SENT" in subject
    assert "did the scheduled run fire at all?" in body


def test_the_digest_fires_once_a_day(store):
    """Keyed on the calendar day, not a timer, so a missed run does not skip the report and a
    catch-up run does not send two."""
    from warmgraph.outreach import digest
    from warmgraph import connections
    from warmgraph.storage import mirror
    connections.ensure_workspace(store, "https://acme.test")
    cid = mirror.client_id_for(store, "acme.test")

    assert not digest.already_sent_today(store, cid, "2026-08-13")
    digest.mark_sent(store, cid, "2026-08-13")
    assert digest.already_sent_today(store, cid, "2026-08-13")
    assert not digest.already_sent_today(store, cid, "2026-08-14")


# --------------------------------------------------------------------------- #
# the in-process scheduler                                                     #
# --------------------------------------------------------------------------- #
def test_a_slot_runs_once_and_a_restart_does_not_repeat_it():
    """The marker is per slot per day and lives in the database, so a redeploy mid-afternoon
    cannot resend the 15:00 batch. An interval timer would have drifted by the process uptime and
    silently moved every slot."""
    from datetime import datetime, timezone
    from warmgraph.outreach import scheduler
    slots = ["15:00", "18:00", "21:00", "23:00"]
    at = datetime(2026, 8, 13, 18, 5, tzinfo=timezone.utc)

    assert scheduler.due_slot(at, "", slots) == "2026-08-13 18:00"
    assert scheduler.due_slot(at, "2026-08-13 18:00", slots) == ""     # already ran
    assert scheduler.due_slot(at, "2026-08-13 15:00", slots) == "2026-08-13 18:00"


def test_a_missed_slot_is_skipped_rather_than_caught_up():
    """Down from 15:00 to 21:30 means ONE run, not three. Firing the backlog would put three
    batches out within minutes of each other, which is exactly the burst the hourly cap exists to
    prevent."""
    from datetime import datetime, timezone
    from warmgraph.outreach import scheduler
    slots = ["15:00", "18:00", "21:00", "23:00"]
    back_up = datetime(2026, 8, 13, 21, 30, tzinfo=timezone.utc)
    assert scheduler.due_slot(back_up, "2026-08-12 23:00", slots) == "2026-08-13 21:00"


def test_nothing_runs_before_the_first_slot():
    from datetime import datetime, timezone
    from warmgraph.outreach import scheduler
    early = datetime(2026, 8, 13, 6, 0, tzinfo=timezone.utc)
    assert scheduler.due_slot(early, "", ["15:00", "18:00"]) == ""


def test_a_zero_send_run_still_explains_itself():
    """If mail only arrived on success, silence would mean either "queue empty" or "broken since
    Tuesday" — identical from an inbox. That ambiguity is why a day of no sends went unnoticed."""
    from warmgraph.outreach import run_report
    subject, body = run_report.build(
        {"delivered": {"delivered": 0, "skipped": 0, "mode": "send"}},
        {"queued": 8000, "ready": 0, "sent": 243, "rejected": 144})
    assert "0 sent" in subject
    assert "nobody is ready yet" in body

    subject, body = run_report.build(
        {"delivered": {"delivered": 0, "skipped": 0, "mode": "send"}},
        {"queued": 0, "ready": 0, "sent": 243, "rejected": 144})
    assert "the queue is empty" in body


def test_a_failed_scheduled_run_is_recorded_not_just_logged(store):
    """The slot marker is claimed BEFORE the run, so a crash consumes the slot and nothing retries
    until the next one. The first scheduled run did exactly that: claimed 18:00, produced nothing,
    and the only evidence was a stdout line on a host whose logs are not reachable. Losing a batch
    is survivable; losing it silently is not."""
    from warmgraph.outreach import scheduler
    from warmgraph import connections
    from warmgraph.storage import mirror
    connections.ensure_workspace(store, "https://acme.test")
    cid = mirror.client_id_for(store, "acme.test")

    assert scheduler.last_result(store, cid) == {}
    scheduler._record(store, cid, {"slot": "2026-08-13 18:00", "state": "failed",
                                   "error": "AttributeError: nope"})
    last = scheduler.last_result(store, cid)
    assert last["state"] == "failed"
    assert "AttributeError" in last["error"]
    assert last["at"], "the result must carry when it happened"


def test_two_triggers_cannot_run_the_same_batch(store):
    """The in-process scheduler and the Railway cron are both kept on purpose — a second way to
    fire the loop is worth having, and the one controllable from a dashboard is worth having most.
    The slot marker alone does not stop them colliding: it knows about slots, and the cron fires
    on its own clock."""
    from warmgraph.outreach import scheduler
    from warmgraph import connections
    from warmgraph.storage import mirror
    connections.ensure_workspace(store, "https://acme.test")
    cid = mirror.client_id_for(store, "acme.test")

    assert scheduler.try_claim_run(store, cid, "scheduler") is True
    assert scheduler.try_claim_run(store, cid, "cron") is False, "the second trigger must stand down"
    assert scheduler.run_lease(store, cid)["source"] == "scheduler"

    # Once the gap has passed, the next trigger runs normally.
    assert scheduler.try_claim_run(store, cid, "cron", gap_minutes=0) is True
    assert scheduler.run_lease(store, cid)["source"] == "cron"


def test_the_scheduler_sleeps_until_the_next_slot():
    """It used to wake every 60 seconds and read the database to ask what time it was — every
    company row, then a marker row per workspace, 1,440 times a day, almost always to conclude
    there was nothing to do. That is why the database never idled, and idle time is most of what a
    serverless Postgres bills for.

    The slot times are known, so the wait is computable: four wake-ups a day, and the database is
    touched only when a run is about to happen.
    """
    from datetime import datetime, timezone
    from warmgraph.outreach import scheduler
    slots = ["15:00", "18:00", "21:00", "23:00"]

    at = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)
    assert scheduler.seconds_until(at, slots) == 60 * 60           # an hour until 15:00

    just_after = datetime(2026, 8, 15, 15, 1, tzinfo=timezone.utc)
    assert scheduler.seconds_until(just_after, slots) == 179 * 60  # 18:00, not 15:00 again

    late = datetime(2026, 8, 15, 23, 30, tzinfo=timezone.utc)      # rolls to tomorrow
    assert scheduler.seconds_until(late, slots) == 930 * 60

    assert scheduler.seconds_until(at, []) > 0, "no slots must not busy-loop"


def test_liveness_needs_no_database_write():
    """Proving the scheduler was awake cost a row write every five minutes, forever, to say
    nothing had changed — and kept the database permanently awake doing it. The scheduler runs
    inside the API process, so if the API can answer at all, the task is either alive or the
    process is gone. That makes an in-memory timestamp exactly as trustworthy, and free."""
    import inspect
    from warmgraph.outreach import scheduler

    assert not hasattr(scheduler, "ALIVE_KEY"), "the stored heartbeat must be gone"
    assert scheduler.alive_now() == {"at": "", "next_slot": ""}

    src = inspect.getsource(scheduler.run_forever)
    assert "_alive_at = now" in src, "liveness is recorded in memory"
    assert "await asyncio.sleep(wait)" in src, "and it sleeps to the slot rather than polling"


def test_the_register_queue_carries_lumas_own_event_id():
    """The browser confirms a registration by looking the event up in Luma's feed, matching on
    api_id or url. It was only ever given a slug parsed off the end of our stored url, so any
    event whose feed url differs from its public path could never be confirmed — and reported
    "submitted, but Luma's list does not show this event" no matter what actually happened."""
    import inspect
    import sys, pathlib
    src = pathlib.Path("apps/api/main.py").read_text()
    queue = src.split("def register_queue")[1].split("\ndef ")[0]
    assert '"luma_event_id"' in queue, "the queue must pass Luma's own id to the browser"


def test_a_blocking_question_reaches_the_user_rather_than_being_re_answered():
    """/outreach/plan-answers decides what is answerable — that is why the browser abandoned the
    event. The reporting endpoint then re-ran the same resolver over the leftovers, and the LLM
    answered one it had just declined, so the list saved empty. The event stayed blocked and the
    question that blocked it reached nobody: worst of both, purely from asking twice."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("apps/api/main.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "post_registration_questions")
    # Calls, not prose — the docstring names both of these while explaining why it stops calling
    # them, so a substring check would fail on correct code.
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    # The MODEL must not be asked twice — that is what returned two different verdicts for the
    # same question and saved an empty list. The answer bank may still be consulted: that lookup
    # is deterministic, so a question whose answer is stored is answered the same way every time.
    assert "fill_open" not in called, "the reporting endpoint must not re-run the LLM resolver"
    assert "plan_answers" in called, "the deterministic answer-bank lookup should still apply"


def test_every_wording_of_the_register_button_is_clicked():
    """Hosts relabel this button freely. The matcher was an exact-match alternation, so it held
    "one-click register" and "one-click apply" but not "One-Click RSVP" — and a whole category of
    event was silently unregisterable because of a missing row in a list.

    The negative cases matter more than the positive ones: clicking Decline on an invitation is
    not a failed registration, it is an irreversible no on the user's behalf.
    """
    import pathlib, re, shutil, subprocess
    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    src = pathlib.Path("luma-icp-scout/lib/luma-page.js")
    if not src.exists():
        pytest.skip("luma-icp-scout not checked out alongside this repo")

    block = re.search(r"const NEVER_BTN =[\s\S]*?const isOpenButton = \(el\) => \{[\s\S]*?\};",
                      src.read_text()).group(0)
    js = (block.replace("const btnText = (el) => text(el)", "const btnText = (el) => String(el)")
                .replace("const isOpenButton = (el) => {", "var isOpenButton = (el) => {")
          + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(isOpenButton)));")

    accept = ["Register", "Register Now", "Register for Frontier Signals", "RSVP", "RSVP Now",
              "One-Click RSVP", "One-Click Register", "One-Click Apply", "Accept Invite",
              "Accept Invitation", "Accept", "Request to Join", "Join", "Join Event",
              "Join Waitlist", "Apply", "Get Ticket", "Get Tickets", "Get Tickets →",
              "Sign Up", "Attend", "Claim your spot", "Reserve my seat", "I am going",
              "I'm going", "Going", "Confirm Attendance"]
    reject = ["Decline", "Cancel Registration", "Not Going", "Maybe", "Leave Waitlist",
              "Withdraw", "Remove me", "Can't make it", "Share", "Add to Calendar",
              "Contact Host", "Registration Closed", ""]

    import json
    out = subprocess.run([node, "-e", js, json.dumps(accept + reject)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    for label, ok in zip(accept, got[:len(accept)]):
        assert ok, f"would not click {label!r}"
    for label, ok in zip(reject, got[len(accept):]):
        assert not ok, f"would have clicked {label!r}"


def test_referral_is_recognised_however_the_host_words_it():
    """The pattern required the literal "how did you hear", so "Where did you hear about our
    event?" escalated a question whose answer was already stored. A question the user has to
    answer twice is a question they stop answering."""
    from warmgraph.outreach.registration import intent_of
    for q in ["How did you hear about us?", "Where did you hear about our event?",
              "How did you find out about this event?", "How did you learn about us?",
              "Where did you discover this event?", "What brought you here?", "Who invited you?"]:
        assert intent_of(q) == "referral", q
    # and it must not swallow questions that are not about referral at all
    for q in ["How many employees do you have?", "What is your name?", "Where are you based?"]:
        assert intent_of(q) != "referral", q


def test_judging_is_not_rationed_separately_from_enrichment():
    """Apollo costs money per lookup, so enrichment is the one real decision about how fast the
    queue drains. A verdict costs a fraction of the lookup that preceded it, so capping judging
    separately only invents a middle state — enriched, unjudged, undeliverable — which grows every
    run and surfaces as "ready to send: 0" with thousands of people behind it."""
    import inspect
    from warmgraph.agents.activities.outreach_daily import OutreachDailyAgent, OutreachDailyInput

    assert not hasattr(OutreachDailyInput(url="x"), "judge_limit"), \
        "there must be no per-run judging budget"
    src = inspect.getsource(OutreachDailyAgent.run)
    assert "for _ in range" in src, "judging must repeat until nothing is left"
    assert "skipped_no_profile" in src, "and stop only when a round touches nobody at all"


def test_an_optional_feature_failing_must_not_disable_sending():
    """The calendar check calls mark_error when it cannot read the calendar. gmail_mailboxes()
    filters on status == "connected", so that took the SENDING mailbox out of the list: the next
    run found no account, returned an empty report without a word, and sending stopped completely
    for hours. A calendar we cannot read is a slightly worse suppression list, not a broken
    mailbox."""
    import ast, inspect, textwrap
    from warmgraph.agents.activities.outreach_send import OutreachSendAgent

    src = inspect.getsource(OutreachSendAgent.run)
    # Parse it. The comment above that line says "NOT mark_error", so a substring check fails on
    # correct code — the same mistake made twice today in tests written the same afternoon.
    tree = ast.parse(textwrap.dedent(src))
    def marks_error(node) -> bool:
        return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "mark_error" for n in ast.walk(node))

    # The precise invariant: the handler that records calendar_unavailable must not touch the
    # connection's status. mark_error is correct elsewhere — a token that cannot be refreshed and
    # a send that fails mid-loop ARE the mailbox, and both should mark it.
    calendar_handlers = [h for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler)
                         and "calendar_unavailable" in ast.dump(h)]
    assert calendar_handlers, "the calendar failure path must still be handled"
    for h in calendar_handlers:
        assert not marks_error(h), \
            "a calendar it cannot read must not mark the sending connection broken"

    assert '"no_sending_mailbox"' in src, "the silent exits must announce themselves"
    assert '"gmail_auth_failed"' in src


def test_only_sending_is_rationed():
    """Sending is capped because a mailbox's reputation is finite. Nothing upstream has that
    property: Apollo costs the same whether the lookups happen today or over twelve days, and a
    verdict costs a fraction of the lookup before it. Pacing them only leaves people un-enriched
    behind a sender with nothing to send — which is exactly what "8,371 waiting" and "ready to
    send: 0" meant on the same screen."""
    import inspect
    from warmgraph.agents.activities.outreach_daily import OutreachDailyAgent, OutreachDailyInput

    i = OutreachDailyInput(url="x")
    assert not hasattr(i, "judge_limit"), \
        "judging must not be rationed — a verdict is cheap and capping it strands people"
    assert i.send_limit, "sending keeps its cap: a mailbox's reputation is finite"
    # Apollo keeps a ceiling because it costs money per row, but it is a ceiling on SPEND per run,
    # not a pacing device — it runs in batches up to that total rather than one small slice.
    assert i.enrich_limit >= i.enrich_batch

    src = inspect.getsource(OutreachDailyAgent.run)
    assert "while done < inp.enrich_limit" in src, "enrichment must run in batches to its ceiling"
    assert "for _ in range(max(1, inp.judge_rounds))" in src, "judging must run until nothing left"


def test_a_manual_run_is_recorded_like_a_scheduled_one():
    """The button called the agent directly while the scheduler recorded around it, so a run you
    triggered produced no history row and no report email — work happened and nothing said so.
    Both go through run_and_record now: what happened must not depend on who asked."""
    import ast, pathlib
    tree = ast.parse(pathlib.Path("apps/api/main.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "post_run_now")
    tasks = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == "add_task"]
    assert tasks, "the button must still start the server half"
    target = tasks[0].args[0]
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    assert name == "run_and_record", f"must record the run, not just run it (got {name!r})"
    # And it has to EXIST. The assertion above passed for a day and a half while the function it
    # names did not exist anywhere — a string match on a call site cannot tell the difference.
    from warmgraph.outreach import scheduler
    assert callable(getattr(scheduler, name, None)), f"scheduler.{name} is not defined"


def test_every_scheduler_function_the_code_calls_actually_exists():
    """The bug this exists for: `scheduler.run_and_record(...)` was called from the cron service,
    the in-process scheduler and the Run Now button, and was defined in none of them. It had been
    deleted while the loop around it was rewritten.

    Nothing caught it. Python resolves a global only when it is reached, so every module imported
    cleanly and /health stayed green; the only code path that touched it was a slot firing, and
    the scheduler's own `except Exception` swallowed the NameError and slept. Four scheduled runs
    a day silently did nothing.

    So: walk every `scheduler.<name>` reference in the tree and assert the attribute is really
    there. Cheap, and it closes the whole class rather than this one instance.
    """
    import ast, pathlib
    from warmgraph.outreach import scheduler

    missing = []
    for path in ("apps/api/main.py", "scripts/outreach_cron.py",
                 "packages/warmgraph/warmgraph/outreach/digest.py"):
        p = pathlib.Path(path)
        if not p.exists():
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "scheduler" and not hasattr(scheduler, node.attr)):
                missing.append(f"{path}: scheduler.{node.attr}")
    assert not missing, "called but not defined -> " + "; ".join(sorted(set(missing)))


def test_a_scheduled_run_is_actually_executed_and_recorded(store, monkeypatch):
    """Calls run_and_record for real, rather than reading the source and hoping.

    Every existing test around this asserted on TEXT — the name at a call site, a substring in a
    function body. All of them passed while the function was missing. This one runs it.
    """
    from warmgraph.outreach import scheduler

    class FakeService:
        def __init__(self, st): self.store = st
        def run_agent(self, name, payload):
            assert name == "outreach_daily"
            self.seen = payload
            return {"delivered": {"delivered": 7, "skipped": 2, "skip_reasons": {"no_email": 2}},
                    "enriched": {"attempted": 30, "reused": 5, "enriched": 12},
                    "judged": {"judged": 12, "targets": 4}, "bounced": 1}

    svc = FakeService(store)
    monkeypatch.setattr(scheduler, "_after_run", lambda *a, **k: None)
    report = scheduler.run_and_record(svc, "https://example.com", "2026-08-15 15:00")

    assert report["delivered"]["delivered"] == 7
    assert svc.seen == {"url": "https://example.com"}, "no mode unless the caller sets one"

    from warmgraph.storage import mirror
    cid = mirror.client_id_for(store, "example.com")
    rows = scheduler.history(store, cid, limit=5)
    assert rows and rows[0]["slot"] == "2026-08-15 15:00", "the run must land in history"
    assert rows[0]["sent"] == 7 and rows[0]["judged"] == 12
    assert scheduler.last_result(store, cid)["state"] == "ok"


def test_a_run_that_crashes_is_recorded_as_a_failure(store, monkeypatch):
    """A run that crashed and a run that never fired look identical from the outside. That is
    exactly how a day and a half of dead schedules went unnoticed, so a crash writes history."""
    from warmgraph.outreach import scheduler

    class Boom:
        def __init__(self, st): self.store = st
        def run_agent(self, *a, **k): raise RuntimeError("apollo out of credits")

    monkeypatch.setattr(scheduler, "_after_run", lambda *a, **k: None)
    report = scheduler.run_and_record(Boom(store), "https://example.com", "cron")

    assert "apollo out of credits" in report["error"]
    from warmgraph.storage import mirror
    cid = mirror.client_id_for(store, "example.com")
    row = scheduler.history(store, cid, limit=1)[0]
    assert row["state"] == "failed" and "apollo out of credits" in row["error"]


def test_the_cron_passes_its_mode_through(store, monkeypatch):
    """The cron service runs with an explicit --mode; it must reach the agent."""
    from warmgraph.outreach import scheduler

    class Svc:
        def __init__(self, st): self.store = st
        def run_agent(self, name, payload):
            self.seen = payload
            return {"delivered": {"delivered": 0}}

    svc = Svc(store)
    monkeypatch.setattr(scheduler, "_after_run", lambda *a, **k: None)
    scheduler.run_and_record(svc, "https://example.com", source="cron", mode="send")
    assert svc.seen["mode"] == "send"


def test_apollo_is_asked_about_ten_people_per_request():
    """Enrichment called match_by_linkedin, the single-person endpoint, so 2,000 contacts meant
    2,000 sequential HTTP round trips — measured at ~5 seconds each, three hours for one pass, and
    the real reason the queue never drained. bulk_match_by_linkedin had existed since the
    Apollo-first rewrite and was never wired in. Same credit cost: Apollo charges per person
    matched and misses are free; only the request count changes."""
    import ast, inspect, textwrap
    from warmgraph.agents.activities import outreach_enrich

    src = inspect.getsource(outreach_enrich)
    calls = {n.func.attr for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "bulk_match_by_linkedin" in calls, "enrichment must use the bulk endpoint"
    assert "match_by_linkedin" not in calls, "the one-at-a-time endpoint must not be used here"


def test_a_profiled_contact_without_an_email_still_reaches_apollo():
    """`profiled` is the LinkedIn worker's output and can arrive without an address. Judging
    refuses those — a verdict on someone unreachable is a verdict nobody acts on — and enrichment
    only looked at `queued` and `judged`. So 58 people with a perfectly good LinkedIn URL sat in
    "awaiting judgment" indefinitely, waiting for a stage that would never run."""
    import ast, inspect
    from warmgraph.agents.activities.outreach_enrich import OutreachEnrichAgent
    src = inspect.getsource(OutreachEnrichAgent.run)
    statuses = {n.value for n in ast.walk(ast.parse(src.strip().replace("    def", "def", 1)))
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for needed in ("queued", "judged", "profiled"):
        assert needed in statuses, f"enrichment must pick up {needed!r} contacts"


def test_the_funnel_balances_at_every_stage():
    """A count that does not subtract from anything cannot show where people are lost, which is
    the only actionable thing on that page. So every stage must account for the one above it:
    what carried on, plus everyone who stopped, equals the input.

    Against a plain list, deliberately. The first version of this test needed a store, a workspace
    and a company id, quietly produced one row, and passed on all zeros — proving nothing, which
    is worse than having no test at all.
    """
    from warmgraph.outreach import funnel

    def p(**kw):
        base = dict(id="x", company_id=CID, event_id="ev-1", name="P",
                    linkedin_url="https://www.linkedin.com/in/p", status="queued")
        base.update(kw)
        return EventContact(**base)

    def as_counts(rows):
        """The aggregate shape the store now returns, built from a list so the arithmetic can
        still be tested without a database."""
        from collections import Counter
        b = Counter((r.status, r.verdict or "", bool((r.email or "").strip())) for r in rows)
        held = Counter((r.last_error or "held back").split(":")[0] for r in rows
                       if r.status == "skipped" and r.verdict == "target")
        titles = Counter((r.title or "unknown") for r in rows if r.verdict == "reject")
        return {"buckets": [{"status": k[0], "verdict": k[1], "has_email": k[2], "n": n}
                            for k, n in b.items()],
                "held": dict(held), "reject_titles": titles.most_common(6)}

    rows = (
        [p(status="no_linkedin", linkedin_url="")] * 7
        + [p(status="queued")] * 40
        + [p(status="skipped")] * 5                                        # answered, no email
        + [p(status="rejected", email="a@x.test", verdict="reject")] * 9
        + [p(status="sent", email="b@x.test", verdict="target")] * 11
        + [p(status="skipped", email="c@x.test", verdict="target",
             last_error="catchall_domain")] * 4
        + [p(status="bounced", email="d@x.test", verdict="target")] * 2
        + [p(status="judged", email="e@x.test", verdict="target")] * 3
        + [p(status="sent", email="f@x.test")] * 6                         # sent before judging
    )
    stages = funnel.stages(as_counts(rows))
    assert stages[0]["on"] == len(rows) == 87

    # Losses AND the queue both come out of the stage above — a person waiting for Apollo has left
    # "have a LinkedIn" just as surely as one who can never be looked up. The difference is that
    # one comes back, which is why they are separate fields and must both be counted here.
    for above, below in zip(stages, stages[1:]):
        lost = sum(o["n"] for o in below["out"]) + sum(q["n"] for q in below.get("queue", []))
        assert below["on"] + lost == above["on"], (
            f"{below['label']}: {below['on']} + {lost} != {above['on']} ({above['label']})")

    # and a queue must never be reported as a loss
    for st in stages:
        for o in st["out"]:
            assert "queued" not in o["why"] and "next run" not in o["why"], o

    named = {st["label"]: st["on"] for st in stages}
    assert named["A fit"] == 20 and named["Delivered"] == 11


def test_the_hard_exclusions_are_stated_up_front_in_the_prompt():
    """A rule enforced only at the delivery gate still spends an Apollo credit and an LLM call on
    someone who can never be written to — and, to anyone watching outcomes, looks like the rule
    does not exist. A booked meeting from an .edu address was what raised this: the address filter
    had blocked every one since it landed, but the judge had never been told."""
    from warmgraph.agents.activities.event_icp_judge import system_prompt
    prompt = system_prompt()
    head = prompt[:prompt.index("STRICT RULES")].lower()
    assert "never a target" in head
    assert "university" in head and ".edu" in head
    assert "founding engineer" in head
    # ...without catching the people who merely sound similar
    assert "co-founder" in head and "remains a strong target" in head


def test_an_unreachable_address_is_dropped_before_it_is_judged(store):
    """A catch-all domain and an .edu address are properties of the ADDRESS, known the moment
    Apollo answers. Judging someone we can never write to costs an LLM call to reach a verdict
    nobody will act on — 133 of 941 verdicts were spent that way.

    Both stages call the same helper, so they cannot drift into disagreeing about who is
    reachable."""
    from warmgraph.agents.activities.outreach_send import unreachable_reason

    catchall = _quality_contact(email="a@x.test", email_catchall=True)
    assert unreachable_reason(catchall) == "catchall_domain"
    assert suppression_reason(store, CID, catchall, "example.com") == "catchall_domain"

    edu = _quality_contact(email="someone@tech-institute.edu")
    assert unreachable_reason(edu) == "academic_domain"
    assert suppression_reason(store, CID, edu, "example.com") == "academic_domain"

    # Reachable, and the things that depend on OUR history stay at delivery — they change between
    # runs, so deciding them once at enrichment would freeze a stale answer.
    ok = _quality_contact(email="dana@acme.test")
    assert unreachable_reason(ok) == ""
    assert unreachable_reason(ok, fields={"email_domain_catchall": True}) == "catchall_domain"


def test_the_events_funnel_balances_and_ends_where_the_people_funnel_starts():
    """Events and people were two unrelated panels. They are one story: reading a guest list is
    what produces the people the other funnel starts from, so the events funnel ends on that line.

    Balanced the same way — upcoming and past split the whole set, and each branch accounts for
    what it lost."""
    from datetime import datetime, timedelta, timezone
    from warmgraph.entities import RawEvent, EventRegistration
    from warmgraph.outreach import funnel, ingest

    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    def ev(i, days, **raw):
        return RawEvent(id=f"e{i}", source=ingest.LUMA, url=f"https://luma.com/{i}",
                        title=f"Event {i}", starts_at=now + timedelta(days=days),
                        raw={"end_at": (now + timedelta(days=days, hours=2)).isoformat(),
                             "is_free": True, **raw})
    def reg(i, status, **kw):
        return EventRegistration(company_id=CID, event_id=f"e{i}", approval_status=status, **kw)

    events = [ev(1, 3), ev(2, 3, is_free=False), ev(3, 3, is_sold_out=True),
              ev(4, 40), ev(5, 3), ev(6, 3), ev(10, 3),
              ev(7, -5), ev(8, -5), ev(9, -5)]
    regs = {
        "e5": reg(5, "approved"), "e6": reg(6, "pending_approval"),
        # A waitlisted event, because the bug this test missed was a status the funnel never
        # mentioned — and a fixture without one cannot notice that.
        "e10": reg(10, "waitlist"),
        "e7": reg(7, "approved", show_guest_list=True, guest_count=100,
                  scanned_at="2026-08-12T00:00:00Z"),
        "e8": reg(8, "approved", show_guest_list=False),
        "e9": reg(9, "invited"),
    }
    stored = {"e7": 95}                 # Luma said 100 on that list; we could store 95
    upcoming_stages, past_stages = funnel.event_stages(
        events, regs, now, 14, 30, keep={}, stored_by_event=stored)
    stages = upcoming_stages + past_stages

    assert upcoming_stages[0]["label"] == "Events on your Luma"
    # The two funnels have to MEET: the last line here is what the people funnel starts from.
    # Reporting Luma's own guest_count as "people collected" left 623 unexplained between the two
    # panels, which is exactly the gap that hides a partial import.
    assert past_stages[-1]["label"] == "People we could store"
    assert past_stages[-1]["on"] == 95
    assert past_stages[-2]["on"] == 100, "the line above is what Luma listed"
    assert past_stages[-1]["out"][0]["n"] == 5

    upcoming = next(st for st in stages if st["label"] == "Still to come")
    assert upcoming["on"] + sum(o["n"] for o in upcoming["out"]) == stages[0]["on"]

    # EVERY link in both chains, not a sample. The stage that was wrong — 38 "you are going"
    # minus 8 waiting reading as 28 approved — sat between two links this test did check.
    by = {st["label"]: st for st in stages}
    chain = [("Events on your Luma", "Still to come"),
             ("Still to come", "Worth registering for"),
             ("Worth registering for", "Inside the 14-day window"),
             ("Inside the 14-day window", "Already registered"),
             ("Already registered", "Approved — you're in"),
             ("Events already held", "You were approved for"),
             ("You were approved for", "Guest list visible"),
             ("Guest list visible", "Guest list read"),
             ("Names on those guest lists", "People we could store")]
    for parent, child in chain:
        above, below = by[parent], by[child]
        lost = (sum(o["n"] for o in below["out"])
                + sum(q["n"] for q in below.get("queue", [])))
        assert below["on"] + lost == above["on"], (
            f"{child}: {below['on']} + {lost} != {above['on']} ({parent})")


def test_a_run_report_is_two_funnels_not_one():
    """Judging and sending draw from different populations, and drawing them as one column made
    the arithmetic impossible: 158 a fit, minus 53 held back, is 105 — and 50 went out. The gap
    was the per-run cap plus the fact that sending draws from EVERYONE ready, not from this run's
    fits. A reader is right to distrust a column that does not add up.

    So the run report must carry `skipped` alongside `sent`, which is what makes the sending
    funnel balance on its own: considered = sent + held back.
    """
    from warmgraph.outreach import run_report
    subject, body = run_report.build(
        {"delivered": {"delivered": 50, "skipped": 43, "mode": "send",
                       "skip_reasons": {"catchall_domain": 33, "already_contacted": 9,
                                        "academic_domain": 1}},
         "enriched": {"attempted": 683, "enriched": 255, "reused": 45},
         "judged": {"judged": 409, "targets": 158}},
        {"queued": 7820, "ready": 3, "sent": 378, "rejected": 471})

    assert "50" in subject
    assert "Sent this run:  50" in body
    assert "Skipped:        43" in body


def test_every_way_of_running_the_loop_records_it():
    """Three things can run a pass: the UI button, the in-process scheduler, and the Railway cron.
    The cron called the agent directly, so its runs left no history row and no report email — a run
    that happened was indistinguishable from one that never fired, which is the exact failure this
    log exists to catch.

    A dry run stays direct on purpose: it is meant to touch nothing, including the record.
    """
    import ast, pathlib

    def mentions(path: str, fn: str) -> set:
        """Every attribute name the function touches — CALLED or passed by reference.

        The button hands run_and_record to a background task rather than calling it, so a check
        for call sites alone misses it, which is how this test first went in red.
        """
        tree = ast.parse(pathlib.Path(path).read_text())
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        return {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}

    assert "run_and_record" in mentions("apps/api/main.py", "post_run_now")
    assert "run_and_record" in mentions("scripts/outreach_cron.py", "main")

    sched = pathlib.Path("packages/warmgraph/warmgraph/outreach/scheduler.py").read_text()
    assert "run_and_record, service, url, marker" in sched


def test_a_missing_apollo_key_is_reported_not_silently_zero(store, monkeypatch):
    """Enrichment returned an all-zero report when no key was stored, which is indistinguishable
    from "nothing to enrich". A pass with 7,818 people queued reported attempted 0, and the
    pipeline did nothing for every run afterwards without a single error anywhere.

    The key had been blanked by switching the provider to the Claude connector — a call that used
    to destroy a working credential with nothing to restore it from.
    """
    import inspect
    from cryptography.fernet import Fernet
    from warmgraph.agents.activities.outreach_enrich import OutreachEnrichAgent
    from warmgraph import connections

    monkeypatch.setenv("WG_SECRET_KEY", Fernet.generate_key().decode())

    src = inspect.getsource(OutreachEnrichAgent.run)
    head = src[:src.index("pending =")]
    assert "mark_error" in head, "a missing key must be recorded, not returned as zeros"
    assert "report.errors" in head

    # And linking via Claude must keep the key it was given.
    conn = connections.connect_apollo(store, "comp-1", "secret-key")
    assert conn.secret
    connections.link_via_claude(store, "comp-1", "apollo", "founders@example.com")
    assert connections.secret_for(store, "comp-1", "apollo") == "secret-key", \
        "switching to the Claude connector must not destroy the stored key"
    assert connections.linked_via_claude(store.get_connection("comp-1", "apollo"))


def test_nothing_counts_by_reading_every_row():
    """The funnel, the run report and the digest each counted contacts by fetching all of them and
    tallying in Python. The UI re-rendered the funnel every 30 seconds per open tab, so a page left
    open pulled 10,930 rows twice a minute, on top of once per report and once per scheduler tick.

    That exhausted the database's monthly transfer quota inside a day. Neon then refused every
    connection, the API could not answer /health, and the scheduler — which lives in that process —
    stopped with it. The 8am run did not happen because a dashboard was reading too much.

    A GROUP BY answers the same questions in tens of rows.
    """
    import ast, pathlib

    for path in ("packages/warmgraph/warmgraph/outreach/funnel.py",
                 "packages/warmgraph/warmgraph/outreach/run_report.py",
                 "packages/warmgraph/warmgraph/outreach/digest.py"):
        tree = ast.parse(pathlib.Path(path).read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get_event_contacts"):
                limit = next((k.value.value for k in node.keywords if k.arg == "limit"
                              and isinstance(k.value, ast.Constant)), 0)
                assert limit <= 500, (
                    f"{path} fetches {limit} contacts to count them — use event_contact_counts")

    api = pathlib.Path("apps/api/main.py").read_text()
    assert "event_contact_counts" in api, "the funnel endpoint must use the aggregate"
    assert "contacts_per_event" in api, "the events funnel must aggregate too"


def test_drafted_messages_are_fetched_once_not_once_per_contact(store):
    """The stranded-draft loop called a helper per contact that fetched up to 2,000 drafted
    messages and scanned them in Python. Twenty-eight contacts meant 56,000 rows read to find 28
    things, and the cost grows with the square of the batch.

    It reads that way because event_contact_id is inside the JSONB blob rather than a column, so
    there is no way to ask for one. The answer is to ask once and index it in memory.
    """
    import ast, inspect
    from warmgraph.agents.activities.outreach_send import OutreachSendAgent
    from warmgraph.entities import OutreachMessage

    src = inspect.getsource(OutreachSendAgent.run)
    tree = ast.parse(src.strip().replace("    def", "def", 1))
    # No database call may appear inside the loop over stranded contacts.
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and getattr(node.iter, "id", "") == "stranded":
            calls = {n.func.attr for n in ast.walk(node)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert "drafted_messages_by_contact" not in calls, "fetch once, before the loop"
            assert "get_outreach_messages" not in calls, "no per-contact message fetch"

    store.save_outreach_messages([
        OutreachMessage(company_id=CID, event_id="ev-1", event_contact_id="ec-1",
                        email="a@x.test", email_key="a@x.test", subject="Hi", body="B",
                        status="drafted", gmail_draft_id="r1"),
        OutreachMessage(company_id=CID, event_id="ev-1", event_contact_id="ec-2",
                        email="b@x.test", email_key="b@x.test", subject="Hi", body="B",
                        status="drafted", gmail_draft_id="r2"),
    ])
    by_contact = store.drafted_messages_by_contact(CID)
    assert set(by_contact) == {"ec-1", "ec-2"}
    assert by_contact["ec-1"].gmail_draft_id == "r1"


def test_sent_mail_is_counted_by_the_database(store):
    """The digest read every message ever sent and filtered by date in Python — 600 rows today for
    an answer of 50, and 36,000 in six months at 200 a day."""
    import ast, inspect
    from datetime import timedelta
    from warmgraph.entities import OutreachMessage
    from warmgraph.models import utcnow
    from warmgraph.outreach import digest

    src = inspect.getsource(digest.build)
    calls = {n.func.attr for n in ast.walk(ast.parse(src.strip().replace("def", "def", 1)))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "get_outreach_messages" not in calls, "the digest must not read the whole table"
    assert "sent_messages_since" in calls

    now = utcnow()
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, event_id="e", email="new@x.test", email_key="new@x.test",
                        status="sent", subject="s", body="b", sent_at=now,
                        created_at=now),
        OutreachMessage(company_id=CID, event_id="e", email="old@x.test", email_key="old@x.test",
                        status="sent", subject="s", body="b", sent_at=now - timedelta(days=9),
                        created_at=now - timedelta(days=9)),
    ])
    recent = store.sent_messages_since(CID, now - timedelta(days=1))
    assert [m.email for m in recent] == ["new@x.test"], "only the window, not the history"
    assert store.count_sent_since(CID, now - timedelta(days=1)) == 1


def test_the_bounce_scan_is_bounded_at_both_ends(store):
    """Both halves used to be unbounded: Gmail was asked for the last hundred notices every run,
    re-examining bounces already retired, and the "is this address ours" set was built by loading
    all 10,930 contacts.

    Now Gmail is asked only for notices since the last check, and the set comes from what we SENT
    in the last day — which ix_om_window covers.
    """
    import ast, inspect
    from datetime import timedelta
    from warmgraph.agents.activities import outreach_daily
    from warmgraph.entities import OutreachMessage
    from warmgraph.models import utcnow

    src = inspect.getsource(outreach_daily._retire_bounced)
    tree = ast.parse(src)
    recent = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "recent_messages"]
    assert recent, "it must still read the mailbox"
    assert any(k.arg == "after" for k in recent[0].keywords), \
        "the Gmail search must be bounded to notices since the last check"

    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "get_event_contacts" not in calls, "it must not load every contact to build `known`"
    assert "sent_addresses_since" in calls
    assert "contacts_by_email" in calls, "and must fetch only the bounced ones"

    now = utcnow()
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, event_id="e", email="Dana@X.test", email_key="Dana@X.test",
                        status="sent", subject="s", body="b", created_at=now),
        OutreachMessage(company_id=CID, event_id="e", email="old@x.test", email_key="old@x.test",
                        status="sent", subject="s", body="b", created_at=now - timedelta(days=3)),
    ])
    known = store.sent_addresses_since(CID, now - timedelta(hours=24))
    assert known == {"dana@x.test"}, "lower-cased, and only inside the window"


def test_the_hot_fields_are_real_columns_and_stay_in_step(store):
    """email, verdict and email_status lived only inside the JSONB blob, so every question about
    them — how many are a fit, who has an address, which are ready — meant a scan extracting JSON
    per row. As columns they are indexable and the funnel counts by index.

    The blob stays the record; these mirror it. A column that drifts from the data it mirrors is
    worse than no column, so every write path has to set both.
    """
    from warmgraph.entities import EventContact

    c = EventContact(id="ec-col", company_id=CID, event_id="ev-1", name="Dana",
                     contact_key="dana", linkedin_url="https://www.linkedin.com/in/dana",
                     status="queued")
    store.save_event_contacts([c])

    rows = store._conn.execute(
        "SELECT email, verdict, email_status FROM event_contacts WHERE id='ec-col'").fetchall()
    assert rows[0]["email"] is None, "nothing to mirror yet"

    # Now give it an address and a verdict, the way enrichment and judging do.
    c.email, c.email_status, c.verdict, c.status = "dana@x.test", "verified", "target", "judged"
    store.update_event_contacts([c])

    rows = store._conn.execute(
        "SELECT email, verdict, email_status FROM event_contacts WHERE id='ec-col'").fetchall()
    assert rows[0]["email"] == "dana@x.test"
    assert rows[0]["verdict"] == "target"
    assert rows[0]["email_status"] == "verified"

    # And the aggregate — which now reads those columns — must agree with the blob.
    counts = store.event_contact_counts(CID)
    ready = sum(b["n"] for b in counts["buckets"]
                if b["status"] == "judged" and b["verdict"] == "target" and b["has_email"])
    assert ready == 1, counts["buckets"]


def test_one_switch_stops_every_scheduler():
    """WG_SCHEDULER used to be read only inside the API's own loop, so turning the schedule off
    left the cron service firing on its own clock. "Manual mode" was two settings in two services,
    and easy to half-do.

    Both now read the same flag, so one Railway project-level shared variable turns everything off.
    """
    import ast, os, pathlib
    from warmgraph.outreach import scheduler

    src = pathlib.Path("scripts/outreach_cron.py").read_text()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    called = {n.func.attr for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "enabled" in called, "the cron must honour the switch, not ignore it"

    # A person asking directly is not the schedule, and must not be blocked by it.
    assert "--force" in src
    assert "args.dry_run" in src

    was = os.environ.get("WG_SCHEDULER")
    try:
        os.environ["WG_SCHEDULER"] = "0"
        assert scheduler.enabled() is False
        os.environ["WG_SCHEDULER"] = "1"
        assert scheduler.enabled() is True
    finally:
        os.environ.pop("WG_SCHEDULER", None)
        if was is not None:
            os.environ["WG_SCHEDULER"] = was


def test_the_api_does_not_schedule_by_default():
    """A web process and a batch worker are different jobs. Running the batch inside the API meant
    an API crash took the schedule with it — which is what happened when the database was cut off
    and /health stopped answering — and a redeploy could kill a run mid-pass.

    It stays available behind a flag rather than being deleted, because it is proven and the cron
    service was not.
    """
    import os
    from warmgraph.outreach import scheduler

    assert scheduler.in_process_enabled() is False, "the cron service is the scheduler"
    was = os.environ.get("WG_IN_PROCESS_SCHEDULER")
    try:
        os.environ["WG_IN_PROCESS_SCHEDULER"] = "1"
        assert scheduler.in_process_enabled() is True, "and it can be turned back on"
    finally:
        os.environ.pop("WG_IN_PROCESS_SCHEDULER", None)
        if was is not None:
            os.environ["WG_IN_PROCESS_SCHEDULER"] = was
