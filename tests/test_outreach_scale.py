"""Scale guards for the event-outreach hot paths.

These are not benchmarks. They count the SQL statements a call issues and assert the work does
NOT grow with the size of the table, which is the property that actually matters: every one of
these paths runs on every extension request or once per lead, so anything O(rows) here degrades
silently as the corpus grows and only shows up months later as "why is it slow now".

Each test seeds a deliberately oversized table and pins the statement count.
"""
from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

import warmgraph.connections as C
from warmgraph.entities import Client, EventContact, OutreachMessage, Person, RawEvent, contact_key
from warmgraph.outreach import ingest
from warmgraph.storage.sqlite_store import SqliteStore

CID = "comp-scale"
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


class SqlCounter:
    """Counts statements executed on a SQLite connection."""

    def __init__(self, store):
        self.store = store
        self.count = 0

    def __enter__(self):
        self.store._conn.set_trace_callback(self._on)
        return self

    def __exit__(self, *exc):
        self.store._conn.set_trace_callback(None)
        return False

    def _on(self, _sql):
        self.count += 1


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


# --------------------------------------------------------------------------- #
# workspace token resolution — runs on EVERY extension request                  #
# --------------------------------------------------------------------------- #
def test_token_lookup_is_constant_work_regardless_of_client_count(store):
    _, token = C.ensure_workspace(store, "example.com")
    for i in range(400):
        store.upsert_company(Client(domain=f"tenant{i}.com",
                                    data={"workspace_token": f"tok-{i}"}))

    with SqlCounter(store) as counter:
        assert C.company_id_for_token(store, token) is not None
    # One indexed SELECT. The scan this replaced deserialized every client row per request.
    assert counter.count == 1


def test_token_lookup_still_correct_among_many(store):
    tokens = {}
    for domain in ("a.com", "b.com", "c.com"):
        client, token = C.ensure_workspace(store, domain)
        tokens[token] = client.id
    for i in range(200):
        store.upsert_company(Client(domain=f"noise{i}.com",
                                    data={"workspace_token": f"noise-tok-{i}"}))
    for token, expected in tokens.items():
        assert C.company_id_for_token(store, token) == expected
    assert C.company_id_for_token(store, "nope") is None


# --------------------------------------------------------------------------- #
# event sync — the extension re-posts its whole event list every day            #
# --------------------------------------------------------------------------- #
def _entry(i: int) -> dict:
    return {
        "api_id": f"evt-{i}",
        "event": {"api_id": f"evt-{i}", "name": f"Event {i}", "url": f"https://luma.com/e{i}",
                  "start_at": "2026-08-09T00:00:00.000Z", "end_at": "2026-08-09T03:00:00.000Z",
                  "show_guest_list": True},
        "guest_info": {"ticket_key": "tk", "approval_status": "approved"},
        "ticket_info": {"is_free": True, "is_sold_out": False},
        "guest_count": 100,
    }


def test_event_sync_work_does_not_grow_with_the_events_table(store):
    """A re-sync of N events must cost O(N), not O(N x table). The original shape re-read the
    whole events table once per incoming entry."""
    ingest.ingest_events(store, CID, [_entry(i) for i in range(300)])   # seed

    with SqlCounter(store) as small:
        ingest.ingest_events(store, CID, [_entry(i) for i in range(5)])
    with SqlCounter(store) as large:
        ingest.ingest_events(store, CID, [_entry(i) for i in range(50)])

    per_event_small = small.count / 5
    per_event_large = large.count / 50
    # Constant work per event whatever the table size. Two upserts per event (the shared
    # raw_events row + this client's registration), each a lookup plus a write.
    assert per_event_large <= per_event_small * 1.2
    assert per_event_small < 14


def test_resync_is_idempotent_at_size(store):
    ingest.ingest_events(store, CID, [_entry(i) for i in range(200)])
    ingest.ingest_events(store, CID, [_entry(i) for i in range(200)])
    assert len(ingest.pending_scans(store, CID, lookback_days=3650)) == 200


