from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
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
    utcnow,
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
    email_domain,
    norm_email,
)
from warmgraph.storage.base import Store


def _dt(value) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class SqliteStore(Store):
    """Zero-infra SQLite store for local dev + tests. Mirrors the Postgres schema."""

    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS ci_reports "
                "(id TEXT PRIMARY KEY, url TEXT, created_at TEXT, data TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS posts ("
                "id TEXT PRIMARY KEY, subject_domain TEXT, platform TEXT, external_id TEXT, "
                "posted_at TEXT, created_at TEXT, data TEXT, UNIQUE(platform, external_id))"
            )
            # legacy `signals` (social Signal model) renamed to `social_signals` — `signals` is now the
            # normalized SignalFact table below.
            for table in ("social_signals", "events", "customer_leads", "accounts", "lead_feedback"):
                self._conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} "
                    "(id TEXT PRIMARY KEY, subject_domain TEXT, created_at TEXT, data TEXT)"
                )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS company_leads "
                "(id TEXT PRIMARY KEY, subject_domain TEXT, signal_type TEXT, created_at TEXT, data TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS contacts "
                "(id TEXT PRIMARY KEY, subject_domain TEXT, company_domain TEXT, created_at TEXT, data TEXT)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS profiles "
                "(domain TEXT PRIMARY KEY, url TEXT, created_at TEXT, refreshed_at TEXT, data TEXT)"
            )
            self._init_normalized(self._conn.execute)
            self._conn.commit()

    # --- Normalized relational schema (clients → raw → signals → customers → people) ---
    def _init_normalized(self, ex) -> None:
        ex("CREATE TABLE IF NOT EXISTS companies "
           "(id TEXT PRIMARY KEY, domain TEXT UNIQUE, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS company_intel "
           "(id TEXT PRIMARY KEY, company_id TEXT, domain TEXT, refreshed_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS raw_job_postings "
           "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS raw_funding_news "
           "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS raw_social_posts "
           "(id TEXT PRIMARY KEY, platform TEXT, external_id TEXT, url TEXT, created_at TEXT, data TEXT, "
           "UNIQUE(platform, external_id))")
        ex("CREATE TABLE IF NOT EXISTS raw_events "
           "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS raw_people "
           "(id TEXT PRIMARY KEY, source TEXT, linkedin_url TEXT, email TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS signals "
           "(id TEXT PRIMARY KEY, customer_id TEXT, signal_type TEXT, source_url TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS customers "
           "(id TEXT PRIMARY KEY, domain TEXT UNIQUE, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS customer_list "
           "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, status TEXT, created_at TEXT, data TEXT, "
           "UNIQUE(company_id, customer_id))")
        ex("CREATE TABLE IF NOT EXISTS people "
           "(id TEXT PRIMARY KEY, linkedin_url TEXT, email TEXT, company_domain TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS customer_contacts "
           "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, person_id TEXT, created_at TEXT, data TEXT, "
           "UNIQUE(company_id, customer_id, person_id))")
        ex("CREATE TABLE IF NOT EXISTS touchpoints "
           "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, person_id TEXT, url_key TEXT, "
           "created_at TEXT, data TEXT, UNIQUE(company_id, person_id, url_key))")
        self._init_outreach(ex)

    # --- Event outreach (connections → event_contacts queue → send ledger) ---
    def _init_outreach(self, ex) -> None:
        ex("CREATE TABLE IF NOT EXISTS connections "
           "(id TEXT PRIMARY KEY, company_id TEXT, provider TEXT, status TEXT, "
           "updated_at TEXT, data TEXT, UNIQUE(company_id, provider))")
        ex("CREATE TABLE IF NOT EXISTS event_contacts "
           "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, contact_key TEXT, person_id TEXT, "
           "status TEXT, priority INTEGER DEFAULT 0, lease_expires_at TEXT, "
           "created_at TEXT, data TEXT, UNIQUE(company_id, event_id, contact_key))")
        # Mirrors the Postgres schema: email / verdict / email_status as real columns so the
        # funnel counts by index rather than extracting JSON per row. SQLite has no
        # ADD COLUMN IF NOT EXISTS, so each is attempted and an existing column is ignored.
        for col in ("email TEXT", "verdict TEXT", "email_status TEXT"):
            try:
                ex(f"ALTER TABLE event_contacts ADD COLUMN {col}")
            except Exception:
                pass                      # already there
        ex("UPDATE event_contacts SET email = json_extract(data,'$.email'), "
           "   verdict = json_extract(data,'$.verdict'), "
           "   email_status = json_extract(data,'$.email_status') "
           "WHERE email IS NULL AND json_extract(data,'$.email') IS NOT NULL")
        ex("CREATE TABLE IF NOT EXISTS outreach_messages "
           "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, person_id TEXT, email_key TEXT, "
           "status TEXT, created_at TEXT, data TEXT)")
        ex("CREATE TABLE IF NOT EXISTS event_registrations "
           "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, status TEXT, "
           "created_at TEXT, data TEXT, UNIQUE(company_id, event_id))")
        ex("CREATE TABLE IF NOT EXISTS app_settings "
           "(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        ex("CREATE TABLE IF NOT EXISTS do_not_contact "
           "(id TEXT PRIMARY KEY, company_id TEXT, value TEXT, created_at TEXT, data TEXT, "
           "UNIQUE(company_id, value))")
        ex("CREATE INDEX IF NOT EXISTS ix_ec_queue ON event_contacts "
           "(company_id, status, priority DESC, created_at)")
        ex("CREATE INDEX IF NOT EXISTS ix_om_email ON outreach_messages (company_id, email_key)")
        ex("CREATE INDEX IF NOT EXISTS ix_om_window ON outreach_messages "
           "(company_id, status, created_at)")
        # Mirrors the Postgres indexes: token resolution runs on every extension request, and
        # attendee identity resolution keys off the LinkedIn URL.
        ex("DROP INDEX IF EXISTS ix_companies_token")   # earlier build indexed the wrong path
        ex("CREATE INDEX IF NOT EXISTS ix_companies_wstoken ON companies "
           "(json_extract(data,'$.data.workspace_token'))")
        ex("CREATE INDEX IF NOT EXISTS ix_people_linkedin ON people (lower(linkedin_url))")

    # --- CI ---
    def save_ci_report(
        self, report: CompetitiveIntelligenceReport
    ) -> CompetitiveIntelligenceReport:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO ci_reports (id, url, created_at, data) VALUES (?,?,?,?)",
                (report.id, report.url, _dt(report.created_at), report.model_dump_json()),
            )
            self._conn.commit()
        return report

    def get_ci_report(self, report_id: str) -> Optional[CompetitiveIntelligenceReport]:
        row = self._conn.execute(
            "SELECT data FROM ci_reports WHERE id=?", (report_id,)
        ).fetchone()
        return CompetitiveIntelligenceReport.model_validate_json(row["data"]) if row else None

    # --- posts (deduped on platform+external_id) ---
    def save_posts(self, posts: List[Post]) -> int:
        inserted = 0
        with self._lock:
            for p in posts:
                ext = p.external_id or p.id
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO posts "
                    "(id,subject_domain,platform,external_id,posted_at,created_at,data) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (p.id, p.subject_domain, p.platform, ext, _dt(p.posted_at), _dt(p.created_at),
                     p.model_dump_json()),
                )
                inserted += cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
            self._conn.commit()
        return inserted

    def get_posts(self, subject_domain: str, platform: Optional[str] = None,
                  limit: int = 200) -> List[Post]:
        with self._lock:
            if platform:
                rows = self._conn.execute(
                    "SELECT data FROM posts WHERE subject_domain=? AND platform=? "
                    "ORDER BY posted_at IS NULL, posted_at DESC LIMIT ?",
                    (subject_domain, platform, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM posts WHERE subject_domain=? "
                    "ORDER BY posted_at IS NULL, posted_at DESC LIMIT ?",
                    (subject_domain, limit)).fetchall()
        return [Post.model_validate_json(r["data"]) for r in rows]

    def get_recent_posts(self, since_days: int = 90, limit: int = 1000) -> List[Post]:
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM posts WHERE posted_at >= ? ORDER BY posted_at DESC LIMIT ?",
                (cutoff, limit)).fetchall()
        return [Post.model_validate_json(r["data"]) for r in rows]

    # --- generic id-keyed JSON tables ---
    def _save_rows(self, table: str, rows) -> int:
        rows = list(rows)
        with self._lock:
            for obj in rows:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO {table} (id,subject_domain,created_at,data) "
                    "VALUES (?,?,?,?)",
                    (obj.id, obj.subject_domain, _dt(obj.created_at), obj.model_dump_json()),
                )
            self._conn.commit()
        return len(rows)

    def _get_rows(self, table: str, model, subject_domain: str, limit: int):
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM {table} WHERE subject_domain=? ORDER BY created_at DESC LIMIT ?",
                (subject_domain, limit)).fetchall()
        return [model.model_validate_json(r["data"]) for r in rows]

    def save_signals(self, signals: List[Signal]) -> int:
        return self._save_rows("social_signals", signals)

    def get_signals(self, subject_domain: str, limit: int = 500) -> List[Signal]:
        return self._get_rows("social_signals", Signal, subject_domain, limit)

    def save_events(self, events: List[Event]) -> int:
        return self._save_rows("events", events)

    def get_events(self, subject_domain: str, limit: int = 200) -> List[Event]:
        return self._get_rows("events", Event, subject_domain, limit)

    def save_leads(self, leads: List[CustomerLead]) -> int:
        return self._save_rows("customer_leads", leads)

    def get_leads(self, subject_domain: str, limit: int = 500) -> List[CustomerLead]:
        return self._get_rows("customer_leads", CustomerLead, subject_domain, limit)

    def save_accounts(self, accounts: List[Account]) -> int:
        return self._save_rows("accounts", accounts)

    def get_accounts(self, subject_domain: str, limit: int = 200) -> List[Account]:
        return self._get_rows("accounts", Account, subject_domain, limit)

    def save_feedback(self, feedback: List[LeadFeedback]) -> int:
        return self._save_rows("lead_feedback", feedback)

    def get_feedback(self, subject_domain: str, limit: int = 1000) -> List[LeadFeedback]:
        return self._get_rows("lead_feedback", LeadFeedback, subject_domain, limit)

    def save_contacts(self, contacts: List[Contact]) -> int:
        contacts = list(contacts)
        with self._lock:
            for c in contacts:
                self._conn.execute(
                    "INSERT OR REPLACE INTO contacts (id,subject_domain,company_domain,created_at,data) "
                    "VALUES (?,?,?,?,?)",
                    (c.id, c.subject_domain, (c.company_domain or "").lower(), _dt(c.created_at),
                     c.model_dump_json()),
                )
            self._conn.commit()
        return len(contacts)

    def get_contacts(self, subject_domain: str, limit: int = 1000) -> List[Contact]:
        return self._get_rows("contacts", Contact, subject_domain, limit)

    def get_contacts_for_company(self, company_domain: str, limit: int = 20) -> List[Contact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM contacts WHERE company_domain=? ORDER BY created_at DESC LIMIT ?",
                ((company_domain or "").lower(), limit)).fetchall()
        return [Contact.model_validate_json(r["data"]) for r in rows]

    def save_profile(self, profile: Profile) -> Profile:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO profiles (domain,url,created_at,refreshed_at,data) "
                "VALUES (?,?,?,?,?)",
                (profile.domain, profile.url, _dt(profile.created_at), _dt(profile.refreshed_at),
                 profile.model_dump_json()),
            )
            self._conn.commit()
        return profile

    def get_profile(self, domain: str) -> Optional[Profile]:
        row = self._conn.execute("SELECT data FROM profiles WHERE domain=?", (domain,)).fetchone()
        return Profile.model_validate_json(row["data"]) if row else None

    def save_company_leads(self, leads: List[CompanyLead]) -> int:
        leads = list(leads)
        with self._lock:
            for L in leads:
                self._conn.execute(
                    "INSERT OR REPLACE INTO company_leads "
                    "(id,subject_domain,signal_type,created_at,data) VALUES (?,?,?,?,?)",
                    (L.id, L.subject_domain, L.signal_type, _dt(L.created_at), L.model_dump_json()),
                )
            self._conn.commit()
        return len(leads)

    def get_company_leads(self, subject_domain, signal_type=None, limit=500) -> List[CompanyLead]:
        with self._lock:
            if signal_type:
                rows = self._conn.execute(
                    "SELECT data FROM company_leads WHERE subject_domain=? AND signal_type=? "
                    "ORDER BY created_at DESC LIMIT ?", (subject_domain, signal_type, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM company_leads WHERE subject_domain=? ORDER BY created_at DESC "
                    "LIMIT ?", (subject_domain, limit)).fetchall()
        return [CompanyLead.model_validate_json(r["data"]) for r in rows]

    def get_recent_company_leads(self, signal_type: str, limit: int = 400) -> List[CompanyLead]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM company_leads WHERE signal_type=? ORDER BY created_at DESC LIMIT ?",
                (signal_type, limit)).fetchall()
        return [CompanyLead.model_validate_json(r["data"]) for r in rows]

    # ===================================================================== #
    # Normalized relational schema (see entities.py)                        #
    # ===================================================================== #
    @staticmethod
    def _ts(obj) -> str:
        for attr in ("created_at", "scraped_at", "refreshed_at"):
            v = getattr(obj, attr, None)
            if v is not None:
                return _dt(v)
        return _dt(utcnow())

    @staticmethod
    def _cutoff(since_days: Optional[int]) -> Optional[str]:
        if not since_days:
            return None
        return (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    def _get_recent(self, table, model, since_days, limit, order="created_at"):
        cutoff = self._cutoff(since_days)
        with self._lock:
            if cutoff:
                rows = self._conn.execute(
                    f"SELECT data FROM {table} WHERE created_at >= ? ORDER BY {order} DESC LIMIT ?",
                    (cutoff, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT data FROM {table} ORDER BY {order} DESC LIMIT ?", (limit,)).fetchall()
        return [model.model_validate_json(r["data"]) for r in rows]

    # --- clients (companies) ---
    def upsert_company(self, client: Client) -> Client:
        dom = (client.domain or "").lower()
        with self._lock:
            row = self._conn.execute("SELECT id FROM companies WHERE domain=?", (dom,)).fetchone()
            if row:
                client.id = row["id"]
            self._conn.execute(
                "INSERT OR REPLACE INTO companies (id,domain,created_at,data) VALUES (?,?,?,?)",
                (client.id, dom, _dt(client.created_at), client.model_dump_json()))
            self._conn.commit()
        return client

    def get_company(self, domain: str) -> Optional[Client]:
        row = self._conn.execute("SELECT data FROM companies WHERE domain=?",
                                 ((domain or "").lower(),)).fetchone()
        return Client.model_validate_json(row["data"]) if row else None

    def get_company_by_id(self, company_id: str) -> Optional[Client]:
        row = self._conn.execute("SELECT data FROM companies WHERE id=?", (company_id,)).fetchone()
        return Client.model_validate_json(row["data"]) if row else None

    # --- intelligence (one row per client domain) ---
    def save_company_intel(self, intel: CompanyIntel) -> CompanyIntel:
        dom = (intel.domain or "").lower()
        with self._lock:
            self._conn.execute("DELETE FROM company_intel WHERE domain=?", (dom,))
            self._conn.execute(
                "INSERT INTO company_intel (id,company_id,domain,refreshed_at,data) VALUES (?,?,?,?,?)",
                (intel.id, intel.company_id, dom, _dt(intel.refreshed_at), intel.model_dump_json()))
            self._conn.commit()
        return intel

    def get_company_intel(self, domain: str) -> Optional[CompanyIntel]:
        row = self._conn.execute(
            "SELECT data FROM company_intel WHERE domain=? ORDER BY refreshed_at DESC LIMIT 1",
            ((domain or "").lower(),)).fetchone()
        return CompanyIntel.model_validate_json(row["data"]) if row else None

    # --- raw corpora ---
    def _save_raw_url(self, table: str, rows) -> int:
        rows = list(rows)
        inserted = 0
        with self._lock:
            for r in rows:
                key_url = r.url or r.id   # avoid false-dedup of blank urls (UNIQUE(url))
                cur = self._conn.execute(
                    f"INSERT OR IGNORE INTO {table} (id,url,source,created_at,data) VALUES (?,?,?,?,?)",
                    (r.id, key_url, r.source, self._ts(r), r.model_dump_json()))
                inserted += cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
            self._conn.commit()
        return inserted

    def save_raw_job_postings(self, rows: List[RawJobPosting]) -> int:
        return self._save_raw_url("raw_job_postings", rows)

    def save_raw_funding_news(self, rows: List[RawFundingNews]) -> int:
        return self._save_raw_url("raw_funding_news", rows)

    def save_raw_events(self, rows: List[RawEvent]) -> int:
        return self._save_raw_url("raw_events", rows)

    def save_raw_social_posts(self, rows: List[RawSocialPost]) -> int:
        rows = list(rows)
        inserted = 0
        with self._lock:
            for r in rows:
                ext = r.external_id or r.id
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO raw_social_posts "
                    "(id,platform,external_id,url,created_at,data) VALUES (?,?,?,?,?,?)",
                    (r.id, r.platform, ext, r.url, self._ts(r), r.model_dump_json()))
                inserted += cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0
            self._conn.commit()
        return inserted

    def save_raw_people(self, rows: List[RawPerson]) -> int:
        rows = list(rows)
        with self._lock:
            for r in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO raw_people "
                    "(id,source,linkedin_url,email,created_at,data) VALUES (?,?,?,?,?,?)",
                    (r.id, r.source, (r.linkedin_url or "").lower(), (r.email or "").lower(),
                     self._ts(r), r.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_raw_job_postings(self, since_days: int = 90, limit: int = 1000) -> List[RawJobPosting]:
        return self._get_recent("raw_job_postings", RawJobPosting, since_days, limit)

    def get_raw_funding_news(self, since_days: int = 90, limit: int = 1000) -> List[RawFundingNews]:
        return self._get_recent("raw_funding_news", RawFundingNews, since_days, limit)

    def get_raw_social_posts(self, since_days: int = 90, limit: int = 1000) -> List[RawSocialPost]:
        return self._get_recent("raw_social_posts", RawSocialPost, since_days, limit)

    def get_raw_events(self, since_days: int = 180, limit: int = 500) -> List[RawEvent]:
        return self._get_recent("raw_events", RawEvent, since_days, limit)

    def get_raw_people(self, limit: int = 2000) -> List[RawPerson]:
        return self._get_recent("raw_people", RawPerson, None, limit)

    # --- signals (global facts) ---
    def save_signal_facts(self, rows: List[SignalFact]) -> int:
        rows = list(rows)
        with self._lock:
            for r in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO signals "
                    "(id,customer_id,signal_type,source_url,created_at,data) VALUES (?,?,?,?,?,?)",
                    (r.id, r.customer_id, r.signal_type, r.source_url, _dt(r.created_at),
                     r.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_signal_facts(self, signal_type: Optional[str] = None, customer_id: Optional[str] = None,
                         since_days: Optional[int] = 90, limit: int = 1000) -> List[SignalFact]:
        clauses, params = [], []
        if signal_type:
            clauses.append("signal_type=?"); params.append(signal_type)
        if customer_id:
            clauses.append("customer_id=?"); params.append(customer_id)
        cutoff = self._cutoff(since_days)
        if cutoff:
            clauses.append("created_at >= ?"); params.append(cutoff)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM signals {where} ORDER BY created_at DESC LIMIT ?", params).fetchall()
        return [SignalFact.model_validate_json(r["data"]) for r in rows]

    # --- customers (prospects) ---
    @staticmethod
    def _customer_key(prospect: Prospect) -> str:
        dom = (prospect.domain or "").lower()
        if dom:
            return dom
        # customers.domain is UNIQUE — synthesize a stable key for domainless prospects
        return "name:" + (prospect.name_key or prospect.name or prospect.id).strip().lower()

    def upsert_customer(self, prospect: Prospect) -> Prospect:
        key = self._customer_key(prospect)
        with self._lock:
            row = self._conn.execute("SELECT id FROM customers WHERE domain=?", (key,)).fetchone()
            if row:
                prospect.id = row["id"]
            prospect.updated_at = utcnow()
            self._conn.execute(
                "INSERT OR REPLACE INTO customers (id,domain,created_at,data) VALUES (?,?,?,?)",
                (prospect.id, key, _dt(prospect.created_at), prospect.model_dump_json()))
            self._conn.commit()
        return prospect

    def get_customer(self, domain: str) -> Optional[Prospect]:
        row = self._conn.execute("SELECT data FROM customers WHERE domain=?",
                                 ((domain or "").lower(),)).fetchone()
        return Prospect.model_validate_json(row["data"]) if row else None

    def get_customer_by_id(self, customer_id: str) -> Optional[Prospect]:
        row = self._conn.execute("SELECT data FROM customers WHERE id=?", (customer_id,)).fetchone()
        return Prospect.model_validate_json(row["data"]) if row else None

    # --- customer list (per-client serving view) ---
    def replace_customer_list(self, company_id: str, rows: List[CustomerListRow]) -> int:
        rows = list(rows)
        with self._lock:
            self._conn.execute("DELETE FROM customer_list WHERE company_id=?", (company_id,))
            for r in rows:
                r.company_id = company_id
                self._conn.execute(
                    "INSERT OR REPLACE INTO customer_list "
                    "(id,company_id,customer_id,status,created_at,data) VALUES (?,?,?,?,?,?)",
                    (r.id, r.company_id, r.customer_id, r.status, _dt(r.created_at),
                     r.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_customer_list(self, company_id: str, limit: int = 200) -> List[CustomerListRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM customer_list WHERE company_id=? "
                "ORDER BY CAST(json_extract(data,'$.stack_score') AS REAL) DESC, "
                "json_extract(data,'$.pref_score') DESC LIMIT ?",
                (company_id, limit)).fetchall()
        return [CustomerListRow.model_validate_json(r["data"]) for r in rows]

    # --- people (global identity-resolved) ---
    def save_people(self, rows: List[Person]) -> int:
        rows = list(rows)
        with self._lock:
            for p in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO people "
                    "(id,linkedin_url,email,company_domain,created_at,data) VALUES (?,?,?,?,?,?)",
                    (p.id, (p.linkedin_url or "").lower(), (p.email or "").lower(),
                     (p.company_domain or "").lower(), _dt(p.created_at), p.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_people(self, company_domain: Optional[str] = None, limit: int = 2000) -> List[Person]:
        with self._lock:
            if company_domain:
                rows = self._conn.execute(
                    "SELECT data FROM people WHERE company_domain=? ORDER BY created_at DESC LIMIT ?",
                    ((company_domain or "").lower(), limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM people ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [Person.model_validate_json(r["data"]) for r in rows]

    def get_person(self, person_id: str) -> Optional[Person]:
        row = self._conn.execute("SELECT data FROM people WHERE id=?", (person_id,)).fetchone()
        return Person.model_validate_json(row["data"]) if row else None

    # --- customer contacts ---
    def save_customer_contacts(self, rows: List[CustomerContact]) -> int:
        rows = list(rows)
        with self._lock:
            for c in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO customer_contacts "
                    "(id,company_id,customer_id,person_id,created_at,data) VALUES (?,?,?,?,?,?)",
                    (c.id, c.company_id, c.customer_id, c.person_id, _dt(c.created_at),
                     c.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_customer_contacts(self, company_id: str, customer_id: Optional[str] = None,
                              limit: int = 500) -> List[CustomerContact]:
        with self._lock:
            if customer_id:
                rows = self._conn.execute(
                    "SELECT data FROM customer_contacts WHERE company_id=? AND customer_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (company_id, customer_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM customer_contacts WHERE company_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (company_id, limit)).fetchall()
        return [CustomerContact.model_validate_json(r["data"]) for r in rows]

    # --- engagement (touchpoints) ---
    def save_touchpoints(self, rows: List[Touchpoint]) -> int:
        rows = list(rows)
        with self._lock:
            for t in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO touchpoints "
                    "(id,company_id,customer_id,person_id,url_key,created_at,data) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (t.id, t.company_id, t.customer_id, t.person_id, (t.url or t.id),
                     _dt(t.created_at), t.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_touchpoints(self, company_id: str, person_id: Optional[str] = None,
                        limit: int = 500) -> List[Touchpoint]:
        with self._lock:
            if person_id:
                rows = self._conn.execute(
                    "SELECT data FROM touchpoints WHERE company_id=? AND person_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (company_id, person_id, limit)).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM touchpoints WHERE company_id=? "
                    "ORDER BY created_at DESC LIMIT ?", (company_id, limit)).fetchall()
        return [Touchpoint.model_validate_json(r["data"]) for r in rows]

    # ======================================================================= #
    # Event outreach                                                          #
    # ======================================================================= #

    # --- events whose state changes (registration status, scanned) ---
    def upsert_raw_event(self, event: RawEvent) -> RawEvent:
        key_url = event.url or event.id   # same guard as _save_raw_url: blank urls must not collide
        with self._lock:
            row = self._conn.execute("SELECT id FROM raw_events WHERE url=?",
                                     (key_url,)).fetchone()
            if row:
                event.id = row["id"]
            self._conn.execute(
                "INSERT OR REPLACE INTO raw_events (id,url,source,created_at,data) "
                "VALUES (?,?,?,?,?)",
                (event.id, key_url, event.source, self._ts(event), event.model_dump_json()))
            self._conn.commit()
        return event

    def get_raw_event(self, event_id: str) -> Optional[RawEvent]:
        with self._lock:
            r = self._conn.execute("SELECT data FROM raw_events WHERE id=?",
                                   (event_id,)).fetchone()
        return RawEvent.model_validate_json(r["data"]) if r else None

    def get_raw_event_by_url(self, url: str) -> Optional[RawEvent]:
        with self._lock:
            r = self._conn.execute("SELECT data FROM raw_events WHERE url=?", (url,)).fetchone()
        return RawEvent.model_validate_json(r["data"]) if r else None

    def get_company_by_token(self, token: str) -> Optional[Client]:
        if not token:
            return None
        with self._lock:
            # `data` is the whole serialized Client; the token lives in its own `data` dict.
            r = self._conn.execute(
                "SELECT data FROM companies WHERE json_extract(data,'$.data.workspace_token')=? "
                "LIMIT 1", (token,)).fetchone()
        return Client.model_validate_json(r["data"]) if r else None

    def get_person_by_linkedin(self, linkedin_url: str) -> Optional[Person]:
        key = (linkedin_url or "").strip().lower()
        if not key:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT data FROM people WHERE lower(linkedin_url)=? LIMIT 1", (key,)).fetchone()
        return Person.model_validate_json(r["data"]) if r else None

    # --- per-client event registrations ---
    def upsert_event_registration(self, reg: EventRegistration) -> EventRegistration:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM event_registrations WHERE company_id=? AND event_id=?",
                (reg.company_id, reg.event_id)).fetchone()
            if row:
                reg.id = row["id"]
            reg.updated_at = utcnow()
            self._conn.execute(
                "INSERT OR REPLACE INTO event_registrations "
                "(id,company_id,event_id,status,created_at,data) VALUES (?,?,?,?,?,?)",
                (reg.id, reg.company_id, reg.event_id, reg.approval_status,
                 _dt(reg.created_at), reg.model_dump_json()))
            self._conn.commit()
        return reg

    def get_event_registration(self, company_id: str, event_id: str) -> Optional[EventRegistration]:
        with self._lock:
            r = self._conn.execute(
                "SELECT data FROM event_registrations WHERE company_id=? AND event_id=?",
                (company_id, event_id)).fetchone()
        return EventRegistration.model_validate_json(r["data"]) if r else None

    def get_event_registrations(self, company_id: str, limit: int = 500) -> List[EventRegistration]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM event_registrations WHERE company_id=? "
                "ORDER BY created_at DESC LIMIT ?", (company_id, limit)).fetchall()
        return [EventRegistration.model_validate_json(r["data"]) for r in rows]

    # --- connected accounts ---
    def upsert_connection(self, conn: Connection) -> Connection:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM connections WHERE company_id=? AND provider=?",
                (conn.company_id, conn.provider)).fetchone()
            if row:
                conn.id = row["id"]
            conn.updated_at = utcnow()
            self._conn.execute(
                "INSERT OR REPLACE INTO connections "
                "(id,company_id,provider,status,updated_at,data) VALUES (?,?,?,?,?,?)",
                (conn.id, conn.company_id, conn.provider, conn.status, _dt(conn.updated_at),
                 conn.model_dump_json()))
            self._conn.commit()
        return conn

    def get_connection(self, company_id: str, provider: str) -> Optional[Connection]:
        with self._lock:
            r = self._conn.execute(
                "SELECT data FROM connections WHERE company_id=? AND provider=?",
                (company_id, provider)).fetchone()
        return Connection.model_validate_json(r["data"]) if r else None

    def list_connections(self, company_id: str) -> List[Connection]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM connections WHERE company_id=? ORDER BY provider",
                (company_id,)).fetchall()
        return [Connection.model_validate_json(r["data"]) for r in rows]

    def delete_connection(self, company_id: str, provider: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM connections WHERE company_id=? AND provider=?",
                (company_id, provider))
            self._conn.commit()
            return cur.rowcount > 0

    # --- event contacts (list + queue) ---
    def save_event_contacts(self, rows: List[EventContact]) -> int:
        rows = list(rows)
        n = 0
        with self._lock:
            for e in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO event_contacts "
                    "(id,company_id,event_id,contact_key,person_id,status,priority,"
                    "lease_expires_at,created_at,email,verdict,email_status,data) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (e.id, e.company_id, e.event_id, e.contact_key, e.person_id, e.status,
                     e.priority, _dt(e.lease_expires_at), _dt(e.created_at),
                     e.email or None, e.verdict or None, e.email_status or None,
                     e.model_dump_json()))
                n += cur.rowcount
            self._conn.commit()
        return n

    def update_event_contacts(self, rows: List[EventContact]) -> int:
        rows = list(rows)
        with self._lock:
            for e in rows:
                e.updated_at = utcnow()
                self._conn.execute(
                    "UPDATE event_contacts SET person_id=?, status=?, priority=?, "
                    "lease_expires_at=?, email=?, verdict=?, email_status=?, data=? "
                    "WHERE id=?",
                    (e.person_id, e.status, e.priority, _dt(e.lease_expires_at),
                     e.email or None, e.verdict or None, e.email_status or None,
                     e.model_dump_json(), e.id))
            self._conn.commit()
        return len(rows)

    def get_event_contact(self, contact_id: str) -> Optional[EventContact]:
        with self._lock:
            r = self._conn.execute("SELECT data FROM event_contacts WHERE id=?",
                                   (contact_id,)).fetchone()
        return EventContact.model_validate_json(r["data"]) if r else None

    def event_contact_counts(self, company_id: str) -> dict:
        """Same three aggregates as the Postgres store. See the note there for why."""
        with self._lock:
            buckets = self._conn.execute(
                "SELECT status, verdict, "
                "       (email IS NOT NULL AND email <> '') AS has_email, COUNT(*) AS n "
                "FROM event_contacts WHERE company_id=? GROUP BY 1,2,3",
                (company_id,)).fetchall()
            held = self._conn.execute(
                "SELECT COALESCE(json_extract(data,'$.last_error'),'') AS why, COUNT(*) AS n "
                "FROM event_contacts WHERE company_id=? "
                "  AND status='skipped' AND verdict='target' "
                "GROUP BY 1 ORDER BY n DESC LIMIT 12", (company_id,)).fetchall()
            titles = self._conn.execute(
                "SELECT COALESCE(json_extract(data,'$.title'),'unknown') AS title, COUNT(*) AS n "
                "FROM event_contacts WHERE company_id=? AND verdict='reject' "
                "GROUP BY 1 ORDER BY n DESC LIMIT 6", (company_id,)).fetchall()
        return {
            "buckets": [{"status": r["status"] or "", "verdict": r["verdict"] or "",
                         "has_email": bool(r["has_email"]), "n": int(r["n"])} for r in buckets],
            "held": {((r["why"] or "held back").split(":")[0]): int(r["n"]) for r in held},
            "reject_titles": [(r["title"] or "unknown", int(r["n"])) for r in titles],
        }

    def contacts_per_event(self, company_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, COUNT(*) AS n FROM event_contacts WHERE company_id=? "
                "GROUP BY event_id", (company_id,)).fetchall()
        return {r["event_id"]: int(r["n"]) for r in rows}

    def get_event_contacts(self, company_id: str, event_id: Optional[str] = None,
                           status: Optional[str] = None, limit: int = 500) -> List[EventContact]:
        sql = "SELECT data FROM event_contacts WHERE company_id=?"
        params: list = [company_id]
        if event_id:
            sql += " AND event_id=?"
            params.append(event_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [EventContact.model_validate_json(r["data"]) for r in rows]

    def count_event_contacts(self, company_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM event_contacts WHERE company_id=? "
                "GROUP BY status", (company_id,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def lease_event_contacts(self, company_id: str, leased_by: str, limit: int = 25,
                             lease_seconds: int = 600) -> List[EventContact]:
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        claimed: List[EventContact] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM event_contacts WHERE company_id=? AND status='queued' "
                "ORDER BY priority DESC, created_at ASC LIMIT ?", (company_id, limit)).fetchall()
            for r in rows:
                e = EventContact.model_validate_json(r["data"])
                e.status, e.leased_by, e.lease_expires_at = "reading", leased_by, expires
                e.attempts += 1
                e.updated_at = now
                cur = self._conn.execute(
                    "UPDATE event_contacts SET status='reading', lease_expires_at=?, data=? "
                    "WHERE id=? AND status='queued'",
                    (_dt(expires), e.model_dump_json(), e.id))
                if cur.rowcount:
                    claimed.append(e)
            self._conn.commit()
        return claimed

    def release_expired_leases(self, company_id: Optional[str] = None) -> int:
        now = utcnow()
        sql = ("SELECT data FROM event_contacts WHERE status='reading' "
               "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?")
        params: list = [_dt(now)]
        if company_id:
            sql += " AND company_id=?"
            params.append(company_id)
        freed = 0
        with self._lock:
            for r in self._conn.execute(sql, tuple(params)).fetchall():
                e = EventContact.model_validate_json(r["data"])
                e.status, e.leased_by, e.lease_expires_at = "queued", "", None
                e.updated_at = now
                cur = self._conn.execute(
                    "UPDATE event_contacts SET status='queued', lease_expires_at=NULL, data=? "
                    "WHERE id=? AND status='reading'", (e.model_dump_json(), e.id))
                freed += cur.rowcount
            self._conn.commit()
        return freed

    # --- send ledger ---
    def save_outreach_messages(self, rows: List[OutreachMessage]) -> int:
        rows = list(rows)
        with self._lock:
            for m in rows:
                m.email_key = m.email_key or norm_email(m.email)
                self._conn.execute(
                    "INSERT OR REPLACE INTO outreach_messages "
                    "(id,company_id,event_id,person_id,email_key,status,created_at,data) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (m.id, m.company_id, m.event_id, m.person_id, m.email_key, m.status,
                     _dt(m.created_at), m.model_dump_json()))
            self._conn.commit()
        return len(rows)

    def get_outreach_messages(self, company_id: str, status: Optional[str] = None,
                              limit: int = 500) -> List[OutreachMessage]:
        sql = "SELECT data FROM outreach_messages WHERE company_id=?"
        params: list = [company_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [OutreachMessage.model_validate_json(r["data"]) for r in rows]

    def sent_addresses_since(self, company_id: str, since) -> set:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT lower(email_key) AS e FROM outreach_messages "
                "WHERE company_id=? AND status='sent' AND created_at >= ?",
                (company_id, _dt(since))).fetchall()
        return {r["e"] for r in rows if r["e"]}

    def contacts_by_email(self, company_id: str, emails) -> List[EventContact]:
        wanted = [e.lower() for e in emails if e]
        if not wanted:
            return []
        marks = ",".join("?" for _ in wanted)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM event_contacts WHERE company_id=? "
                f"AND lower(email) IN ({marks})",
                (company_id, *wanted)).fetchall()
        return [EventContact.model_validate_json(r["data"]) for r in rows]

    def drafted_messages_by_contact(self, company_id: str) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM outreach_messages WHERE company_id=? AND status='drafted' "
                "ORDER BY created_at DESC LIMIT 5000", (company_id,)).fetchall()
        out: dict = {}
        for r in rows:
            m = OutreachMessage.model_validate_json(r["data"])
            if m.event_contact_id and m.event_contact_id not in out and m.subject:
                out[m.event_contact_id] = m
        return out

    def count_sent_since(self, company_id: str, since) -> int:
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=? "
                "AND status='sent' AND created_at >= ?",
                (company_id, _dt(since))).fetchone()
        return int(r["n"]) if r else 0

    def sent_messages_since(self, company_id: str, since,
                            limit: int = 200) -> List[OutreachMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM outreach_messages WHERE company_id=? AND status='sent' "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT ?",
                (company_id, _dt(since), limit)).fetchall()
        return [OutreachMessage.model_validate_json(r["data"]) for r in rows]

    def has_contacted(self, company_id: str, email: str) -> bool:
        key = norm_email(email)
        if not key:
            return False
        # Only real outbound counts — see the Postgres implementation for why `skipped` doesn't.
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM outreach_messages WHERE company_id=? AND email_key=? "
                "AND status IN ('drafted','sent') LIMIT 1", (company_id, key)).fetchone()
        return bool(r)

    def count_outreach_messages(self, company_id: str, since_minutes: int,
                                statuses: tuple = ("drafted", "sent")) -> int:
        since = _dt(utcnow() - timedelta(minutes=since_minutes))
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            r = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=? "
                f"AND status IN ({marks}) AND created_at >= ?",
                (company_id, *statuses, since)).fetchone()
        return int(r["n"]) if r else 0

    # --- manual exclusions ---
    def save_do_not_contact(self, rows: List[DoNotContact]) -> int:
        rows = list(rows)
        n = 0
        with self._lock:
            for d in rows:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO do_not_contact (id,company_id,value,created_at,data) "
                    "VALUES (?,?,?,?,?)",
                    (d.id, d.company_id, d.value, _dt(d.created_at), d.model_dump_json()))
                n += cur.rowcount
            self._conn.commit()
        return n

    def get_do_not_contact(self, company_id: str, limit: int = 2000) -> List[DoNotContact]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM do_not_contact WHERE company_id=? "
                "ORDER BY created_at DESC LIMIT ?", (company_id, limit)).fetchall()
        return [DoNotContact.model_validate_json(r["data"]) for r in rows]

    def is_do_not_contact(self, company_id: str, email: str) -> bool:
        key = norm_email(email)
        if not key:
            return False
        with self._lock:
            r = self._conn.execute(
                "SELECT 1 FROM do_not_contact WHERE company_id=? AND value IN (?,?) LIMIT 1",
                (company_id, key, email_domain(key))).fetchone()
        return bool(r)

    # --- app-wide settings ---
    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            r = self._conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (key,value,updated_at) VALUES (?,?,?)",
                (key, value, _dt(utcnow())))
            self._conn.commit()

    def raw_scan(self, table: str, limit: int = 100000) -> List[dict]:
        import json
        with self._lock:
            rows = self._conn.execute(f"SELECT data FROM {table} LIMIT ?", (limit,)).fetchall()
        return [json.loads(r["data"]) for r in rows]
