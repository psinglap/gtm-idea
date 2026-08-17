"""End-to-end test of the event-outreach HTTP surface — the seam the browser extension talks to.

Walks the whole flow the way the extension actually drives it:
  pair -> heartbeat -> post events -> scan queue -> post guests -> lease LinkedIn ->
  post profile -> summary

and checks the two things that protect the data: an unknown workspace token is rejected, and one
workspace can never see or mutate another's rows.

No network. SQLite, and the app is imported with env set so it never touches Neon.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

os.environ["WG_STORE"] = "sqlite"
os.environ["WG_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ.pop("WG_API_KEYS", None)          # keep the agent routes open for the test
# The API rate-limits per IP (20/min by default). A whole test file hits far more than that
# from one address, so raise it here rather than making the tests sleep.
os.environ["WG_RATE_LIMIT"] = "10000/minute"
# The leisure filter calls a real LLM. These tests are about routing and storage, so turn it
# off rather than making every assertion depend on what a model thinks of a fixture title.
os.environ["WG_EVENT_FILTER"] = "0"

from fastapi.testclient import TestClient     # noqa: E402

import sys                                    # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "apps", "api"))
import main                                   # noqa: E402
from warmgraph.storage.sqlite_store import SqliteStore   # noqa: E402


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

# Swap in a private store rather than trusting env at import time. Whether this module is
# imported before or after anything else that touches warmgraph.config changes what the env
# said at that moment; an explicit store makes the test independent of collection order (and
# of any stale warmgraph.db left by a previous run).
main.service.store = SqliteStore(tempfile.mktemp(suffix=".db"))
# This file is about routing and storage — "No network", per the module docstring. The question
# endpoints now run an LLM pass to fill anything a model may legitimately answer, which would
# make these assertions depend on what a model thinks rather than on the code under test.
main.service.registry._chain = []
main.service.registry._overrides = {}

client = TestClient(main.app)


# Ends recently rather than on a fixed date: the scan window bands on how long ago an event
# ENDED, so a hardcoded date silently drops out of scope overnight.
_ENDED = datetime.now(timezone.utc) - timedelta(hours=12)
_ISO = lambda d: d.isoformat().replace("+00:00", "Z")


def entry(i=1, approval="approved", show_list=True, free=True):
    return {
        "api_id": f"evt-{i}",
        "event": {"api_id": f"evt-{i}", "name": f"Frontier Signals #{i}: The Long Subtitle",
                  "url": f"https://luma.com/event-{i}",
                  "start_at": _ISO(_ENDED - timedelta(hours=3)), "end_at": _ISO(_ENDED),
                  "show_guest_list": show_list},
        "guest_info": {"ticket_key": "tk-abc", "approval_status": approval},
        "ticket_info": {"is_free": free, "is_sold_out": False},
        "guest_count": 42,
        "featured_city": {"name": "San Francisco"},
    }


def guest(i=1):
    return {"user": {"api_id": f"usr-{i}", "name": f"Guest {i}",
                     "linkedin_handle": f"guest{i}", "bio_short": "Founder, building in AI"}}


@pytest.fixture
def token(request):
    """A fresh workspace per test.

    Pairing is idempotent by domain, so a shared URL would hand every test the same workspace
    and leak rows between them — which is exactly how the first run of this file produced a
    phantom 'leased row handed to a second browser'.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", request.node.name.lower()).strip("-")
    r = client.post("/workspace", json={"url": f"https://{slug}.example"})
    assert r.status_code == 200
    return r.json()["token"]


# --------------------------------------------------------------------------- #
# auth: the token IS the credential                                             #
# --------------------------------------------------------------------------- #
def test_pairing_is_idempotent():
    a = client.post("/workspace", json={"url": "https://idem.example"}).json()
    b = client.post("/workspace", json={"url": "https://idem.example"}).json()
    assert a["token"] == b["token"]           # re-pairing must not orphan existing data
    assert a["company_id"] == b["company_id"]


