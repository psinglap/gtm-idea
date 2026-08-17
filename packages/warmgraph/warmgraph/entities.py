"""Normalized relational entities (the new schema) — stored as real rows with IDs + FK references on
Neon Postgres (JSON-array embeddings on SQLite for tests). Distinct class names from the legacy
`models.py` DTOs (Account/Contact/Signal) which stay as the API serving shape.

Naming: `companies` = CLIENTS (the URL posters, `company_id`); `customers` = PROSPECTS (the customer
list, `customer_id`). Layers: raw_* (bronze) → signals/customers/people (refined) →
customer_list/customer_contacts (serving)."""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from warmgraph.models import new_id, utcnow

Vec = List[float]  # embedding (pgvector on PG; JSON array on SQLite)


# --------------------------------------------------------------------------- #
# Clients + intelligence                                                       #
# --------------------------------------------------------------------------- #
class Client(BaseModel):
    """`companies` — a client/tenant (whoever entered the URL, e.g. example.com)."""
    id: str = Field(default_factory=lambda: new_id("comp"))
    domain: str = ""            # UNIQUE
    url: str = ""
    name: str = ""
    industry: str = ""
    relationship_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    data: dict = Field(default_factory=dict)


class CompanyIntel(BaseModel):
    """`company_intel` — CI + ICP + embedded signal contexts for a client (was `profiles`)."""
    id: str = Field(default_factory=lambda: new_id("intel"))
    company_id: str = ""        # FK -> companies
    domain: str = ""
    profile: dict = Field(default_factory=dict)
    competitive: dict = Field(default_factory=dict)
    icp: dict = Field(default_factory=dict)
    contexts: list = Field(default_factory=list)   # [{signal_type, context_text, embedding, params}]
    refreshed_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Raw corpora (bronze) — one table per source shape; dedup by url               #
# --------------------------------------------------------------------------- #
class RawDoc(BaseModel):
    """Shared base for document-shaped raw scrapes (job postings, funding news)."""
    id: str = Field(default_factory=lambda: new_id("raw"))
    source: str = ""
    url: str = ""              # UNIQUE (dedup)
    title: str = ""
    content: str = ""
    scraped_at: datetime = Field(default_factory=utcnow)
    embedding: Vec = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class RawJobPosting(RawDoc):
    company_hint: str = ""
    role: str = ""
    location: str = ""


class RawFundingNews(RawDoc):
    published_at: str = ""


class RawSocialPost(BaseModel):
    """`raw_social_posts` — one table, `platform` column (same shape, different scrapers)."""
    id: str = Field(default_factory=lambda: new_id("rsp"))
    platform: str = ""         # linkedin|twitter|reddit|bluesky|hackernews|...
    external_id: str = ""
    author: str = ""
    author_handle: str = ""
    text: str = ""
    url: str = ""
    posted_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=utcnow)
    embedding: Vec = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class RawEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rev"))
    source: str = ""           # luma|meetup|...
    url: str = ""              # UNIQUE
    title: str = ""
    description: str = ""
    starts_at: Optional[datetime] = None
    city: str = ""
    is_virtual: bool = False
    embedding: Vec = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class RawPerson(BaseModel):
    """`raw_people` — unified raw people activity log from ANY source (events/LinkedIn/providers).
    Refined into `people` by identity resolution. Event attendees fold in here."""
    id: str = Field(default_factory=lambda: new_id("rpe"))
    source: str = ""           # provider/source
    person: str = ""
    title: str = ""
    company_hint: str = ""
    linkedin_url: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    where_active: List[str] = Field(default_factory=list)   # platforms
    current_problems: List[str] = Field(default_factory=list)
    event_refs: List[str] = Field(default_factory=list)     # raw_events ids/urls attended
    posts_refs: List[str] = Field(default_factory=list)     # raw_social_posts ids/urls
    scraped_at: datetime = Field(default_factory=utcnow)
    embedding: Vec = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Refined: signals (facts) + prospects + people                                #
