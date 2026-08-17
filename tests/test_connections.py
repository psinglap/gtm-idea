"""Offline tests for the connections layer — encryption, workspace tenancy, and the status the
front panel renders. No network: nothing here talks to Google or Apollo."""
from __future__ import annotations

import tempfile
from datetime import timedelta

import pytest
from cryptography.fernet import Fernet, InvalidToken

import warmgraph.connections as C
from warmgraph.connections import crypto, google
from warmgraph.entities import Connection
from warmgraph.models import utcnow
from warmgraph.storage.sqlite_store import SqliteStore


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv("WG_SECRET_KEY", k)
    return k


# --------------------------------------------------------------------------- #
# crypto                                                                        #
# --------------------------------------------------------------------------- #
def test_round_trip(key):
    assert crypto.decrypt(crypto.encrypt("refresh-token-123")) == "refresh-token-123"


def test_ciphertext_does_not_contain_the_plaintext(key):
    assert "refresh-token-123" not in crypto.encrypt("refresh-token-123")


def test_empty_stays_empty(key):
    """Session providers (Luma/LinkedIn) hold no credential at all."""
    assert crypto.encrypt("") == ""
    assert crypto.decrypt("") == ""


def test_raises_when_no_key_exists_and_none_can_be_created(monkeypatch):
    """With no env key AND no working store to generate one, the failure must be loud rather
    than silently protecting secrets with something predictable."""
    monkeypatch.delenv("WG_SECRET_KEY", raising=False)
    crypto.bootstrap(lambda: "")          # store unreachable
    try:
        assert crypto.is_configured() is False
        with pytest.raises(crypto.SecretKeyMissing):
            crypto.encrypt("something")
    finally:
        crypto.bootstrap(None)


def test_a_key_is_generated_on_demand_so_there_is_no_setup_step(monkeypatch):
    """Connecting Gmail should be Sign in with Google, not 'first generate a Fernet key'."""
    monkeypatch.delenv("WG_SECRET_KEY", raising=False)
    generated = crypto.generate_key()
    crypto.bootstrap(lambda: generated)
    try:
        assert crypto.is_configured() is True
        assert crypto.decrypt(crypto.encrypt("refresh-abc")) == "refresh-abc"
    finally:
        crypto.bootstrap(None)


def test_env_key_wins_over_the_generated_one(monkeypatch):
    """The stronger option stays available: an env key keeps the secret out of the database."""
    env_key = Fernet.generate_key().decode()
    monkeypatch.setenv("WG_SECRET_KEY", env_key)
    crypto.bootstrap(lambda: Fernet.generate_key().decode())
    try:
        ciphertext = crypto.encrypt("token")
        assert Fernet(env_key.encode()).decrypt(ciphertext.encode()).decode() == "token"
    finally:
        crypto.bootstrap(None)


def test_rejects_a_malformed_key(monkeypatch):
    monkeypatch.setenv("WG_SECRET_KEY", "not-a-fernet-key")
    with pytest.raises(crypto.SecretKeyMissing):
        crypto.encrypt("x")


def test_key_rotation_old_ciphertext_still_decrypts(monkeypatch):
    old = Fernet.generate_key().decode()
    monkeypatch.setenv("WG_SECRET_KEY", old)
    ciphertext = crypto.encrypt("apollo-key")

    new = Fernet.generate_key().decode()
    monkeypatch.setenv("WG_SECRET_KEY", f"{new},{old}")     # new first = new writes
    assert crypto.decrypt(ciphertext) == "apollo-key"
    assert crypto.decrypt(crypto.encrypt("fresh")) == "fresh"

    monkeypatch.setenv("WG_SECRET_KEY", new)                # old key retired
    with pytest.raises(InvalidToken):
        crypto.decrypt(ciphertext)


def test_try_decrypt_degrades_instead_of_raising(monkeypatch):
    """Read paths must not blow up a whole cron run over one unreadable row."""
    monkeypatch.setenv("WG_SECRET_KEY", Fernet.generate_key().decode())
    assert crypto.try_decrypt("garbage") == ""


# --------------------------------------------------------------------------- #
# workspace tenancy (no sign-in)                                                #
# --------------------------------------------------------------------------- #
def test_workspace_token_is_stable_and_resolves(store):
    client, token = C.ensure_workspace(store, "example.com")
    again, token2 = C.ensure_workspace(store, "example.com")
    assert again.id == client.id
    assert token2 == token                     # regenerating would lock the user out
    assert C.company_id_for_token(store, token) == client.id


def test_unknown_token_resolves_to_nothing(store):
    C.ensure_workspace(store, "example.com")
    assert C.company_id_for_token(store, "not-a-real-token") is None
    assert C.company_id_for_token(store, "") is None


def test_workspaces_are_isolated(store):
    a, ta = C.ensure_workspace(store, "example.com")
    b, tb = C.ensure_workspace(store, "other.com")
    assert ta != tb
    assert C.company_id_for_token(store, ta) == a.id
    assert C.company_id_for_token(store, tb) == b.id