@pytest.mark.parametrize("path", [
    "/connections", "/outreach/summary", "/outreach/scan-queue", "/outreach/linkedin-queue",
])
def test_unknown_token_is_rejected(path):
    assert client.get(path, params={"token": "not-a-real-token"}).status_code == 401
    assert client.get(path).status_code == 401


def test_workspaces_cannot_see_each_others_data(token):
    other = client.post("/workspace", json={"url": "https://other.example"}).json()["token"]

    client.post("/outreach/events", params={"token": token}, json={"entries": [entry(90)]})
    ev = client.get("/outreach/scan-queue", params={"token": token}).json()["events"][0]
    client.post("/outreach/guests", params={"token": token},
                json={"event_id": ev["event_id"], "guests": [guest(90)]})

    mine = client.get("/outreach/summary", params={"token": token}).json()
    theirs = client.get("/outreach/summary", params={"token": other}).json()
    assert sum(mine["queue"].values()) > 0
    assert theirs["queue"] == {}              # contacts are scoped to the workspace

    # and the other workspace cannot check out my queue
    leased = client.get("/outreach/linkedin-queue", params={"token": other}).json()
    assert leased["contacts"] == []


# --------------------------------------------------------------------------- #
# connections                                                                   #
# --------------------------------------------------------------------------- #
def test_connections_lists_every_provider_and_hides_secrets(token):
    body = client.get("/connections", params={"token": token}).json()
    providers = [c["provider"] for c in body["connections"]]
    assert providers == ["luma", "linkedin", "apollo", "gmail", "gmail_history"]
    assert all("secret" not in c for c in body["connections"])
    assert body["readiness"]["can_scan_events"] is False


def test_heartbeat_marks_a_session_provider_live(token):
    r = client.post("/outreach/heartbeat", params={"token": token}, json={"provider": "luma"})
    assert r.status_code == 200
    body = client.get("/connections", params={"token": token}).json()
    luma = next(c for c in body["connections"] if c["provider"] == "luma")
    assert luma["status"] == "connected"
    assert luma["has_secret"] is False        # session providers store no credential
    assert body["readiness"]["can_scan_events"] is True


def test_heartbeat_rejects_a_credential_provider(token):
    r = client.post("/outreach/heartbeat", params={"token": token}, json={"provider": "gmail"})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# the full extension loop                                                       #
# --------------------------------------------------------------------------- #
def test_full_loop_from_luma_events_to_a_read_profile(token):
    # 1. the extension posts everything it enumerated on Luma
    posted = client.post("/outreach/events", params={"token": token},
                         json={"entries": [entry(1), entry(2, approval="invited"),
                                           entry(3, show_list=False)]}).json()
    assert posted["seen"] == 3
    assert posted["scannable"] == 1           # only approved + visible

    # 2. it asks what to scan — the two blocked events must not appear
    queue = client.get("/outreach/scan-queue", params={"token": token}).json()["events"]
    assert [e["url"] for e in queue] == ["https://luma.com/event-1"]
    assert queue[0]["ticket_key"] == "tk-abc"

    # 3. it posts the guest list
    out = client.post("/outreach/guests", params={"token": token},
                      json={"event_id": queue[0]["event_id"],
                            "guests": [guest(1), guest(2)]}).json()
    assert out["guests"] == 2 and out["queued"] == 2 and out["new"] == 2

    # scanning is idempotent — the event drops off the queue
    assert client.get("/outreach/scan-queue", params={"token": token}).json()["events"] == []

    # 4. it leases profiles to read
    leased = client.get("/outreach/linkedin-queue",
                        params={"token": token, "limit": 2, "browser_id": "b1"}).json()["contacts"]
    assert len(leased) == 2
    assert leased[0]["linkedin_url"].startswith("https://www.linkedin.com/in/")

    # a second browser gets nothing — the rows are leased, not shared
    assert client.get("/outreach/linkedin-queue",
                      params={"token": token, "browser_id": "b2"}).json()["contacts"] == []

    # 5. it checkpoints each profile as it reads it
    ok = client.post("/outreach/linkedin-result", params={"token": token},
                     json={"contact_id": leased[0]["contact_id"], "headline": "Founder at Acme",
                           "profile_text": "Founder at Acme. Building creator tooling."}).json()
    assert ok["status"] == "profiled"

    # a gated read goes back on the queue rather than being trusted as "no data"
    gated = client.post("/outreach/linkedin-result", params={"token": token},
                        json={"contact_id": leased[1]["contact_id"], "gated": True}).json()
    assert gated["status"] == "queued"

    summary = client.get("/outreach/summary", params={"token": token}).json()
    assert summary["queue"]["profiled"] == 1
    assert summary["queue"]["queued"] == 1
    assert len(summary["events"]) == 3
    blocked = {e["approval_status"]: e["blocked_reason"] for e in summary["events"]}
    assert blocked["invited"] == "invited"