# --------------------------------------------------------------------------- #
class SignalFact(BaseModel):
    """`signals` — a GLOBAL fact about a prospect ("X is hiring role Y"), embedded. Refined from raw.
    NO client FK: a fact is reusable across clients (per-client relevance is computed at ranking)."""
    id: str = Field(default_factory=lambda: new_id("sig"))
    customer_id: str = ""      # FK -> customers (the PROSPECT it's about)
    signal_type: str = ""      # hiring|fundraising|social|team
    source: str = ""
    source_url: str = ""       # -> the raw row that produced it
    signal_date: str = ""
    text: str = ""
    role: str = ""
    relevance: str = ""
    industry: str = ""
    embedding: Vec = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class Prospect(BaseModel):
    """`customers` — a prospect company, discovered from signals (dedup by domain)."""
    id: str = Field(default_factory=lambda: new_id("cust"))
    domain: str = ""           # UNIQUE
    name: str = ""
    name_key: str = ""         # normalized-name fallback when domain missing
    website: str = ""
    industry: str = ""
    location: str = ""
    funding_stage: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    data: dict = Field(default_factory=dict)


class CustomerListRow(BaseModel):
    """`customer_list` — per-client ranked view: client ↔ prospect. UNIQUE(company_id, customer_id)."""
    id: str = Field(default_factory=lambda: new_id("cl"))
    company_id: str = ""       # FK -> companies (CLIENT)
    customer_id: str = ""      # FK -> customers (PROSPECT)
    stack_score: int = 0
    pref_score: float = 0.0
    relevance: float = 0.0
    latest_signal_date: str = ""
    status: str = "new"        # new|approved|rejected
    created_at: datetime = Field(default_factory=utcnow)


class Person(BaseModel):
    """`people` — our OWN global people DB, refined from many `raw_people` by identity resolution
    (shared-key + embedding/context match, merge all details). Keyed to EMPLOYER by company_domain."""
    id: str = Field(default_factory=lambda: new_id("person"))
    person: str = ""
    title: str = ""
    seniority: str = ""        # buyer|champion
    is_decision_maker: bool = False
    company_domain: str = ""   # EMPLOYER (matched to customers.domain)
    linkedin_url: str = ""
    email: str = ""
    email_status: str = "unknown"
    email_confidence: float = 0.0
    phone: str = ""
    location: str = ""
    titles: List[str] = Field(default_factory=list)          # all titles seen across sources
    problems: List[str] = Field(default_factory=list)
    where_active: List[str] = Field(default_factory=list)
    touchpoint_refs: List[str] = Field(default_factory=list)
    providers: List[str] = Field(default_factory=list)
    source_url: str = ""
    embedding: Vec = Field(default_factory=list)             # rich profile vector
    data: dict = Field(default_factory=dict)                 # merged raw payloads
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class CustomerContact(BaseModel):
    """`customer_contacts` — the contacts identified AT a prospect, for a client."""
    id: str = Field(default_factory=lambda: new_id("cc"))
    company_id: str = ""       # FK -> companies (CLIENT)
    customer_id: str = ""      # FK -> customers (PROSPECT)
    person_id: str = ""        # FK -> people
    is_decision_maker: bool = False
    role_match: str = ""
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Engagement                                                                   #
# --------------------------------------------------------------------------- #
class Touchpoint(BaseModel):
    """`touchpoints` — a concrete way to tap into a contact, computed by matching a person across
    ALL raw (their posts / events / their company's hiring+funding). Each = a specific opening with
    a suggested action + the evidence it's grounded in."""
    id: str = Field(default_factory=lambda: new_id("tp"))
    company_id: str = ""       # FK -> companies (CLIENT)
    customer_id: str = ""      # FK -> customers (PROSPECT)
    person_id: str = ""        # FK -> people
    type: str = ""             # post-comment | problem | event | company-signal | intro
    source: str = ""           # platform / source (linkedin, luma, greenhouse, techcrunch, …)
    url: str = ""
    date: str = ""
    title: str = ""
    suggested_action: str = ""
    evidence: str = ""
    status: str = "new"        # new | queued | done | dismissed
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- #
# Event outreach — connected accounts, the event contact list (= work queue),   #
# the send ledger, and manual exclusions. See the Luma → Gmail pipeline.        #
# --------------------------------------------------------------------------- #
def norm_email(value: str) -> str:
    """Lowercased, trimmed email — the key every suppression lookup uses."""
    return (value or "").strip().lower()


