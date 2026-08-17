"""Offline tests for the event-outreach storage layer (connections → event_contacts queue →
send ledger → do-not-contact). SQLite mirror; no network, no LLM, no Gmail.

The behaviours that actually matter in production and are easy to regress:
  • re-scanning an event must NOT reset rows already in flight (insert-if-absent on contact_key)
  • leasing is a compare-and-swap, so the same row is never handed to two workers
  • a lease that expires (Chrome quit / laptop closed) returns the row to `queued`
  • suppression counts real outbound only — a `skipped` audit row must not block forever
  • the daily/hourly caps count from the ledger inside a time window
"""
from __future__ import annotations

import tempfile
from datetime import timedelta

import pytest

from warmgraph.entities import (
    Connection,
    DoNotContact,
    EventContact,
    OutreachMessage,
    contact_key,
    email_domain,
    norm_email,
)
from warmgraph.models import utcnow
from warmgraph.storage.sqlite_store import SqliteStore

CID = "comp-test"


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


def ec(**kw) -> EventContact:
    kw.setdefault("company_id", CID)
    kw.setdefault("event_id", "rev-1")
    kw.setdefault("contact_key", contact_key(kw.get("linkedin_url", ""), kw.get("luma_user_id", "")))
    return EventContact(**kw)


# --------------------------------------------------------------------------- #
# keys                                                                          #
# --------------------------------------------------------------------------- #
def test_contact_key_normalizes_linkedin_variants():
    a = contact_key("https://www.linkedin.com/in/janedoe/")
    b = contact_key("https://linkedin.com/in/janedoe?utm_source=luma")
    c = contact_key("HTTPS://WWW.LINKEDIN.COM/IN/JaneDoe")
    assert a == b == c == "li:janedoe"


def test_contact_key_falls_back_to_luma_id_then_empty():
    assert contact_key("", "usr-123") == "luma:usr-123"
    assert contact_key("", "") == ""


def test_email_helpers():
    assert norm_email("  Jane@Acme.COM ") == "jane@acme.com"
    assert email_domain("Jane@Acme.com") == "acme.com"


# --------------------------------------------------------------------------- #
# connections                                                                   #
# --------------------------------------------------------------------------- #
def test_connection_upsert_is_one_row_per_provider(store):
    a = store.upsert_connection(Connection(company_id=CID, provider="gmail",
                                           status="connected", account_label="p@example.com",
                                           secret="cipher-1"))
    b = store.upsert_connection(Connection(company_id=CID, provider="gmail",
                                           status="connected", account_label="p@example.com",
                                           secret="cipher-2"))
    assert b.id == a.id
    assert store.get_connection(CID, "gmail").secret == "cipher-2"
    assert len(store.list_connections(CID)) == 1
    assert store.delete_connection(CID, "gmail") is True
    assert store.get_connection(CID, "gmail") is None


def test_connection_redacted_never_leaks_the_secret():
    d = Connection(company_id=CID, provider="apollo", secret="super-secret").redacted()
    assert "secret" not in d
    assert d["has_secret"] is True


# --------------------------------------------------------------------------- #
# event_contacts — dedup + queue                                                #
# --------------------------------------------------------------------------- #
def test_rescanning_an_event_does_not_reset_rows_in_flight(store):
    """The scan is idempotent: a second scan of the same event inserts nothing and, critically,
    leaves a row that has already been read/judged exactly where it was."""
    first = ec(linkedin_url="https://linkedin.com/in/janedoe", name="Jane")
    assert store.save_event_contacts([first]) == 1

    first.status = "judged"
    first.verdict = "target"
    first.linkedin_text = "Founder at Acme"
    store.update_event_contacts([first])

    # same person, same event, fresh object (as a re-scan would produce)
    again = ec(linkedin_url="https://www.linkedin.com/in/janedoe/", name="Jane Doe")
    assert store.save_event_contacts([again]) == 0

    rows = store.get_event_contacts(CID, event_id="rev-1")
    assert len(rows) == 1
    assert rows[0].status == "judged"
    assert rows[0].verdict == "target"


def test_same_person_at_a_different_event_is_a_separate_row(store):
    store.save_event_contacts([ec(linkedin_url="https://linkedin.com/in/janedoe")])
    store.save_event_contacts([ec(event_id="rev-2", linkedin_url="https://linkedin.com/in/janedoe")])
    assert len(store.get_event_contacts(CID)) == 2