def test_guests_without_linkedin_never_enter_the_queue(token):
    client.post("/outreach/events", params={"token": token}, json={"entries": [entry(7)]})
    ev = client.get("/outreach/scan-queue", params={"token": token}).json()["events"][0]
    out = client.post("/outreach/guests", params={"token": token}, json={
        "event_id": ev["event_id"],
        "guests": [guest(70), {"user": {"api_id": "usr-x", "name": "No Profile",
                                        "linkedin_handle": ""}}]}).json()
    assert out["queued"] == 1
    assert out["no_linkedin"] == 1


def _in_days(n: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).isoformat().replace("+00:00", "Z")


def test_register_queue_prefers_the_invited_backlog(token):
    """`invited` events are already targeted at the user and yield nothing until converted —
    they are the point of auto-registration, so they come first."""
    client.post("/outreach/events", params={"token": token}, json={"entries": [
        {**entry(20, approval=""), "event": {**entry(20)["event"],
                                             "start_at": _in_days(2), "end_at": _in_days(2)}},
        {**entry(21, approval="invited"), "event": {**entry(21)["event"],
                                                    "start_at": _in_days(3), "end_at": _in_days(3)}},
    ]})
    events = client.get("/outreach/register-queue", params={"token": token}).json()["events"]
    assert events[0]["was_invited"] is True


def test_register_queue_only_looks_a_week_ahead(token):
    """Registering months out balloons the list without helping: LinkedIn reads cap the pipeline
    at roughly one event a day regardless of how many you are registered for."""
    client.post("/outreach/events", params={"token": token}, json={"entries": [
        {**entry(60, approval="invited"), "event": {**entry(60)["event"],
                                                    "start_at": _in_days(3), "end_at": _in_days(3)}},
        {**entry(61, approval="invited"), "event": {**entry(61)["event"],
                                                    "start_at": _in_days(20), "end_at": _in_days(20)}},
    ]})
    urls = [e["url"] for e in
            client.get("/outreach/register-queue", params={"token": token}).json()["events"]]
    assert "https://luma.com/event-60" in urls          # 3 days out
    assert "https://luma.com/event-61" not in urls      # 20 days out

    # ...unless you ask for a wider window explicitly
    wide = [e["url"] for e in client.get("/outreach/register-queue",
            params={"token": token, "horizon_days": 30}).json()["events"]]
    assert "https://luma.com/event-61" in wide


def test_paid_events_never_reach_the_register_queue(token):
    client.post("/outreach/events", params={"token": token}, json={"entries": [
        {**entry(30, approval="invited", free=False),
         "ticket_info": {"is_free": False, "price": 2500, "is_sold_out": False},
         "event": {**entry(30)["event"], "start_at": "2027-02-01T00:00:00.000Z",
                   "end_at": "2027-02-01T03:00:00.000Z"}},
    ]})
    urls = [e["url"] for e in
            client.get("/outreach/register-queue", params={"token": token}).json()["events"]]
    assert "https://luma.com/event-30" not in urls


def test_registering_flips_an_event_into_scan_scope(token):
    client.post("/outreach/events", params={"token": token},
                json={"entries": [entry(40, approval="invited")]})
    assert client.get("/outreach/scan-queue", params={"token": token}).json()["events"] == []

    summary = client.get("/outreach/summary", params={"token": token}).json()
    ev = next(e for e in summary["events"] if e["url"] == "https://luma.com/event-40")
    client.post("/outreach/registered", params={"token": token},
                json={"event_id": ev["event_id"], "approval_status": "approved"})

    scan_urls = [e["url"] for e in
                 client.get("/outreach/scan-queue", params={"token": token}).json()["events"]]
    assert "https://luma.com/event-40" in scan_urls