def email_domain(value: str) -> str:
    return norm_email(value).rpartition("@")[2]


def contact_key(linkedin_url: str = "", luma_user_id: str = "") -> str:
    """Stable per-attendee dedup key, available at SCAN time (before identity resolution gives
    us a person_id). LinkedIn URL normalized to its handle; falls back to the Luma user id."""
    u = (linkedin_url or "").strip().lower()
    if u:
        u = u.split("?")[0].rstrip("/")
        u = u.rpartition("/in/")[2] or u.rpartition("/")[2]
        if u:
            return "li:" + u
    return ("luma:" + luma_user_id.strip()) if luma_user_id else ""


# The queue state machine. `queued` -> `reading` (leased by a browser) -> `profiled` (LinkedIn
# text captured) -> `judged`/`rejected` -> `enriched` (email found) -> `sent`/`drafted`/`skipped`.
# `unreadable` = LinkedIn could not be read after retries, so per the hard gate it is dropped.
EVENT_CONTACT_STATUSES = (
    "queued", "reading", "profiled", "judged", "rejected", "enriched",
    "drafted", "sent", "skipped", "unreadable", "no_linkedin",
)


class Connection(BaseModel):
    """`connections` — one connected account per provider, per client.

    NOT a login: Gmail is connected for the capability (sending), never used to authenticate
    into the app. Luma and LinkedIn store NO credential at all — they are "connected" when the
    browser extension reports a live logged-in session, so `secret` stays empty for those.
    `secret` is Fernet ciphertext (Gmail refresh token / Apollo API key) and must never leave
    the server: use `redacted()` for anything API-facing."""
    id: str = Field(default_factory=lambda: new_id("conn"))
    company_id: str = ""       # FK -> companies (CLIENT)
    provider: str = ""         # luma | linkedin | apollo | gmail
    status: str = "disconnected"   # connected | disconnected | expired | error
    account_label: str = ""    # display only, e.g. the Gmail address
    secret: str = ""           # Fernet ciphertext; "" for session-based providers
    scopes: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    last_ok_at: Optional[datetime] = None
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def redacted(self) -> dict:
        """Safe to serialize to the web app: everything except the secret."""
        d = self.model_dump(mode="json")
        d.pop("secret", None)
        d["has_secret"] = bool(self.secret)
        return d


class EventRegistration(BaseModel):
    """`event_registrations` — ONE CLIENT'S relationship with one event.

    `raw_events` stays global: an event is a public fact, and two clients attending the same
    Luma event must share one row. But everything here is personal — your ticket key, whether
    the host approved *you*, whether *you* have scanned it. Keeping these on the shared event
    row means the second client to sync silently overwrites the first one's registration status,
    which stops their scans without any visible error.
    """
    id: str = Field(default_factory=lambda: new_id("ereg"))
    company_id: str = ""       # FK -> companies (CLIENT)
    event_id: str = ""         # FK -> raw_events (the shared event)
    ticket_key: str = ""       # per-attendee; required to read the guest list
    approval_status: str = ""  # invited | pending_approval | approved
    show_guest_list: bool = False
    guest_count: int = 0
    scanned_at: str = ""
    registered_at: str = ""
    short_name: str = ""       # what the email calls this event; rule-derived then user-editable
    short_name_source: str = ""   # venue | calendar | title — so the UI can show WHY
    short_name_edited: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def scannable(self) -> bool:
        """Verified live against Luma: the guest list needs BOTH an approved registration AND a
        visible list. `invited` and `pending_approval` both 403."""
        return self.approval_status == "approved" and self.show_guest_list

    @property
    def blocked_reason(self) -> str:
        if not self.show_guest_list:
            return "guest_list_hidden"
        return self.approval_status if self.approval_status != "approved" else ""