def test_lease_claims_by_priority_and_is_not_reissued(store):
    store.save_event_contacts([
        ec(linkedin_url="https://linkedin.com/in/low", priority=1),
        ec(linkedin_url="https://linkedin.com/in/high", priority=9),
    ])
    claimed = store.lease_event_contacts(CID, leased_by="browser-A", limit=1)
    assert [c.linkedin_url for c in claimed] == ["https://linkedin.com/in/high"]
    assert claimed[0].status == "reading"
    assert claimed[0].attempts == 1

    # a second worker must not get the same row back
    again = store.lease_event_contacts(CID, leased_by="browser-B", limit=5)
    assert [c.linkedin_url for c in again] == ["https://linkedin.com/in/low"]

    assert store.count_event_contacts(CID) == {"reading": 2}


def test_expired_lease_returns_the_row_to_the_queue(store):
    """Chrome quit mid-run. The row must come back, and only the expired one."""
    store.save_event_contacts([
        ec(linkedin_url="https://linkedin.com/in/a"),
        ec(linkedin_url="https://linkedin.com/in/b"),
    ])
    dead, live = store.lease_event_contacts(CID, leased_by="browser-A", limit=2)
    dead.lease_expires_at = utcnow() - timedelta(minutes=5)
    store.update_event_contacts([dead])

    assert store.release_expired_leases(CID) == 1
    assert store.count_event_contacts(CID) == {"queued": 1, "reading": 1}

    requeued = store.get_event_contacts(CID, status="queued")[0]
    assert requeued.id == dead.id
    assert requeued.leased_by == ""
    assert requeued.lease_expires_at is None
    # it keeps its attempt count, so a poison row can be retired after N tries
    assert requeued.attempts == 1
    assert store.get_event_contact(live.id).status == "reading"


def test_get_event_contacts_filters(store):
    store.save_event_contacts([
        ec(linkedin_url="https://linkedin.com/in/a"),
        ec(event_id="rev-2", linkedin_url="https://linkedin.com/in/b"),
    ])
    assert len(store.get_event_contacts(CID, event_id="rev-2")) == 1
    assert len(store.get_event_contacts(CID, status="queued")) == 2
    assert store.get_event_contacts(CID, status="sent") == []


# --------------------------------------------------------------------------- #
# send ledger + suppression                                                     #
# --------------------------------------------------------------------------- #
def test_has_contacted_matches_case_insensitively(store):
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email="Jane@Acme.com", status="sent")])
    assert store.has_contacted(CID, "jane@acme.com") is True
    assert store.has_contacted(CID, "  JANE@ACME.COM ") is True
    assert store.has_contacted(CID, "other@acme.com") is False
    assert store.has_contacted(CID, "") is False


def test_a_skipped_row_does_not_suppress_forever(store):
    """Skips are audit records. Someone skipped today for a missing email must still be
    reachable tomorrow once Apollo finds one."""
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email="jane@acme.com", status="skipped",
                        skip_reason="no_email")])
    assert store.has_contacted(CID, "jane@acme.com") is False


def test_suppression_is_scoped_per_client(store):
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email="jane@acme.com", status="sent")])
    assert store.has_contacted("comp-other", "jane@acme.com") is False


def test_cap_counts_only_real_sends_inside_the_window(store):
    now = utcnow()
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email="a@x.com", status="sent"),
        OutreachMessage(company_id=CID, email="b@x.com", status="drafted"),
        OutreachMessage(company_id=CID, email="c@x.com", status="skipped"),
        OutreachMessage(company_id=CID, email="d@x.com", status="sent",
                        created_at=now - timedelta(hours=26)),
    ])
    assert store.count_outreach_messages(CID, since_minutes=1440) == 2   # sent + drafted, today
    assert store.count_outreach_messages(CID, since_minutes=60) == 2
    assert store.count_outreach_messages(CID, since_minutes=1440 * 3) == 3  # + yesterday's


# --------------------------------------------------------------------------- #
# do-not-contact                                                                #
# --------------------------------------------------------------------------- #
def test_do_not_contact_matches_address_or_whole_domain(store):
    store.save_do_not_contact([
        DoNotContact(company_id=CID, value="friend@acme.com", kind="email"),
        DoNotContact(company_id=CID, value="investor.example", kind="domain"),
    ])
    assert store.is_do_not_contact(CID, "friend@acme.com") is True
    assert store.is_do_not_contact(CID, "FRIEND@ACME.COM") is True
    assert store.is_do_not_contact(CID, "partner@investor.example") is True   # domain rule
    assert store.is_do_not_contact(CID, "someone@acme.com") is False
    assert store.is_do_not_contact(CID, "") is False


def test_do_not_contact_dedups(store):
    d = DoNotContact(company_id=CID, value="friend@acme.com")
    assert store.save_do_not_contact([d]) == 1
    assert store.save_do_not_contact([DoNotContact(company_id=CID, value="friend@acme.com")]) == 0
    assert len(store.get_do_not_contact(CID)) == 1