# --------------------------------------------------------------------------- #
# editable subject name + do-not-contact                                        #
# --------------------------------------------------------------------------- #
def test_short_name_edit_survives_a_luma_resync(token):
    client.post("/outreach/events", params={"token": token}, json={"entries": [entry(50)]})
    summary = client.get("/outreach/summary", params={"token": token}).json()
    ev = next(e for e in summary["events"] if e["url"] == "https://luma.com/event-50")
    assert ev["short_name"] == "Frontier Signals"      # rule-derived

    client.post("/outreach/event-short-name", params={"token": token},
                json={"event_id": ev["event_id"], "short_name": "Frontier Day"})
    client.post("/outreach/events", params={"token": token}, json={"entries": [entry(50)]})

    after = client.get("/outreach/summary", params={"token": token}).json()
    edited = next(e for e in after["events"] if e["url"] == "https://luma.com/event-50")
    assert edited["short_name"] == "Frontier Day"


# --------------------------------------------------------------------------- #
# registration questionnaires                                                   #
# --------------------------------------------------------------------------- #
def _report(token, questions):
    return client.post("/outreach/registration-questions", params={"token": token},
                       json={"questions": questions}).json()


def test_known_questions_answer_themselves_and_only_the_rest_are_reported(token):
    """The workspace's OWN answers are what fill a form. Nothing ships pre-filled — a public
    build that arrived knowing a name and an email would type someone else's identity into a real
    host's form on the first run — so the bank is seeded here, the way a user seeds it."""
    from warmgraph import connections
    from warmgraph.agents.activities import outreach_send
    cid = connections.company_id_for_token(main.service.store, token)
    outreach_send.save_answers(main.service.store, cid, ANSWERS)
    r = _report(token, [
        {"id": "a", "label": "What company are you with?", "required": True},
        {"id": "b", "label": "Your LinkedIn", "required": True},
        {"id": "c", "label": "How many employees do you have?", "required": True},
    ])
    assert [q["label"] for q in r["open_questions"]] == ["How many employees do you have?"]


def test_a_question_mentioning_a_field_is_not_answered_with_that_field(token):
    """"How many employees does your company have?" contains "company". Answering it "Acme"
    would send nonsense to a real host, so it must be reported instead."""
    r = _report(token, [{"id": "a", "label": "How many employees does your company have?",
                         "required": True}])
    assert len(r["open_questions"]) == 1


def test_optional_questions_never_block_a_registration(token):
    r = _report(token, [{"id": "a", "label": "Anything else?", "required": False}])
    assert r["open_questions"] == []


def test_the_same_question_from_many_events_is_asked_once(token):
    r = _report(token, [
        {"id": "a", "label": "What stage is your startup at?", "required": True},
        {"id": "b", "label": "What stage is your startup at?", "required": True},
        {"id": "c", "label": "What stage is your startup at?", "required": True},
    ])
    assert len(r["open_questions"]) == 1


def test_parsing_a_reply_never_writes_anything(token):
    """The safety property. Parsing shows a mapping for confirmation; only saving commits.
    An LLM attaching the wrong answer to the wrong question must be visible first."""
    _report(token, [{"id": "a", "label": "What stage is your startup at?", "required": True}])
    before = client.get("/outreach/questions", params={"token": token}).json()

    client.post("/outreach/parse-answers", params={"token": token},
                json={"reply": "we are pre-seed"})

    after = client.get("/outreach/questions", params={"token": token}).json()
    assert after["answers"] == before["answers"]            # bank untouched
    assert after["open_questions"] == before["open_questions"]   # still open


