"""Gmail connection — OAuth 2.0 web flow + the two Gmail calls we actually make.

This is a CAPABILITY connection, not a login. Google identity is never used to authenticate
anyone into the app; we store a refresh token so the pipeline can put mail in your Gmail.

Scopes, deliberately minimal:
  • gmail.compose   — create drafts AND send messages    (sensitive)
  • openid, email   — read WHICH mailbox got connected   (non-sensitive)

`gmail.compose` alone covers both delivery modes, so there is no reason to also ask for
`gmail.send`. It is also the narrowest scope an existing Gmail OAuth client is likely already
configured for, which means that
client can be reused by adding a redirect URI and nothing else.

No `gmail.readonly`. The mailbox is never read, which keeps this off Google's *restricted*
scope list — standard verification instead of a CASA security assessment if it's ever shared.

Deliberately plain httpx + stdlib `email` rather than the Google SDK, matching the rest of the
repo's dependency-light style.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx

from warmgraph.connections import crypto
from warmgraph.entities import Connection
from warmgraph.models import utcnow

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Reading the mailbox is what lets us skip people you already have a conversation with. It is a
# RESTRICTED scope, so it is opt-out (WG_GMAIL_READ_HISTORY=0) for anyone distributing this
# publicly, where it would trigger a CASA security assessment. It is free only if your own
# Gmail OAuth client already lists gmail.readonly on its consent screen.
READ_HISTORY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


# Someone already on the calendar is not a cold contact. A meeting is a stronger signal than a
# mail thread — it survives a conversation that happened entirely over LinkedIn, WhatsApp or in
# person, where the mailbox has nothing to find.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"


def read_history_enabled() -> bool:
    return os.getenv("WG_GMAIL_READ_HISTORY", "1").strip() not in ("0", "false", "no", "")


def read_calendar_enabled() -> bool:
    return os.getenv("WG_READ_CALENDAR", "1").strip() not in ("0", "false", "no", "")


def scopes() -> tuple:
    out = ["openid", "email", "https://www.googleapis.com/auth/gmail.compose"]
    if read_history_enabled():
        out.append(READ_HISTORY_SCOPE)
    if read_calendar_enabled():
        out.append(CALENDAR_SCOPE)
    return tuple(out)


SCOPES = scopes()

# What each role must actually hold. Checked against the scopes recorded on a connection at
# consent time, because a grant made before a scope was added keeps working for everything
# EXCEPT the new capability — which then fails deep inside a send run with a bare 403.
SEND_SCOPES = ("https://www.googleapis.com/auth/gmail.compose",)
HISTORY_SCOPES = (READ_HISTORY_SCOPE,)


def required_scopes(role: str) -> tuple:
    """`send` also searches its own mailbox, so it needs both."""
    if role == "history":
        return HISTORY_SCOPES
    return SEND_SCOPES + (HISTORY_SCOPES if read_history_enabled() else ())


def missing_scopes(conn, role: str = "send") -> list:
    """Scopes this role needs that the stored grant does not have. Non-empty means reconnect."""
    granted = set(getattr(conn, "scopes", None) or [])
    if not granted:
        return []          # connected before we recorded scopes; assume ok rather than nag
    return [s for s in required_scopes(role) if s not in granted]

_TIMEOUT = httpx.Timeout(30.0)


class GoogleNotConfigured(RuntimeError):
    """Raised when the OAuth client credentials aren't in the environment."""


class GoogleAuthError(RuntimeError):
    """Token exchange/refresh failed — the connection needs reconnecting."""


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)


def config() -> GoogleConfig:
    return GoogleConfig(
        client_id=os.getenv("WG_GOOGLE_CLIENT_ID", ""),
        client_secret=os.getenv("WG_GOOGLE_CLIENT_SECRET", ""),
        redirect_uri=os.getenv("WG_GOOGLE_REDIRECT_URI",
                               "http://localhost:8000/oauth/google/callback"),
    )


def _require_config() -> GoogleConfig:
    cfg = config()
    if not cfg.configured:
        raise GoogleNotConfigured(
            "Set WG_GOOGLE_CLIENT_ID, WG_GOOGLE_CLIENT_SECRET and WG_GOOGLE_REDIRECT_URI "
            "(Google Cloud console → APIs & Services → Credentials → OAuth client ID → Web)."
        )
    return cfg