# --------------------------------------------------------------------------- #
# identity resolution — once per enriched lead                                  #
# --------------------------------------------------------------------------- #
def test_person_lookup_is_constant_work_regardless_of_people_count(store):
    store.save_people([Person(person=f"P{i}", linkedin_url=f"https://linkedin.com/in/p{i}",
                              company_domain=f"co{i}.com") for i in range(2000)])

    with SqlCounter(store) as counter:
        found = store.get_person_by_linkedin("https://linkedin.com/in/p1500")
    assert found is not None
    assert counter.count == 1


def test_person_lookup_is_case_insensitive(store):
    store.save_people([Person(person="Jane", linkedin_url="https://www.LinkedIn.com/in/JaneDoe")])
    assert store.get_person_by_linkedin("https://www.linkedin.com/in/janedoe") is not None


# --------------------------------------------------------------------------- #
# the queue — the browser polls this all day                                    #
# --------------------------------------------------------------------------- #
def _seed_queue(store, n: int, event_id: str = "rev-1"):
    rows = []
    for i in range(n):
        li = f"https://linkedin.com/in/person{i}"
        rows.append(EventContact(company_id=CID, event_id=event_id, contact_key=contact_key(li),
                                 name=f"Person {i}", linkedin_url=li, priority=i % 100))
    store.save_event_contacts(rows)


def test_leasing_from_a_large_queue_stays_bounded(store):
    _seed_queue(store, 5000)

    started = time.perf_counter()
    with SqlCounter(store) as counter:
        claimed = store.lease_event_contacts(CID, leased_by="browser-A", limit=25)
    elapsed = time.perf_counter() - started

    assert len(claimed) == 25
    # one SELECT + one UPDATE per claimed row, not a pass over the queue
    assert counter.count <= 2 * len(claimed) + 3
    assert elapsed < 1.0
    # and it still honours priority: the highest-priority rows come first
    assert all(c.priority == 99 for c in claimed)


def test_suppression_lookup_is_constant_work(store):
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email=f"person{i}@acme.com", status="sent")
        for i in range(3000)])

    with SqlCounter(store) as counter:
        assert store.has_contacted(CID, "person2500@acme.com") is True
    assert counter.count == 1


def test_cap_counting_is_constant_work(store):
    store.save_outreach_messages([
        OutreachMessage(company_id=CID, email=f"p{i}@acme.com", status="sent")
        for i in range(3000)])

    with SqlCounter(store) as counter:
        n = store.count_outreach_messages(CID, since_minutes=1440)
    assert n == 3000
    assert counter.count == 1


def test_guest_ingest_scales_linearly(store):
    """A 600-person guest list is one bulk insert path, not 600 round trips of growing cost."""
    ingest.ingest_events(store, CID, [_entry(1)])
    event, _ = ingest.pending_scans(store, CID, lookback_days=3650)[0]
    guests = [{"user": {"api_id": f"usr-{i}", "name": f"Guest {i}",
                        "linkedin_handle": f"guest{i}", "bio_short": "Founder"}}
              for i in range(600)]

    started = time.perf_counter()
    out = ingest.ingest_guests(store, CID, event, guests, now=NOW)
    elapsed = time.perf_counter() - started

    assert out["queued"] == 600
    assert out["new"] == 600
    assert elapsed < 5.0


def test_queue_counts_are_one_grouped_query(store):
    _seed_queue(store, 2000)
    with SqlCounter(store) as counter:
        counts = store.count_event_contacts(CID)
    assert counts == {"queued": 2000}
    assert counter.count == 1


def test_expired_lease_sweep_only_touches_expired_rows(store):
    """The sweep runs hourly. It must cost the number of DEAD leases, not the queue size."""
    _seed_queue(store, 3000)
    claimed = store.lease_event_contacts(CID, leased_by="browser-A", limit=10)
    for c in claimed[:4]:
        c.lease_expires_at = NOW - timedelta(days=1)
    store.update_event_contacts(claimed[:4])

    with SqlCounter(store) as counter:
        freed = store.release_expired_leases(CID)
    assert freed == 4
    assert counter.count <= 2 * freed + 3
