"""Connected accounts for the event-outreach pipeline.

Four providers, two very different kinds:

  • gmail, apollo  — a real credential (OAuth refresh token / API key), Fernet-encrypted in
                     `connections.secret`.
  • luma, linkedin — NO credential is ever stored. Both are read through the user's own
                     logged-in browser, so "connected" simply means the extension reported a
                     live session recently. This is not a shortcut: Luma has no third-party
                     OAuth at all, and LinkedIn's OAuth cannot read other people's profiles.

There is no sign-in and no users table. Tenancy comes from a random per-install workspace
token that maps to a `company_id`, the same pattern as `luma-icp-scout/server/app.py`.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import List, Optional, Tuple

from warmgraph.connections import apollo, crypto, google
from warmgraph.entities import Client, Connection
from warmgraph.models import utcnow

PROVIDERS = ("luma", "linkedin", "apollo", "gmail", "gmail_history")
SESSION_PROVIDERS = ("luma", "linkedin")   # browser-session based, no stored credential

# Only `gmail` ever sends. Every gmail* connection is SEARCHED for prior conversations, because
# the mailbox you send from is often not the mailbox your history lives in — and suppression
# that looks in the wrong place silently reports "no history" for people you know well.
SENDER_PROVIDER = "gmail"
HISTORY_PROVIDER_PREFIX = "gmail"


def provider_role(provider: str) -> str:
    return "send" if provider == SENDER_PROVIDER else "history"

# How long an extension heartbeat keeps luma/linkedin looking "connected". Longer than the
# extension's 15-minute ping so a laptop lid closed over lunch doesn't flip the chip red.
SESSION_TTL_MINUTES = 45


# --------------------------------------------------------------------------- #
# Workspace (tenancy without accounts)                                          #
# --------------------------------------------------------------------------- #
def new_workspace_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_workspace(store, domain: str) -> Tuple[Client, str]:
    """Get-or-create the client row for a domain and make sure it has a workspace token.
    Returns (client, token). The token is the bearer credential for this workspace.

    Reads the existing client FIRST: `upsert_company` overwrites `data` wholesale, so building
    a fresh Client here would wipe the stored token and mint a new one on every call — which
    would silently invalidate the user's link each time anything touched the workspace.
    """
    dom = (domain or "").lower()
    client = store.get_company(dom)
    if client is None:
        # Believing a single "not found" here is destructive, not merely wrong. `upsert_company`
        # matches on domain and replaces `data` WHOLESALE, so constructing a fresh Client for a
        # domain that already exists erases that workspace's answer bank, email template, open
        # questions and event-filter cache in one statement. This happened: a workspace with 26
        # saved answers came back holding nothing but a freshly minted token.
        #
        # A second read costs one query on the only path that can do that damage.
        client = store.get_company(dom)
    if client is None:
        client = store.upsert_company(Client(domain=dom, url=domain))

    token = (client.data or {}).get("workspace_token", "")
    if not token:
        token = new_workspace_token()
        client.data = {**(client.data or {}), "workspace_token": token}
        store.upsert_company(client)
    return client, token


def company_id_for_token(store, token: str) -> Optional[str]:
    """Resolve a workspace token to its company_id. Returns None for an unknown token.

    Indexed lookup, not a scan: this runs on every single extension request (queue polls,
    heartbeats, every profile checkpoint), so an O(all clients) scan here would be the first
    thing to fall over as workspaces are added.
    """
    if not token:
        return None
    client = store.get_company_by_token(token)
    return client.id if client else None


# --------------------------------------------------------------------------- #
# Status (what the front panel renders)                                         #
# --------------------------------------------------------------------------- #
def _is_live_session(conn: Connection) -> bool:
    if not conn.last_ok_at:
        return False
    return (utcnow() - conn.last_ok_at) < timedelta(minutes=SESSION_TTL_MINUTES)


def connection_status(store, company_id: str) -> List[dict]:
    """All four providers, always, so the UI can render a stable row of chips. Never includes
    the secret — `Connection.redacted()` strips it."""
    by_provider = {c.provider: c for c in store.list_connections(company_id)}
    out: List[dict] = []
    for provider in PROVIDERS:
        conn = by_provider.get(provider)
        if conn is None:
            out.append({
                "provider": provider, "status": "disconnected", "account_label": "",
                "has_secret": False, "via_claude": False,
                "kind": "session" if provider in SESSION_PROVIDERS else "credential",
                "hint": _hint(provider),
            })
            continue
        d = conn.redacted()
        d["via_claude"] = linked_via_claude(conn)
        if provider.startswith(HISTORY_PROVIDER_PREFIX) and conn.secret:
            # A grant made before a scope existed keeps working for everything except the new
            # capability. Surface that here rather than letting it fail mid-send.
            missing = google.missing_scopes(conn, provider_role(provider))
            d["missing_scopes"] = missing
            d["needs_reconnect"] = bool(missing)
            if missing:
                d["status"] = "needs_reconnect"
        if provider in SESSION_PROVIDERS and conn.status != "error":
            # A stale heartbeat means Chrome is closed or the user logged out — report it
            # honestly rather than showing green off a week-old ping. An explicit error (e.g.
            # LinkedIn started gating us) outranks freshness: a recent successful ping must
            # not paint over the reason the worker stopped.
            d["status"] = "connected" if _is_live_session(conn) else "stale"
        d["kind"] = "session" if provider in SESSION_PROVIDERS else "credential"
        d["hint"] = _hint(provider)
        out.append(d)
    return out


def _hint(provider: str) -> str:
    return {
        "luma": "Log in to Luma in Chrome. No credential is stored.",
        "linkedin": "Log in to LinkedIn in Chrome. Profiles are read only from your own session.",
        "apollo": "Paste your Apollo API key, or use the Apollo account connected in Claude.",
        "gmail": "The account outreach is SENT from. Also searched for prior conversations.",
        "gmail_history": ("An extra mailbox searched only for prior conversations — never sends. "
                          "Connect the account where your existing threads actually live."),
    }.get(provider, "")


def gmail_mailboxes(store, company_id: str) -> List[Connection]:
    """Every Gmail account we can search for prior conversations, sender first.

    Suppression unions across all of them: a person you emailed from ANY connected mailbox is
    someone you already know.
    """
    out = [c for c in store.list_connections(company_id)
           if c.provider.startswith(HISTORY_PROVIDER_PREFIX) and c.secret
           and c.status == "connected"]
    out.sort(key=lambda c: 0 if c.provider == SENDER_PROVIDER else 1)
    return out


def readiness(store, company_id: str) -> dict:
    """What the pipeline can and cannot do right now — used to explain a stalled queue."""
    rows = connection_status(store, company_id)
    status = {c["provider"]: c["status"] for c in rows}
    via_claude = {c["provider"] for c in rows if c.get("via_claude")}
    return {
        "can_scan_events": status.get("luma") == "connected",
        "can_read_linkedin": status.get("linkedin") == "connected",
        "can_find_emails": status.get("apollo") == "connected",
        "can_deliver": status.get("gmail") == "connected",
        "history_mailboxes": [c.account_label or c.provider
                              for c in gmail_mailboxes(store, company_id)],
        # Anything linked through the Claude connector only works while a Claude run is
        # driving — a cron on a server cannot reach it. Say so rather than implying autonomy.
        "unattended_blocked_by": sorted(via_claude),
        "secrets_configured": crypto.is_configured(),
        "google_configured": google.config().configured,
    }


# --------------------------------------------------------------------------- #
# Mutations                                                                     #
# --------------------------------------------------------------------------- #
def session_ping(store, company_id: str, provider: str, account_label: str = "") -> Connection:
    """Extension heartbeat for luma/linkedin: 'I have a live logged-in session'. Stores no
    credential — only the fact and the time."""
    conn = store.get_connection(company_id, provider) or Connection(
        company_id=company_id, provider=provider)
    conn.status = "connected"
    conn.last_ok_at = utcnow()
    conn.last_error = ""
    if account_label:
        conn.account_label = account_label
    return store.upsert_connection(conn)


def connect_gmail(store, company_id: str, code: str) -> Connection:
    return store.upsert_connection(google.connect(company_id, code))


def connect_apollo(store, company_id: str, api_key: str) -> Connection:
    return store.upsert_connection(apollo.connect(company_id, api_key))


def link_via_claude(store, company_id: str, provider: str, account_label: str = "",
                    note: str = "") -> Connection:
    """Mark a provider as usable through the Claude connector rather than a stored credential.

    Apollo and Gmail can both be reached two ways: a credential we hold (works unattended, on a
    server), or the connector already authenticated in the user's Claude session (works only
    while a Claude run is driving). This records the second case honestly — nothing is stored,
    and `readiness` still reports that unattended runs need a key.
    """
    conn = store.get_connection(company_id, provider) or Connection(
        company_id=company_id, provider=provider)
    conn.status = "connected"
    # The stored key is KEPT. This used to blank it, on the reasoning that the connector holds the
    # auth so a key is redundant — but that makes a single call to this endpoint destroy a working
    # credential with nothing to restore it from. It happened to Apollo, and enrichment then did
    # nothing at all for every run afterwards. Being linked via Claude is recorded in `scopes`,
    # which is what linked_via_claude now reads, so the key can survive without changing meaning.
    conn.account_label = account_label or conn.account_label
    conn.last_ok_at = utcnow()
    conn.last_error = ""
    conn.scopes = ["via-claude"]
    return store.upsert_connection(conn)


def linked_via_claude(conn: Optional[Connection]) -> bool:
    """Authorised through the Claude connector. Read from `scopes`, not from the ABSENCE of a
    key — inferring it from emptiness is what made blanking the key look necessary."""
    return bool(conn and conn.status == "connected"
                and "via-claude" in (conn.scopes or []))


def secret_for(store, company_id: str, provider: str) -> str:
    """Decrypted credential, or "" when absent/unreadable. Callers treat "" as disconnected."""
    conn = store.get_connection(company_id, provider)
    return crypto.try_decrypt(conn.secret) if conn else ""


def mark_error(store, company_id: str, provider: str, message: str) -> None:
    """Record a provider failure so the UI can show a red chip with the real reason instead of
    the pipeline silently doing nothing."""
    conn = store.get_connection(company_id, provider)
    if conn is None:
        return
    conn.status = "error"
    conn.last_error = (message or "")[:300]
    store.upsert_connection(conn)