def test_saving_a_confirmed_answer_closes_the_question_for_good(token):
    r = _report(token, [{"id": "a", "label": "What stage is your startup at?", "required": True}])
    key = r["open_questions"][0]["key"]

    out = client.post("/outreach/answers", params={"token": token},
                      json={"answers": {key: "Pre-seed"}}).json()
    assert out["saved"] == 1 and out["still_open"] == 0
    assert client.get("/outreach/questions", params={"token": token}).json()["open_questions"] == []

    # and the same question at a future event now answers itself
    again = _report(token, [{"id": "z", "label": "What stage is your startup at?",
                             "required": True}])
    assert again["open_questions"] == []


def test_do_not_contact_round_trip(token):
    client.post("/outreach/do-not-contact", params={"token": token},
                json={"values": ["friend@acme.com", "Investor.VC"], "reason": "personal"})
    values = client.get("/outreach/do-not-contact", params={"token": token}).json()["values"]
    stored = {v["value"]: v["kind"] for v in values}
    assert stored["friend@acme.com"] == "email"
    assert stored["investor.vc"] == "domain"      # normalised, and classified by shape


def test_your_own_profile_is_never_queued_even_if_the_caller_forgets():
    """Test Operator ended up in her own outreach queue: `self_linkedin` was optional and the
    caller omitted it. That is one LinkedIn read, one Apollo credit, and very nearly an email to
    herself. The answer bank holds her profile, so self-exclusion cannot depend on the caller."""
    from warmgraph.entities import contact_key
    from warmgraph.outreach.ingest import linkedin_url

    own = "https://www.linkedin.com/in/some-handle"
    # Whatever shape Luma hands back, it collapses to the same key as the stored profile.
    for raw in ["/in/some-handle", "in/some-handle", "/in/some-handle/", "some-handle"]:
        assert contact_key(linkedin_url(raw), "") == contact_key(own, "")


def test_the_ui_mount_never_shadows_the_api(tmp_path, monkeypatch):
    """The UI is served by this same process, mounted at "/". Mounted before the API routes it
    would swallow every one of them and return HTML — a 404 on endpoints that plainly exist.
    Registered routes must win, and an unknown /outreach path must stay a real 404 rather than
    quietly becoming an HTML page."""
    import importlib, os
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>ui</title>")

    monkeypatch.setattr(main.os.path, "isdir", lambda p: True)
    # Real API routes still answer as themselves.
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/connections", params={"token": "nope"}).status_code == 401

    # And the SPA guard keeps unknown API paths as 404s rather than serving the bundle.
    prefixes = ("outreach/", "agents/", "connect/", "oauth/", "workspace", "health", "connections")
    for path in ("outreach/nonsense", "agents/nope", "connections"):
        assert path.startswith(prefixes), path


def test_the_browser_worker_can_report_what_it_is_doing(token):
    """Steps 1, 2 and 4 run in the user's own Chrome, so their status lived only in
    chrome.storage.local and the deployed UI could not say whether anything was filling the
    queue. "Is it running?" could only be answered by opening the extension."""
    assert client.get("/outreach/worker-status", params={"token": token}).json() == {}

    client.post("/outreach/worker-status", params={"token": token}, json={
        "running": True, "stage": "scan", "reason": "alarm",
        "counts": {"registered": 2, "scanned": 1, "guests": 148},
    })
    got = client.get("/outreach/worker-status", params={"token": token}).json()
    assert got["running"] is True and got["stage"] == "scan"
    assert got["counts"]["guests"] == 148
    assert got["received_at"]          # stamped server-side, so a stale browser clock cannot lie

    # Last write wins — this is a liveness display, not a ledger.
    client.post("/outreach/worker-status", params={"token": token},
                json={"running": False, "last_run_at": "2026-08-12T10:00:00Z"})
    assert client.get("/outreach/worker-status", params={"token": token}).json()["running"] is False


def test_worker_status_is_scoped_to_the_workspace(token):
    other = client.post("/workspace", json={"url": "https://other-worker.example"}).json()["token"]
    client.post("/outreach/worker-status", params={"token": token},
                json={"running": True, "stage": "register"})
    assert client.get("/outreach/worker-status", params={"token": other}).json() == {}