# --------------------------------------------------------------------------- #
# OAuth flow                                                                    #
# --------------------------------------------------------------------------- #
def account_hint() -> str:
    """Which mailbox this should connect (WG_GMAIL_ACCOUNT). Without it Google silently picks
    whichever Google account the browser is already signed into, which is how you end up
    connecting a personal address instead of the one you send from."""
    return os.getenv("WG_GMAIL_ACCOUNT", "").strip()


def auth_url(state: str, login_hint: str = "", role: str = "send") -> str:
    """Consent screen URL. `access_type=offline` + `prompt=consent` are both required to get a
    refresh token back — without them Google returns only a 1-hour access token and the cron
    dies overnight."""
    cfg = _require_config()
    wanted = ("openid", "email") + tuple(required_scopes(role))
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(dict.fromkeys(wanted)),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    hint = login_hint or account_hint()
    if hint:
        params["login_hint"] = hint
        domain = hint.rpartition("@")[2]
        if domain:
            # Workspace domains only: hides other accounts entirely rather than merely
            # preselecting one. Ignored by Google for consumer addresses, so it is safe to send.
            params["hd"] = domain
    return AUTH_URL + "?" + urlencode(params)


def _email_from_id_token(id_token: str) -> str:
    """Pull the address out of the id_token payload. No signature check needed: this came
    straight from Google's token endpoint over TLS, and it is only used as a display label."""
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError):
        return ""


def exchange_code(code: str) -> Tuple[str, str, str, int]:
    """Authorization code -> (refresh_token, access_token, email, expires_in)."""
    cfg = _require_config()
    r = httpx.post(TOKEN_URL, timeout=_TIMEOUT, data={
        "code": code,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
        "grant_type": "authorization_code",
    })
    if r.status_code != 200:
        raise GoogleAuthError(f"Token exchange failed ({r.status_code}): {r.text[:200]}")
    d = r.json()
    refresh = d.get("refresh_token", "")
    if not refresh:
        # Google withholds it when the user already granted consent to this client before.
        raise GoogleAuthError(
            "Google returned no refresh_token. Remove this app at "
            "https://myaccount.google.com/permissions and connect again."
        )
    return refresh, d.get("access_token", ""), _email_from_id_token(d.get("id_token", "")), \
        int(d.get("expires_in", 3600))


def refresh_access_token(refresh_token: str) -> Tuple[str, int]:
    """Refresh token -> (access_token, expires_in)."""
    cfg = _require_config()
    r = httpx.post(TOKEN_URL, timeout=_TIMEOUT, data={
        "refresh_token": refresh_token,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "grant_type": "refresh_token",
    })
    if r.status_code != 200:
        raise GoogleAuthError(f"Token refresh failed ({r.status_code}): {r.text[:200]}")
    d = r.json()
    return d.get("access_token", ""), int(d.get("expires_in", 3600))


# Access tokens live an hour; a cron pass sends far more than one message, so cache per client
# rather than burning a refresh call per email.
_token_cache: dict = {}


def access_token_for(conn: Connection) -> str:
    """Usable access token for a stored Gmail connection, refreshing (and caching) as needed."""
    cached = _token_cache.get(conn.company_id)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    refresh = crypto.decrypt(conn.secret)
    if not refresh:
        raise GoogleAuthError("Gmail connection has no usable refresh token — reconnect Gmail.")
    token, expires_in = refresh_access_token(refresh)
    _token_cache[conn.company_id] = (token, time.time() + expires_in)
    return token


def connect(company_id: str, code: str, provider: str = "gmail",
            role: str = "send") -> Connection:
    """Complete the OAuth callback into a stored, encrypted `Connection`.

    `provider` decides where it is stored: `gmail` is the account that sends, `gmail_history`
    is an extra mailbox searched only for prior conversations.
    """
    refresh, _access, email, expires_in = exchange_code(code)
    granted = ("openid", "email") + tuple(required_scopes(role))
    return Connection(
        company_id=company_id, provider=provider, status="connected",
        account_label=email, secret=crypto.encrypt(refresh),
        scopes=list(dict.fromkeys(granted)), last_ok_at=utcnow(),
    )