# --------------------------------------------------------------------------- #
# status panel                                                                  #
# --------------------------------------------------------------------------- #
def test_status_always_lists_every_provider(store):
    rows = C.connection_status(store, "comp-1")
    assert [r["provider"] for r in rows] == list(C.PROVIDERS)
    assert all(r["status"] == "disconnected" for r in rows)
    assert all(r["hint"] for r in rows)


def test_status_never_leaks_a_secret(store, key):
    store.upsert_connection(Connection(company_id="comp-1", provider="apollo",
                                       status="connected", secret=crypto.encrypt("sk-live")))
    row = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "apollo")
    assert "secret" not in row
    assert row["has_secret"] is True


def test_session_provider_goes_stale_without_a_heartbeat(store):
    """Luma/LinkedIn are only 'connected' while the browser is actually reporting in."""
    C.session_ping(store, "comp-1", "luma", account_label="operator")
    fresh = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "luma")
    assert fresh["status"] == "connected"
    assert fresh["kind"] == "session"

    conn = store.get_connection("comp-1", "luma")
    conn.last_ok_at = utcnow() - timedelta(minutes=C.SESSION_TTL_MINUTES + 5)
    store.upsert_connection(conn)
    stale = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "luma")
    assert stale["status"] == "stale"


def test_readiness_reports_what_the_pipeline_can_do(store, key):
    r = C.readiness(store, "comp-1")
    assert r == {"can_scan_events": False, "can_read_linkedin": False, "can_find_emails": False,
                 "can_deliver": False, "unattended_blocked_by": [], "history_mailboxes": [],
                 "secrets_configured": True,
                 "google_configured": google.config().configured}

    C.session_ping(store, "comp-1", "luma")
    assert C.readiness(store, "comp-1")["can_scan_events"] is True


def test_linking_apollo_via_claude_stores_no_credential(store):
    """The connector holds the auth. We record that it works, not a secret we don't have."""
    conn = C.link_via_claude(store, "comp-1", "apollo", "founders@example.com")
    assert conn.secret == ""
    row = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "apollo")
    assert row["status"] == "connected"
    assert row["account_label"] == "founders@example.com"
    assert row["via_claude"] is True
    assert row["has_secret"] is False


def test_a_claude_linked_provider_is_flagged_as_not_autonomous(store):
    """Honest readiness: enrichment works today, but a server cron cannot reach the connector."""
    C.link_via_claude(store, "comp-1", "apollo", "founders@example.com")
    r = C.readiness(store, "comp-1")
    assert r["can_find_emails"] is True
    assert r["unattended_blocked_by"] == ["apollo"]


def test_pasting_a_key_later_replaces_the_claude_link(store, key):
    """Adding the API key upgrades the same connection to one a cron can use."""
    C.link_via_claude(store, "comp-1", "apollo", "founders@example.com")
    conn = store.get_connection("comp-1", "apollo")
    conn.secret = crypto.encrypt("sk-live")
    conn.scopes = []
    store.upsert_connection(conn)
    assert C.readiness(store, "comp-1")["unattended_blocked_by"] == []