class EventContact(BaseModel):
    """`event_contacts` — the event contact list AND the work queue, one row per attendee per
    event, per client. Separate from `customer_contacts` (the signal-driven list) on purpose;
    company details are reached through `customer_id` rather than duplicated here."""
    id: str = Field(default_factory=lambda: new_id("ec"))
    company_id: str = ""       # FK -> companies (CLIENT)
    event_id: str = ""         # FK -> raw_events
    person_id: str = ""        # FK -> people    (set once identity-resolved)
    customer_id: str = ""      # FK -> customers (set once Apollo gives us their employer)
    contact_key: str = ""      # dedup key available at scan time (see contact_key())

    # --- from the Luma guest list ---
    luma_user_id: str = ""
    name: str = ""
    linkedin_url: str = ""
    luma_bio: str = ""
    avatar_url: str = ""

    # --- from the LinkedIn read (the hard gate: no text, no judgment) ---
    linkedin_headline: str = ""
    linkedin_text: str = ""

    # --- from the judge ---
    verdict: str = ""          # target | reject
    score: float = 0.0
    reason: str = ""
    judged_by: str = ""

    # --- from Apollo ---
    email: str = ""
    email_key: str = ""        # normalized, for suppression lookups
    # Apollo's confidence in the address: "verified" | "extrapolated" | "unavailable" | "".
    # Recorded rather than inferred, because an address with no status is not the same as a good
    # one. A hand-built sheet imported 98 addresses with no status; 17 turned out to be
    # extrapolated — Apollo guessing first.last@domain from a naming pattern — and the first one
    # sent bounced. Only "verified" is mailable; see outreach_enrich.ACCEPTED_EMAIL_STATUS.
    email_status: str = ""
    # True when the employer's mail server accepts EVERY address. On such a domain Apollo's
    # "verified" means only that the server answered yes, so the address can be perfectly
    # deliverable and still belong to nobody. Mail to it reaches a stranger rather than bouncing.
    email_catchall: bool = False
    title: str = ""
    company_name: str = ""

    # --- queue mechanics ---
    status: str = "queued"
    priority: int = 0          # higher first; set from event recency + whether a bio exists
    leased_by: str = ""        # browser instance id holding this row
    lease_expires_at: Optional[datetime] = None
    attempts: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OutreachMessage(BaseModel):
    """`outreach_messages` — the send ledger and audit trail. One row per person we have ever
    drafted, sent, or deliberately skipped. This table IS the suppression source: the mailbox
    is never read, so anything not recorded here is invisible to us."""
    id: str = Field(default_factory=lambda: new_id("om"))
    company_id: str = ""       # FK -> companies (CLIENT)
    event_id: str = ""         # FK -> raw_events
    person_id: str = ""        # FK -> people
    event_contact_id: str = ""  # FK -> event_contacts
    email: str = ""
    email_key: str = ""        # normalized email — the suppression lookup key
    subject: str = ""
    body: str = ""
    gmail_draft_id: str = ""
    gmail_message_id: str = ""
    gmail_thread_id: str = ""
    status: str = "drafted"    # drafted | sent | skipped | failed
    skip_reason: str = ""      # already_contacted | do_not_contact | own_domain | no_email | …
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    sent_at: Optional[datetime] = None


class DoNotContact(BaseModel):
    """`do_not_contact` — manual exclusions (investors, customers, friends, competitors). An
    email or a bare domain. Checked at send time; this is how the no-mailbox-read blind spot
    gets closed."""
    id: str = Field(default_factory=lambda: new_id("dnc"))
    company_id: str = ""       # FK -> companies (CLIENT)
    value: str = ""            # normalized email OR bare domain ("acme.com")
    kind: str = "email"        # email | domain
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
