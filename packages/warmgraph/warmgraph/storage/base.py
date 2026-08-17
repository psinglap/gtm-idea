from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from warmgraph.models import (
    Account,
    CompanyLead,
    CompetitiveIntelligenceReport,
    Contact,
    CustomerLead,
    Event,
    LeadFeedback,
    Post,
    Profile,
    Signal,
)
from warmgraph.entities import (
    Client,
    CompanyIntel,
    Connection,
    CustomerContact,
    CustomerListRow,
    DoNotContact,
    EventContact,
    EventRegistration,
    OutreachMessage,
    Person,
    Prospect,
    RawEvent,
    RawFundingNews,
    RawJobPosting,
    RawPerson,
    RawSocialPost,
    SignalFact,
    Touchpoint,
)


class Store(ABC):
    """Persistence for the agent platform. SQLite (local/test) + Postgres (Neon, prod)
    implement it. Tables: ci_reports (CI/ICP JSONB), posts, signals, events, customer_leads."""

    @abstractmethod
    def init_schema(self) -> None: ...

    # --- Competitive intelligence (CI + ICP/winning-category live in the JSONB) ---
    @abstractmethod
    def save_ci_report(
        self, report: CompetitiveIntelligenceReport
    ) -> CompetitiveIntelligenceReport: ...

    @abstractmethod
    def get_ci_report(self, report_id: str) -> Optional[CompetitiveIntelligenceReport]: ...

    # --- Signals layer ---
    @abstractmethod
    def save_posts(self, posts: List[Post]) -> int:
        """Upsert posts, deduped on (platform, external_id). Returns # newly inserted."""

    @abstractmethod
    def get_posts(self, subject_domain: str, platform: Optional[str] = None,
                  limit: int = 200) -> List[Post]: ...

    @abstractmethod
    def get_recent_posts(self, since_days: int = 90, limit: int = 1000) -> List[Post]:
        """Global recent posts (across ALL customers) — the shared corpus for semantic retrieval."""

    @abstractmethod
    def save_signals(self, signals: List[Signal]) -> int: ...

    @abstractmethod
    def get_signals(self, subject_domain: str, limit: int = 500) -> List[Signal]: ...

    @abstractmethod
    def save_events(self, events: List[Event]) -> int: ...

    @abstractmethod
    def get_events(self, subject_domain: str, limit: int = 200) -> List[Event]: ...

    @abstractmethod
    def save_leads(self, leads: List[CustomerLead]) -> int: ...

    @abstractmethod
    def get_leads(self, subject_domain: str, limit: int = 500) -> List[CustomerLead]: ...

    # --- Customer profile (built once per URL, reused) ---
    @abstractmethod
    def save_profile(self, profile: Profile) -> Profile: ...

    @abstractmethod
    def get_profile(self, domain: str) -> Optional[Profile]: ...

    @abstractmethod
    def save_company_leads(self, leads: List[CompanyLead]) -> int: ...

    @abstractmethod
    def get_company_leads(self, subject_domain: str, signal_type: Optional[str] = None,
                          limit: int = 500) -> List[CompanyLead]: ...

    @abstractmethod
    def get_recent_company_leads(self, signal_type: str, limit: int = 400) -> List[CompanyLead]:
        """Global company leads of a type (across ALL customers) — the shared corpus."""

    # --- Accounts (signal stacking) ---
    @abstractmethod
    def save_accounts(self, accounts: List[Account]) -> int: ...

    @abstractmethod
    def get_accounts(self, subject_domain: str, limit: int = 200) -> List[Account]: ...

    # --- Eval / preference feedback (approve-reject on leads) ---
    @abstractmethod
    def save_feedback(self, feedback: List[LeadFeedback]) -> int: ...

    @abstractmethod
    def get_feedback(self, subject_domain: str, limit: int = 1000) -> List[LeadFeedback]: ...

    # --- Contacts (decision-makers per company) ---
    @abstractmethod
    def save_contacts(self, contacts: List[Contact]) -> int: ...

    @abstractmethod
    def get_contacts(self, subject_domain: str, limit: int = 1000) -> List[Contact]: ...

    @abstractmethod
    def get_contacts_for_company(self, company_domain: str, limit: int = 20) -> List[Contact]: ...

    # ======================================================================= #
    # Normalized relational schema (clients → intel → raw → signals →          #
    # customers → customer_list → people → customer_contacts). See entities.py. #
    # The layered pattern: raw_* (bronze) → signals/customers/people (refined)  #
    # → customer_list/customer_contacts (serving). Embeddings ride inside the   #
    # JSON `data` payload; brute-force cosine in Python (pgvector ANN later).   #
    # ======================================================================= #

    # --- Clients (companies) ---
    @abstractmethod
    def upsert_company(self, client: Client) -> Client:
        """Get-or-create a client by domain; preserves the existing id on conflict."""

    @abstractmethod
    def get_company(self, domain: str) -> Optional[Client]: ...

    @abstractmethod
    def get_company_by_id(self, company_id: str) -> Optional[Client]: ...

    # --- Intelligence (company_intel, was profiles) ---
    @abstractmethod
    def save_company_intel(self, intel: CompanyIntel) -> CompanyIntel: ...

    @abstractmethod
    def get_company_intel(self, domain: str) -> Optional[CompanyIntel]: ...

    # --- Raw corpora (bronze); scrapers write here, analyzers read ---
    @abstractmethod
    def save_raw_job_postings(self, rows: List[RawJobPosting]) -> int:
        """Upsert deduped on url. Returns # newly inserted."""

    @abstractmethod
    def save_raw_funding_news(self, rows: List[RawFundingNews]) -> int: ...

    @abstractmethod
    def save_raw_social_posts(self, rows: List[RawSocialPost]) -> int:
        """Upsert deduped on (platform, external_id). Returns # newly inserted."""

    @abstractmethod
    def save_raw_events(self, rows: List[RawEvent]) -> int: ...

    @abstractmethod
    def save_raw_people(self, rows: List[RawPerson]) -> int: ...

    @abstractmethod
    def get_raw_job_postings(self, since_days: int = 90, limit: int = 1000) -> List[RawJobPosting]: ...

    @abstractmethod
    def get_raw_funding_news(self, since_days: int = 90, limit: int = 1000) -> List[RawFundingNews]: ...

    @abstractmethod
    def get_raw_social_posts(self, since_days: int = 90, limit: int = 1000) -> List[RawSocialPost]: ...

    @abstractmethod
    def get_raw_events(self, since_days: int = 180, limit: int = 500) -> List[RawEvent]: ...

    @abstractmethod
    def get_raw_people(self, limit: int = 2000) -> List[RawPerson]: ...

    # --- Signals (global facts about prospects) ---
    @abstractmethod
    def save_signal_facts(self, rows: List[SignalFact]) -> int: ...

    @abstractmethod
    def get_signal_facts(self, signal_type: Optional[str] = None, customer_id: Optional[str] = None,
                         since_days: Optional[int] = 90, limit: int = 1000) -> List[SignalFact]:
        """Global shared corpus of signal facts (across ALL clients) for semantic retrieval."""

    # --- Customers (prospect registry) ---
    @abstractmethod
    def upsert_customer(self, prospect: Prospect) -> Prospect:
        """Get-or-create a prospect by domain (name_key fallback); preserves the existing id."""

    @abstractmethod
    def get_customer(self, domain: str) -> Optional[Prospect]: ...

    @abstractmethod
    def get_customer_by_id(self, customer_id: str) -> Optional[Prospect]: ...

    # --- Customer list (per-client ranked serving view) ---
    @abstractmethod
    def replace_customer_list(self, company_id: str, rows: List[CustomerListRow]) -> int:
        """Atomically replace a client's ranked customer_list."""

    @abstractmethod
    def get_customer_list(self, company_id: str, limit: int = 200) -> List[CustomerListRow]: ...

    # --- People (global identity-resolved DB) ---
    @abstractmethod
    def save_people(self, rows: List[Person]) -> int: ...

    @abstractmethod
    def get_people(self, company_domain: Optional[str] = None, limit: int = 2000) -> List[Person]: ...

    @abstractmethod
    def get_person(self, person_id: str) -> Optional[Person]: ...

    # --- Customer contacts (contacts AT a prospect, for a client) ---
    @abstractmethod
    def save_customer_contacts(self, rows: List[CustomerContact]) -> int: ...

    @abstractmethod
    def get_customer_contacts(self, company_id: str, customer_id: Optional[str] = None,
                              limit: int = 500) -> List[CustomerContact]: ...

    # --- Engagement (touchpoints) ---
    @abstractmethod
    def save_touchpoints(self, rows: List[Touchpoint]) -> int:
        """Upsert deduped on (company_id, person_id, url). Returns # rows written."""

    @abstractmethod
    def get_touchpoints(self, company_id: str, person_id: Optional[str] = None,
                        limit: int = 500) -> List[Touchpoint]: ...

    # ======================================================================= #
    # Event outreach (Luma → LinkedIn → Apollo → Gmail). See entities.py.      #
    # ======================================================================= #

    # --- Events whose state changes over time ---
    @abstractmethod
    def upsert_raw_event(self, event: RawEvent) -> RawEvent:
        """Insert-or-UPDATE by url, preserving the existing id.

        `save_raw_events` is insert-only (bronze rows are immutable scrapes), but a Luma
        registration is a state machine: invited → pending_approval → approved, and separately
        scanned/unscanned. Those transitions have to persist, so event rows need a real upsert.
        """

    @abstractmethod
    def get_raw_event(self, event_id: str) -> Optional[RawEvent]: ...

    @abstractmethod
    def get_raw_event_by_url(self, url: str) -> Optional[RawEvent]:
        """Indexed single-event lookup. Syncing N events must not load the whole table N times."""

    # --- Indexed lookups that keep the hot paths O(1) instead of O(all rows) ---
    @abstractmethod
    def get_company_by_token(self, token: str) -> Optional[Client]:
        """Resolve a workspace token. Called on EVERY extension request, so it must be an
        indexed lookup rather than a scan over every client."""

    @abstractmethod
    def get_person_by_linkedin(self, linkedin_url: str) -> Optional[Person]:
        """Exact identity match on the one key event attendees always have. Avoids pulling the
        whole `people` table through identity resolution for each enrichment."""

    # --- Per-client event registrations (the personal half of a shared event) ---
    @abstractmethod
    def upsert_event_registration(self, reg: EventRegistration) -> EventRegistration:
        """Get-or-create by (company_id, event_id); preserves the existing id."""

    @abstractmethod
    def get_event_registration(self, company_id: str,
                               event_id: str) -> Optional[EventRegistration]: ...

    @abstractmethod
    def get_event_registrations(self, company_id: str,
                                limit: int = 500) -> List[EventRegistration]: ...

    # --- Connected accounts ---
    @abstractmethod
    def sent_addresses_since(self, company_id: str, since) -> set:
        raise NotImplementedError

    def contacts_by_email(self, company_id: str, emails) -> List[EventContact]:
        raise NotImplementedError

    def drafted_messages_by_contact(self, company_id: str) -> dict:
        raise NotImplementedError

    def count_sent_since(self, company_id: str, since) -> int:
        raise NotImplementedError

    def sent_messages_since(self, company_id: str, since, limit: int = 200):
        raise NotImplementedError

    def event_contact_counts(self, company_id: str) -> dict:
        """Aggregate counts for the funnel and reports, without reading the rows."""
        raise NotImplementedError

    def contacts_per_event(self, company_id: str) -> dict:
        raise NotImplementedError

    def upsert_connection(self, conn: Connection) -> Connection:
        """Get-or-create by (company_id, provider); preserves the existing id on conflict."""

    @abstractmethod
    def get_connection(self, company_id: str, provider: str) -> Optional[Connection]: ...

    @abstractmethod
    def list_connections(self, company_id: str) -> List[Connection]: ...

    @abstractmethod
    def delete_connection(self, company_id: str, provider: str) -> bool:
        """Returns True if a row was removed."""

    # --- Event contacts (the event list + the work queue) ---
    @abstractmethod
    def save_event_contacts(self, rows: List[EventContact]) -> int:
        """INSERT-IF-ABSENT on (company_id, event_id, contact_key). Re-scanning an event must
        never reset progress on rows already in flight. Returns # newly inserted."""

    @abstractmethod
    def update_event_contacts(self, rows: List[EventContact]) -> int:
        """Overwrite by id — the path every pipeline stage uses to advance a row."""

    @abstractmethod
    def get_event_contact(self, contact_id: str) -> Optional[EventContact]: ...

    @abstractmethod
    def get_event_contacts(self, company_id: str, event_id: Optional[str] = None,
                           status: Optional[str] = None, limit: int = 500) -> List[EventContact]: ...

    @abstractmethod
    def count_event_contacts(self, company_id: str) -> dict:
        """{status: count} for the queue display."""

    @abstractmethod
    def lease_event_contacts(self, company_id: str, leased_by: str, limit: int = 25,
                             lease_seconds: int = 600) -> List[EventContact]:
        """Atomically claim up to `limit` `queued` rows for a browser worker: sets status
        `reading`, `leased_by`, and `lease_expires_at`. Highest priority first."""

    @abstractmethod
    def release_expired_leases(self, company_id: Optional[str] = None) -> int:
        """Return rows whose lease expired (Chrome quit mid-run) to `queued`. Returns # freed."""

    # --- Send ledger ---
    @abstractmethod
    def save_outreach_messages(self, rows: List[OutreachMessage]) -> int: ...

    @abstractmethod
    def get_outreach_messages(self, company_id: str, status: Optional[str] = None,
                              limit: int = 500) -> List[OutreachMessage]: ...

    @abstractmethod
    def has_contacted(self, company_id: str, email: str) -> bool:
        """True if this address was ever drafted or sent to — the primary suppression check."""

    @abstractmethod
    def count_outreach_messages(self, company_id: str, since_minutes: int,
                                statuses: tuple = ("drafted", "sent")) -> int:
        """How many messages actually went out in the window — drives the daily + hourly caps."""

    # --- Manual exclusions ---
    @abstractmethod
    def save_do_not_contact(self, rows: List[DoNotContact]) -> int: ...

    @abstractmethod
    def get_do_not_contact(self, company_id: str, limit: int = 2000) -> List[DoNotContact]: ...

    @abstractmethod
    def is_do_not_contact(self, company_id: str, email: str) -> bool:
        """True if the address, or its domain, is on the exclusion list."""

    # --- App-wide settings (not per client): the auto-generated encryption key lives here ---
    @abstractmethod
    def get_setting(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def set_setting(self, key: str, value: str) -> None: ...

    # --- Migration / introspection helper ---
    @abstractmethod
    def raw_scan(self, table: str, limit: int = 100000) -> List[dict]:
        """Return the raw JSON `data` payloads of every row in a table (migration helper)."""