def test_a_stale_grant_is_flagged_for_reconnect_not_shown_green(store, key):
    """The exact failure seen live: Gmail authorised before the history scope existed. It looked
    connected, then every send failed a 403 nobody could see."""
    store.upsert_connection(Connection(
        company_id="comp-1", provider="gmail", status="connected",
        account_label="operator@example.com", secret=crypto.encrypt("refresh"),
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.compose"]))
    row = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "gmail")
    assert row["needs_reconnect"] is True
    assert row["status"] == "needs_reconnect"          # NOT "connected"
    assert "gmail.readonly" in row["missing_scopes"][0]


def test_a_complete_grant_is_not_nagged(store, key):
    store.upsert_connection(Connection(
        company_id="comp-1", provider="gmail", status="connected",
        secret=crypto.encrypt("refresh"),
        scopes=["openid", "email",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.readonly"]))
    row = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "gmail")
    assert row["needs_reconnect"] is False
    assert row["status"] == "connected"


def test_every_gmail_mailbox_is_searchable_sender_first(store, key):
    """Sending uses one account; suppression must union across all of them."""
    store.upsert_connection(Connection(
        company_id="comp-1", provider="gmail_history", status="connected",
        account_label="founders@example.com", secret=crypto.encrypt("r2"),
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.readonly"]))
    store.upsert_connection(Connection(
        company_id="comp-1", provider="gmail", status="connected",
        account_label="operator@example.com", secret=crypto.encrypt("r1"),
        scopes=["openid", "email", "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/gmail.readonly"]))
    boxes = C.gmail_mailboxes(store, "comp-1")
    assert [c.account_label for c in boxes] == ["operator@example.com", "founders@example.com"]
    assert C.readiness(store, "comp-1")["history_mailboxes"] == [
        "operator@example.com", "founders@example.com"]


def test_mark_error_outranks_a_fresh_heartbeat(store):
    """LinkedIn started gating us and the worker paused. A ping from a minute ago must not
    paint that green — the reason the queue stopped has to stay visible."""
    C.session_ping(store, "comp-1", "linkedin")
    C.mark_error(store, "comp-1", "linkedin", "5 consecutive gated reads, paused for the day")
    row = next(r for r in C.connection_status(store, "comp-1") if r["provider"] == "linkedin")
    assert row["status"] == "error"
    assert "gated" in store.get_connection("comp-1", "linkedin").last_error


def test_secret_for_returns_empty_when_disconnected(store, key):
    assert C.secret_for(store, "comp-1", "gmail") == ""
    store.upsert_connection(Connection(company_id="comp-1", provider="gmail",
                                       secret=crypto.encrypt("refresh-abc")))
    assert C.secret_for(store, "comp-1", "gmail") == "refresh-abc"


# --------------------------------------------------------------------------- #
# Gmail message building (no network)                                           #
# --------------------------------------------------------------------------- #
def test_mime_is_plain_text_and_base64url():
    import base64
    raw = google.build_mime("jane@acme.com", "Wanted to say hello at Frontier Signals",
                            "Hi Jane,\n\nShort note.\n", from_name="Sam")
    decoded = base64.urlsafe_b64decode(raw).decode()
    assert "To: jane@acme.com" in decoded
    assert "Subject: Wanted to say hello at Frontier Signals" in decoded
    assert "text/plain" in decoded          # never HTML: no tracking pixels, reads like a person
    assert "Short note." in decoded


def test_auth_url_requests_offline_access(monkeypatch):
    """Without access_type=offline + prompt=consent Google returns no refresh token and the
    nightly cron dies after an hour."""
    monkeypatch.setenv("WG_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("WG_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("WG_GOOGLE_REDIRECT_URI", "http://localhost:8000/oauth/google/callback")
    url = google.auth_url(state="tok123")
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "gmail.compose" in url           # covers drafts AND sending on its own
    assert "gmail.send" not in url          # redundant with compose
    assert "gmail.readonly" in url          # needed to skip people you already know
    assert "state=tok123" in url


def test_history_scope_can_be_turned_off_for_public_distribution(monkeypatch):
    """gmail.readonly is RESTRICTED. Anyone shipping this publicly can drop it and fall back to
    the ledger, trading suppression quality for a much lighter Google review."""
    monkeypatch.setenv("WG_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("WG_GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("WG_GMAIL_READ_HISTORY", "0")
    assert google.read_history_enabled() is False
    assert "gmail.readonly" not in google.auth_url(state="x")


def test_auth_url_fails_loudly_when_unconfigured(monkeypatch):
    for var in ("WG_GOOGLE_CLIENT_ID", "WG_GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(google.GoogleNotConfigured):
        google.auth_url(state="x")


def test_ensure_workspace_never_erases_an_existing_workspace(tmp_path):
    """A workspace with 26 saved answers came back holding nothing but a fresh token.
    `upsert_company` replaces `data` wholesale, so a fresh Client for an existing domain wipes
    the answer bank, template, open questions and filter cache."""
    from warmgraph.connections import ensure_workspace
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "t.db"))
    client, token = ensure_workspace(store, "example.com")

    client = store.get_company_by_id(client.id)
    client.data = {**(client.data or {}), "outreach_answers": {"company": "Acme"}}
    store.upsert_company(client)

    again, token2 = ensure_workspace(store, "example.com")
    assert token2 == token, "the token must be stable, or every live link breaks"
    assert (again.data or {}).get("outreach_answers") == {"company": "Acme"}


def test_a_single_missed_read_does_not_clobber(tmp_path):
    """The specific failure: one transient None from get_company was enough to destroy the row."""
    from warmgraph import connections
    from warmgraph.storage.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "t.db"))
    client, token = connections.ensure_workspace(store, "example.com")
    client = store.get_company_by_id(client.id)
    client.data = {**(client.data or {}), "outreach_answers": {"role": "Founder"}}
    store.upsert_company(client)

    real, calls = store.get_company, {"n": 0}

    def flaky(dom):                      # first read fails, as under a cold connection pool
        calls["n"] += 1
        return None if calls["n"] == 1 else real(dom)

    store.get_company = flaky
    again, token2 = connections.ensure_workspace(store, "example.com")
    store.get_company = real

    assert token2 == token
    assert (again.data or {}).get("outreach_answers") == {"role": "Founder"}


def test_apollo_is_matched_on_the_linkedin_url_alone(monkeypatch):
    """A name is not an identity. If the URL misses and the name hits, Apollo returns someone
    ELSE of the same name, and their title, employer and email get written onto this contact
    and eventually emailed. Verified live: a name-only lookup for someone Apollo does not hold
    returns a hollow invented record, so the name buys nothing on a miss either."""
    import httpx
    from warmgraph.connections import apollo

    sent = {}

    class _Resp:
        status_code = 200
        def json(self): return {"person": {"name": "x"}}

    def fake_post(url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    apollo.match_by_linkedin("k", "https://www.linkedin.com/in/some-handle", name="Test Operator")
    assert sent == {"linkedin_url": "https://www.linkedin.com/in/some-handle"}
    assert "name" not in sent