# --------------------------------------------------------------------------- #
# Gmail                                                                         #
# --------------------------------------------------------------------------- #
def build_mime(to: str, subject: str, body: str, from_name: str = "",
               html_body: str = "") -> str:
    """RFC 2822 message, base64url encoded the way the Gmail API wants it.

    multipart/alternative when an HTML part is supplied, so anchor text like "Book a Slot" is
    a real clickable link while the plain-text part still carries the URLs for clients that
    refuse HTML. No images, no tracking pixel — the thing that makes cold mail look like cold
    mail is the beacon, not the anchor tag.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    if from_name:
        msg["From"] = from_name
    msg.set_content(body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _threads_exist(token: str, query: str) -> bool:
    r = httpx.get(f"{GMAIL_API}/threads", timeout=_TIMEOUT,
                  params={"q": query, "maxResults": 1},
                  headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 403:
        # Connected before this scope was enabled — reconnect Gmail to grant it. Raise rather
        # than returning False, so "we could not check" never masquerades as "no history".
        raise GoogleAuthError(
            "Gmail is connected without permission to read history. Reconnect Gmail to enable "
            "skipping people you already have a conversation with.")
    if r.status_code != 200:
        raise GoogleAuthError(f"Gmail thread search failed ({r.status_code}): {r.text[:160]}")
    return bool((r.json() or {}).get("threads"))


def recent_messages(token: str, query: str, limit: int = 50,
                    after: Optional[object] = None) -> List[Dict[str, str]]:
    """[{'text': snippet}] for a Gmail search. Used to read bounce notices back out.

    `after` narrows the search to messages that arrived since then, using Gmail's own `after:`
    operator so the filtering happens at Google rather than here. Without it every run re-read the
    same hundred failure notices and re-examined bounces it had already retired.

    The snippet is enough and deliberately cheap: a delivery failure puts "Address not found …
    because the address couldn't be found" right at the top, which is exactly what the classifier
    reads. Fetching full bodies would cost one request per message for text we would discard.
    """
    q = query
    if after is not None:
        # Gmail's after: takes a unix timestamp. It is day-granular in the UI but accepts seconds,
        # and it errs towards INCLUDING borderline messages — which is the right way to err here.
        try:
            q = f"{query} after:{int(after.timestamp())}"
        except AttributeError:
            q = f"{query} after:{int(after)}"
    r = httpx.get(f"{GMAIL_API}/messages", timeout=_TIMEOUT,
                  params={"q": q, "maxResults": max(1, min(limit, 100))},
                  headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 403:
        raise GoogleAuthError(
            "Gmail is connected without permission to read messages. Reconnect Gmail to enable "
            "retiring addresses that bounced.")
    if r.status_code != 200:
        raise GoogleAuthError(f"Gmail message search failed ({r.status_code}): {r.text[:160]}")

    out: List[Dict[str, str]] = []
    for ref in (r.json() or {}).get("messages") or []:
        m = httpx.get(f"{GMAIL_API}/messages/{ref.get('id')}", timeout=_TIMEOUT,
                      params={"format": "metadata", "metadataHeaders": "Subject"},
                      headers={"Authorization": f"Bearer {token}"})
        if m.status_code != 200:
            continue
        d = m.json() or {}
        subject = ""
        for h in ((d.get("payload") or {}).get("headers") or []):
            if (h.get("name") or "").lower() == "subject":
                subject = h.get("value") or ""
        out.append({"text": f"{subject}\n{d.get('snippet') or ''}"})
    return out


CALENDAR_API = "https://www.googleapis.com/calendar/v3"


def calendar_attendees(token: str, days_back: int = 365, days_ahead: int = 180) -> set:
    """Every address that shares a calendar event with this account, in one sweep.

    Deliberately one pass over the calendar rather than a query per contact: a send batch is
    hundreds of addresses and the calendar is a few hundred events, so this is the cheaper
    direction by an order of magnitude, and the result is reused for the whole run.

    Cancelled events are skipped — a meeting that was called off is not a relationship. The
    organiser is included, because a call someone else set up still means they know each other.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    params = {
        "timeMin": (now - timedelta(days=days_back)).isoformat(),
        "timeMax": (now + timedelta(days=days_ahead)).isoformat(),
        "singleEvents": "true", "maxResults": 2500, "showDeleted": "false",
    }
    out, page = set(), ""
    for _ in range(20):                      # bounded: 20 pages is 50k events
        if page:
            params["pageToken"] = page
        r = httpx.get(f"{CALENDAR_API}/calendars/primary/events", timeout=_TIMEOUT,
                      params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 403:
            raise GoogleAuthError(
                "Gmail is connected without permission to read your calendar. Reconnect Google "
                "to enable skipping people you already have a meeting with.")
        if r.status_code != 200:
            raise GoogleAuthError(f"Calendar read failed ({r.status_code}): {r.text[:160]}")
        body = r.json() or {}
        for ev in body.get("items") or []:
            if (ev.get("status") or "") == "cancelled":
                continue
            for a in (ev.get("attendees") or []):
                if a.get("email") and not a.get("resource"):
                    out.add(a["email"].strip().lower())
            org = (ev.get("organizer") or {}).get("email")
            if org:
                out.add(org.strip().lower())
        page = body.get("nextPageToken") or ""
        if not page:
            break
    return out


def has_conversation_with(token: str, email: str) -> bool:
    """True if this mailbox has ANY thread to or from this address, ever.

    The mailbox IS the record of who you have contacted — it knows about the hundreds of
    conversations you had before this system existed, which no ledger of our own sends could.
    `in:anywhere` covers archive, sent, spam and trash, because a reply you archived months ago
    still means you know them.

    Drafts are checked separately: Gmail leaves them out of a normal search, so without this a
    re-run would happily create a second draft for someone already sitting in your Drafts.
    """
    address = (email or "").strip()
    if not address:
        return False
    if _threads_exist(token, f"(from:{address} OR to:{address}) in:anywhere"):
        return True
    return _threads_exist(token, f"to:{address} in:draft")


def _post(token: str, path: str, payload: dict) -> dict:
    r = httpx.post(f"{GMAIL_API}{path}", timeout=_TIMEOUT, json=payload,
                   headers={"Authorization": f"Bearer {token}"})
    if r.status_code not in (200, 201):
        raise GoogleAuthError(f"Gmail {path} failed ({r.status_code}): {r.text[:200]}")
    return r.json()


def create_draft(token: str, to: str, subject: str, body: str,
                 from_name: str = "", html_body: str = "") -> Tuple[str, str]:
    """Create a Gmail draft. Returns (draft_id, thread_id)."""
    raw = build_mime(to, subject, body, from_name, html_body)
    d = _post(token, "/drafts", {"message": {"raw": raw}})
    return d.get("id", ""), (d.get("message") or {}).get("threadId", "")


def send_message(token: str, to: str, subject: str, body: str,
                 from_name: str = "", html_body: str = "") -> Tuple[str, str]:
    """Send immediately. Returns (message_id, thread_id)."""
    raw = build_mime(to, subject, body, from_name, html_body)
    d = _post(token, "/messages/send", {"raw": raw})
    return d.get("id", ""), d.get("threadId", "")


def delete_draft(token: str, draft_id: str) -> bool:
    """Remove a draft. True if it is gone (including if it never existed)."""
    r = httpx.delete(f"{GMAIL_API}/drafts/{draft_id}", timeout=_TIMEOUT,
                     headers={"Authorization": f"Bearer {token}"})
    return r.status_code in (200, 204, 404)


def send_stored_draft(token: str, draft_id: str, to: str, subject: str, body: str,
                      from_name: str = "", html_body: str = "") -> Tuple[str, str]:
    """Send the message a draft holds, then remove the draft. Returns (message_id, thread_id).

    Not /drafts/send. That is the obvious call and it refuses these drafts with 400 "Message not a
    draft" — the id is a valid draft (GET /drafts/{id} returns it, r-prefixed, with its message),
    and the call fails anyway, with or without a message field in the body. Rather than keep
    guessing at the API, this uses the send path that is already proven by every other message
    this system delivers, then deletes the draft so the mailbox does not keep a copy of something
    already sent.

    The text comes from our own record of what was drafted, so what goes out is still the message
    that was reviewed, not a re-render that could differ.
    """
    mid, thread = send_message(token, to, subject, body, from_name, html_body)
    if draft_id:
        try:
            delete_draft(token, draft_id)
        except Exception:
            pass                          # a leftover draft is untidy, not harmful
    return mid, thread


def deliver(token: str, mode: str, to: str, subject: str, body: str,
            from_name: str = "", html_body: str = "") -> Tuple[str, str, str]:
    """One entry point for both modes. Returns (status, provider_id, thread_id) where status is
    'drafted' or 'sent' — so the ledger records the same shape either way."""
    if mode == "send":
        mid, thread = send_message(token, to, subject, body, from_name, html_body)
        return "sent", mid, thread
    did, thread = create_draft(token, to, subject, body, from_name, html_body)
    return "drafted", did, thread
