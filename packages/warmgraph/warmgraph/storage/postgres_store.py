from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

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


class PostgresStore(Store):
    """Hosted Postgres store (Neon) — same Store interface as SQLite, JSONB `data` columns,
    pgvector-ready. Safe for Neon's pooled endpoint: autocommit + prepared statements off."""

    def __init__(self, dsn: str):
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=10,
            open=True,
            # Neon (serverless) closes idle server-side connections; a long scrape can outlast one.
            # `check` validates (and transparently replaces) a connection on checkout so writes after
            # a slow step don't hit a dead SSL socket; `max_idle` recycles idle connections early.
            check=ConnectionPool.check_connection,
            max_idle=60.0,
            kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
        )
        self.init_schema()

    def _cursor_op(self, fn: Callable, retries: int = 3):
        """Run fn(cursor) on a pooled connection, retrying on a dead connection. Neon closes idle
        server-side connections during long steps (e.g. a 90s scrape); a broken connection is
        discarded by the pool on error, so a retry transparently gets a fresh one."""
        last: Optional[Exception] = None
        for _ in range(retries):
            try:
                with self.pool.connection() as conn, conn.cursor() as cur:
                    return fn(cur)
            except OperationalError as e:
                last = e
                try:
                    self.pool.check()   # ask the pool to drop/rebuild dead connections
                except Exception:
                    pass
        raise last  # type: ignore[misc]

    def _run(self, sql: str, params: tuple = (), fetch: Optional[str] = None):
        def op(cur):
            cur.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return None
        return self._cursor_op(op)

    def init_schema(self) -> None:
        self._run(
            "CREATE TABLE IF NOT EXISTS ci_reports "
            "(id TEXT PRIMARY KEY, url TEXT, created_at TIMESTAMPTZ, data JSONB)"
        )
        self._run(
            "CREATE TABLE IF NOT EXISTS posts ("
            "id TEXT PRIMARY KEY, subject_domain TEXT, platform TEXT, external_id TEXT, "
            "posted_at TIMESTAMPTZ, created_at TIMESTAMPTZ, data JSONB, "
            "UNIQUE(platform, external_id))"
        )
        # legacy `signals` (social Signal model) → `social_signals`; `signals` is now SignalFact below.
        for table in ("social_signals", "events", "customer_leads", "accounts", "lead_feedback"):
            self._run(
                f"CREATE TABLE IF NOT EXISTS {table} "
                "(id TEXT PRIMARY KEY, subject_domain TEXT, created_at TIMESTAMPTZ, data JSONB)"
            )
        self._run(
            "CREATE TABLE IF NOT EXISTS company_leads "
            "(id TEXT PRIMARY KEY, subject_domain TEXT, signal_type TEXT, created_at TIMESTAMPTZ, data JSONB)"
        )
        self._run(
            "CREATE TABLE IF NOT EXISTS contacts "
            "(id TEXT PRIMARY KEY, subject_domain TEXT, company_domain TEXT, created_at TIMESTAMPTZ, data JSONB)"
        )
        self._run(
            "CREATE TABLE IF NOT EXISTS profiles "
            "(domain TEXT PRIMARY KEY, url TEXT, created_at TIMESTAMPTZ, refreshed_at TIMESTAMPTZ, data JSONB)"
        )
        self._init_normalized()

    # --- Normalized relational schema (clients → raw → signals → customers → people) ---
    def _init_normalized(self) -> None:
        r = self._run
        r("CREATE TABLE IF NOT EXISTS companies "
          "(id TEXT PRIMARY KEY, domain TEXT UNIQUE, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS company_intel "
          "(id TEXT PRIMARY KEY, company_id TEXT, domain TEXT, refreshed_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS raw_job_postings "
          "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS raw_funding_news "
          "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS raw_social_posts "
          "(id TEXT PRIMARY KEY, platform TEXT, external_id TEXT, url TEXT, created_at TIMESTAMPTZ, data JSONB, "
          "UNIQUE(platform, external_id))")
        r("CREATE TABLE IF NOT EXISTS raw_events "
          "(id TEXT PRIMARY KEY, url TEXT UNIQUE, source TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS raw_people "
          "(id TEXT PRIMARY KEY, source TEXT, linkedin_url TEXT, email TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS signals "
          "(id TEXT PRIMARY KEY, customer_id TEXT, signal_type TEXT, source_url TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS customers "
          "(id TEXT PRIMARY KEY, domain TEXT UNIQUE, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS customer_list "
          "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, status TEXT, created_at TIMESTAMPTZ, data JSONB, "
          "UNIQUE(company_id, customer_id))")
        r("CREATE TABLE IF NOT EXISTS people "
          "(id TEXT PRIMARY KEY, linkedin_url TEXT, email TEXT, company_domain TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS customer_contacts "
          "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, person_id TEXT, created_at TIMESTAMPTZ, data JSONB, "
          "UNIQUE(company_id, customer_id, person_id))")
        r("CREATE TABLE IF NOT EXISTS touchpoints "
          "(id TEXT PRIMARY KEY, company_id TEXT, customer_id TEXT, person_id TEXT, url_key TEXT, "
          "created_at TIMESTAMPTZ, data JSONB, UNIQUE(company_id, person_id, url_key))")
        self._init_outreach()

    # --- Event outreach (connections → event_contacts queue → send ledger) ---
    def _init_outreach(self) -> None:
        r = self._run
        r("CREATE TABLE IF NOT EXISTS connections "
          "(id TEXT PRIMARY KEY, company_id TEXT, provider TEXT, status TEXT, "
          "updated_at TIMESTAMPTZ, data JSONB, UNIQUE(company_id, provider))")
        r("CREATE TABLE IF NOT EXISTS event_contacts "
          "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, contact_key TEXT, person_id TEXT, "
          "status TEXT, priority INTEGER DEFAULT 0, lease_expires_at TIMESTAMPTZ, "
          "created_at TIMESTAMPTZ, data JSONB, UNIQUE(company_id, event_id, contact_key))")
        r("CREATE TABLE IF NOT EXISTS outreach_messages "
          "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, person_id TEXT, email_key TEXT, "
          "status TEXT, created_at TIMESTAMPTZ, data JSONB)")
        r("CREATE TABLE IF NOT EXISTS event_registrations "
          "(id TEXT PRIMARY KEY, company_id TEXT, event_id TEXT, status TEXT, "
          "created_at TIMESTAMPTZ, data JSONB, UNIQUE(company_id, event_id))")
        r("CREATE TABLE IF NOT EXISTS app_settings "
          "(key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ)")
        r("CREATE TABLE IF NOT EXISTS do_not_contact "
          "(id TEXT PRIMARY KEY, company_id TEXT, value TEXT, created_at TIMESTAMPTZ, data JSONB, "
          "UNIQUE(company_id, value))")
        # email / verdict / email_status were only inside the JSONB blob, so every question about
        # them — how many are a fit, how many have an address, which are ready — meant a scan with
        # JSONB extraction per row. As real columns they are indexable, and the funnel becomes an
        # index scan instead of reading the table.
        r("ALTER TABLE event_contacts ADD COLUMN IF NOT EXISTS email TEXT")
        r("ALTER TABLE event_contacts ADD COLUMN IF NOT EXISTS verdict TEXT")
        r("ALTER TABLE event_contacts ADD COLUMN IF NOT EXISTS email_status TEXT")
        # Backfill once, for rows written before the columns existed. Bounded by the WHERE, so it
        # is a no-op on every start after the first.
        r("UPDATE event_contacts SET email = data->>'email', verdict = data->>'verdict', "
          "email_status = data->>'email_status' "
          "WHERE email IS NULL AND data ? 'email'")

        # The queue pop and the suppression lookup are the two hot paths.
        r("CREATE INDEX IF NOT EXISTS ix_ec_queue ON event_contacts "
          "(company_id, status, priority DESC, created_at)")
        # The funnel counts by these three together.
        r("CREATE INDEX IF NOT EXISTS ix_ec_funnel ON event_contacts "
          "(company_id, status, verdict)")
        r("CREATE INDEX IF NOT EXISTS ix_ec_email ON event_contacts (company_id, lower(email))")
        r("CREATE INDEX IF NOT EXISTS ix_om_email ON outreach_messages (company_id, email_key)")
        r("CREATE INDEX IF NOT EXISTS ix_om_window ON outreach_messages "
          "(company_id, status, created_at)")
        # Workspace-token resolution runs on every extension request; without this it is a
        # sequential scan over `companies` that deserializes every row.
        r("DROP INDEX IF EXISTS ix_companies_token")   # earlier build indexed the wrong path
        r("CREATE INDEX IF NOT EXISTS ix_companies_wstoken ON companies "
          "((data->'data'->>'workspace_token'))")
        # Identity resolution for event attendees keys off the LinkedIn URL.
        r("CREATE INDEX IF NOT EXISTS ix_people_linkedin ON people (lower(linkedin_url))")

    # --- CI ---
    def save_ci_report(
        self, report: CompetitiveIntelligenceReport
    ) -> CompetitiveIntelligenceReport:
        self._run(
            "INSERT INTO ci_reports (id,url,created_at,data) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET url=EXCLUDED.url, data=EXCLUDED.data",
            (report.id, report.url, report.created_at, Jsonb(report.model_dump(mode="json"))),
        )
        return report

    def get_ci_report(self, report_id: str) -> Optional[CompetitiveIntelligenceReport]:
        row = self._run("SELECT data FROM ci_reports WHERE id=%s", (report_id,), fetch="one")
        return CompetitiveIntelligenceReport.model_validate(row["data"]) if row else None

    # --- posts (deduped on platform+external_id) ---
    def save_posts(self, posts: List[Post]) -> int:
        def op(cur):
            inserted = 0
            for p in posts:
                ext = p.external_id or p.id
                cur.execute(
                    "INSERT INTO posts (id,subject_domain,platform,external_id,posted_at,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (platform, external_id) DO NOTHING",
                    (p.id, p.subject_domain, p.platform, ext, p.posted_at, p.created_at,
                     Jsonb(p.model_dump(mode="json"))),
                )
                inserted += cur.rowcount or 0
            return inserted
        return self._cursor_op(op)

    def get_posts(self, subject_domain: str, platform: Optional[str] = None,
                  limit: int = 200) -> List[Post]:
        if platform:
            rows = self._run(
                "SELECT data FROM posts WHERE subject_domain=%s AND platform=%s "
                "ORDER BY posted_at DESC NULLS LAST LIMIT %s",
                (subject_domain, platform, limit), fetch="all")
        else:
            rows = self._run(
                "SELECT data FROM posts WHERE subject_domain=%s "
                "ORDER BY posted_at DESC NULLS LAST LIMIT %s",
                (subject_domain, limit), fetch="all")
        return [Post.model_validate(r["data"]) for r in (rows or [])]

    def get_recent_posts(self, since_days: int = 90, limit: int = 1000) -> List[Post]:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        rows = self._run("SELECT data FROM posts WHERE posted_at >= %s ORDER BY posted_at DESC "
                         "LIMIT %s", (cutoff, limit), fetch="all")
        return [Post.model_validate(r["data"]) for r in (rows or [])]

    # --- generic id-keyed JSONB tables (signals / events / customer_leads) ---
    def _save_rows(self, table: str, rows) -> int:
        rows = list(rows)

        def op(cur):
            for obj in rows:
                cur.execute(
                    f"INSERT INTO {table} (id,subject_domain,created_at,data) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    (obj.id, obj.subject_domain, obj.created_at, Jsonb(obj.model_dump(mode="json"))),
                )
            return len(rows)
        return self._cursor_op(op)

    def _get_rows(self, table: str, model, subject_domain: str, limit: int):
        rows = self._run(
            f"SELECT data FROM {table} WHERE subject_domain=%s ORDER BY created_at DESC LIMIT %s",
            (subject_domain, limit), fetch="all")
        return [model.model_validate(r["data"]) for r in (rows or [])]

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

        def op(cur):
            for c in contacts:
                cur.execute(
                    "INSERT INTO contacts (id,subject_domain,company_domain,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    (c.id, c.subject_domain, (c.company_domain or "").lower(), c.created_at,
                     Jsonb(c.model_dump(mode="json"))),
                )
            return len(contacts)
        return self._cursor_op(op)

    def get_contacts(self, subject_domain: str, limit: int = 1000) -> List[Contact]:
        return self._get_rows("contacts", Contact, subject_domain, limit)

    def get_contacts_for_company(self, company_domain: str, limit: int = 20) -> List[Contact]:
        rows = self._run(
            "SELECT data FROM contacts WHERE company_domain=%s ORDER BY created_at DESC LIMIT %s",
            ((company_domain or "").lower(), limit), fetch="all")
        return [Contact.model_validate(r["data"]) for r in (rows or [])]

    def save_profile(self, profile: Profile) -> Profile:
        self._run(
            "INSERT INTO profiles (domain,url,created_at,refreshed_at,data) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (domain) DO UPDATE SET url=EXCLUDED.url, refreshed_at=EXCLUDED.refreshed_at, "
            "data=EXCLUDED.data",
            (profile.domain, profile.url, profile.created_at, profile.refreshed_at,
             Jsonb(profile.model_dump(mode="json"))),
        )
        return profile

    def get_profile(self, domain: str) -> Optional[Profile]:
        row = self._run("SELECT data FROM profiles WHERE domain=%s", (domain,), fetch="one")
        return Profile.model_validate(row["data"]) if row else None

    def save_company_leads(self, leads: List[CompanyLead]) -> int:
        def op(cur):
            for L in leads:
                cur.execute(
                    "INSERT INTO company_leads (id,subject_domain,signal_type,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    (L.id, L.subject_domain, L.signal_type, L.created_at,
                     Jsonb(L.model_dump(mode="json"))),
                )
            return len(leads)
        return self._cursor_op(op)

    def get_company_leads(self, subject_domain, signal_type=None, limit=500) -> List[CompanyLead]:
        if signal_type:
            rows = self._run(
                "SELECT data FROM company_leads WHERE subject_domain=%s AND signal_type=%s "
                "ORDER BY created_at DESC LIMIT %s", (subject_domain, signal_type, limit), fetch="all")
        else:
            rows = self._run(
                "SELECT data FROM company_leads WHERE subject_domain=%s ORDER BY created_at DESC "
                "LIMIT %s", (subject_domain, limit), fetch="all")
        return [CompanyLead.model_validate(r["data"]) for r in (rows or [])]

    def get_recent_company_leads(self, signal_type: str, limit: int = 400) -> List[CompanyLead]:
        rows = self._run("SELECT data FROM company_leads WHERE signal_type=%s ORDER BY created_at DESC "
                         "LIMIT %s", (signal_type, limit), fetch="all")
        return [CompanyLead.model_validate(r["data"]) for r in (rows or [])]

    # ===================================================================== #
    # Normalized relational schema (see entities.py)                        #
    # ===================================================================== #
    @staticmethod
    def _ts(obj):
        for attr in ("created_at", "scraped_at", "refreshed_at"):
            v = getattr(obj, attr, None)
            if v is not None:
                return v
        return utcnow()

    @staticmethod
    def _cutoff(since_days: Optional[int]):
        if not since_days:
            return None
        return datetime.now(timezone.utc) - timedelta(days=since_days)

    def _get_recent(self, table, model, since_days, limit, order="created_at"):
        cutoff = self._cutoff(since_days)
        if cutoff:
            rows = self._run(
                f"SELECT data FROM {table} WHERE created_at >= %s ORDER BY {order} DESC LIMIT %s",
                (cutoff, limit), fetch="all")
        else:
            rows = self._run(f"SELECT data FROM {table} ORDER BY {order} DESC LIMIT %s",
                             (limit,), fetch="all")
        return [model.model_validate(r["data"]) for r in (rows or [])]

    # --- clients (companies) ---
    def upsert_company(self, client: Client) -> Client:
        dom = (client.domain or "").lower()
        existing = self._run("SELECT id FROM companies WHERE domain=%s", (dom,), fetch="one")
        if existing:
            client.id = existing["id"]
        self._run(
            "INSERT INTO companies (id,domain,created_at,data) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET domain=EXCLUDED.domain, data=EXCLUDED.data",
            (client.id, dom, client.created_at, Jsonb(client.model_dump(mode="json"))))
        return client

    def get_company(self, domain: str) -> Optional[Client]:
        r = self._run("SELECT data FROM companies WHERE domain=%s", ((domain or "").lower(),), fetch="one")
        return Client.model_validate(r["data"]) if r else None

    def get_company_by_id(self, company_id: str) -> Optional[Client]:
        r = self._run("SELECT data FROM companies WHERE id=%s", (company_id,), fetch="one")
        return Client.model_validate(r["data"]) if r else None

    # --- intelligence (one row per client domain) ---
    def save_company_intel(self, intel: CompanyIntel) -> CompanyIntel:
        dom = (intel.domain or "").lower()

        def op(cur):
            cur.execute("DELETE FROM company_intel WHERE domain=%s", (dom,))
            cur.execute(
                "INSERT INTO company_intel (id,company_id,domain,refreshed_at,data) "
                "VALUES (%s,%s,%s,%s,%s)",
                (intel.id, intel.company_id, dom, intel.refreshed_at,
                 Jsonb(intel.model_dump(mode="json"))))
            return None
        self._cursor_op(op)
        return intel

    def get_company_intel(self, domain: str) -> Optional[CompanyIntel]:
        r = self._run("SELECT data FROM company_intel WHERE domain=%s ORDER BY refreshed_at DESC LIMIT 1",
                      ((domain or "").lower(),), fetch="one")
        return CompanyIntel.model_validate(r["data"]) if r else None

    # --- raw corpora ---
    def _save_raw_url(self, table: str, rows) -> int:
        rows = list(rows)

        def op(cur):
            n = 0
            for r in rows:
                key_url = r.url or r.id   # avoid false-dedup of blank urls (UNIQUE(url))
                cur.execute(
                    f"INSERT INTO {table} (id,url,source,created_at,data) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (url) DO NOTHING",
                    (r.id, key_url, r.source, self._ts(r), Jsonb(r.model_dump(mode="json"))))
                n += cur.rowcount or 0
            return n
        return self._cursor_op(op)

    def save_raw_job_postings(self, rows: List[RawJobPosting]) -> int:
        return self._save_raw_url("raw_job_postings", rows)

    def save_raw_funding_news(self, rows: List[RawFundingNews]) -> int:
        return self._save_raw_url("raw_funding_news", rows)

    def save_raw_events(self, rows: List[RawEvent]) -> int:
        return self._save_raw_url("raw_events", rows)

    def save_raw_social_posts(self, rows: List[RawSocialPost]) -> int:
        rows = list(rows)

        def op(cur):
            n = 0
            for r in rows:
                ext = r.external_id or r.id
                cur.execute(
                    "INSERT INTO raw_social_posts (id,platform,external_id,url,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (platform, external_id) DO NOTHING",
                    (r.id, r.platform, ext, r.url, self._ts(r), Jsonb(r.model_dump(mode="json"))))
                n += cur.rowcount or 0
            return n
        return self._cursor_op(op)

    def save_raw_people(self, rows: List[RawPerson]) -> int:
        rows = list(rows)

        def op(cur):
            for r in rows:
                cur.execute(
                    "INSERT INTO raw_people (id,source,linkedin_url,email,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    (r.id, r.source, (r.linkedin_url or "").lower(), (r.email or "").lower(),
                     self._ts(r), Jsonb(r.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

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

        def op(cur):
            for r in rows:
                cur.execute(
                    "INSERT INTO signals (id,customer_id,signal_type,source_url,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data",
                    (r.id, r.customer_id, r.signal_type, r.source_url, r.created_at,
                     Jsonb(r.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_signal_facts(self, signal_type: Optional[str] = None, customer_id: Optional[str] = None,
                         since_days: Optional[int] = 90, limit: int = 1000) -> List[SignalFact]:
        clauses, params = [], []
        if signal_type:
            clauses.append("signal_type=%s"); params.append(signal_type)
        if customer_id:
            clauses.append("customer_id=%s"); params.append(customer_id)
        cutoff = self._cutoff(since_days)
        if cutoff:
            clauses.append("created_at >= %s"); params.append(cutoff)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self._run(f"SELECT data FROM signals {where} ORDER BY created_at DESC LIMIT %s",
                         tuple(params), fetch="all")
        return [SignalFact.model_validate(r["data"]) for r in (rows or [])]

    # --- customers (prospects) ---
    @staticmethod
    def _customer_key(prospect: Prospect) -> str:
        dom = (prospect.domain or "").lower()
        if dom:
            return dom
        return "name:" + (prospect.name_key or prospect.name or prospect.id).strip().lower()

    def upsert_customer(self, prospect: Prospect) -> Prospect:
        key = self._customer_key(prospect)
        existing = self._run("SELECT id FROM customers WHERE domain=%s", (key,), fetch="one")
        if existing:
            prospect.id = existing["id"]
        prospect.updated_at = utcnow()
        self._run(
            "INSERT INTO customers (id,domain,created_at,data) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET domain=EXCLUDED.domain, data=EXCLUDED.data",
            (prospect.id, key, prospect.created_at, Jsonb(prospect.model_dump(mode="json"))))
        return prospect

    def get_customer(self, domain: str) -> Optional[Prospect]:
        r = self._run("SELECT data FROM customers WHERE domain=%s", ((domain or "").lower(),), fetch="one")
        return Prospect.model_validate(r["data"]) if r else None

    def get_customer_by_id(self, customer_id: str) -> Optional[Prospect]:
        r = self._run("SELECT data FROM customers WHERE id=%s", (customer_id,), fetch="one")
        return Prospect.model_validate(r["data"]) if r else None

    # --- customer list (per-client serving view) ---
    def replace_customer_list(self, company_id: str, rows: List[CustomerListRow]) -> int:
        rows = list(rows)

        def op(cur):
            cur.execute("DELETE FROM customer_list WHERE company_id=%s", (company_id,))
            for r in rows:
                r.company_id = company_id
                cur.execute(
                    "INSERT INTO customer_list (id,company_id,customer_id,status,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (company_id, customer_id) DO UPDATE SET "
                    "status=EXCLUDED.status, data=EXCLUDED.data",
                    (r.id, r.company_id, r.customer_id, r.status, r.created_at,
                     Jsonb(r.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_customer_list(self, company_id: str, limit: int = 200) -> List[CustomerListRow]:
        rows = self._run(
            "SELECT data FROM customer_list WHERE company_id=%s "
            "ORDER BY (data->>'stack_score')::float DESC NULLS LAST, "
            "(data->>'pref_score')::float DESC NULLS LAST LIMIT %s",
            (company_id, limit), fetch="all")
        return [CustomerListRow.model_validate(r["data"]) for r in (rows or [])]

    # --- people (global identity-resolved) ---
    def save_people(self, rows: List[Person]) -> int:
        rows = list(rows)

        def op(cur):
            for p in rows:
                cur.execute(
                    "INSERT INTO people (id,linkedin_url,email,company_domain,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
                    "company_domain=EXCLUDED.company_domain, data=EXCLUDED.data",
                    (p.id, (p.linkedin_url or "").lower(), (p.email or "").lower(),
                     (p.company_domain or "").lower(), p.created_at,
                     Jsonb(p.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_people(self, company_domain: Optional[str] = None, limit: int = 2000) -> List[Person]:
        if company_domain:
            rows = self._run(
                "SELECT data FROM people WHERE company_domain=%s ORDER BY created_at DESC LIMIT %s",
                ((company_domain or "").lower(), limit), fetch="all")
        else:
            rows = self._run("SELECT data FROM people ORDER BY created_at DESC LIMIT %s",
                             (limit,), fetch="all")
        return [Person.model_validate(r["data"]) for r in (rows or [])]

    def get_person(self, person_id: str) -> Optional[Person]:
        r = self._run("SELECT data FROM people WHERE id=%s", (person_id,), fetch="one")
        return Person.model_validate(r["data"]) if r else None

    # --- customer contacts ---
    def save_customer_contacts(self, rows: List[CustomerContact]) -> int:
        rows = list(rows)

        def op(cur):
            for c in rows:
                cur.execute(
                    "INSERT INTO customer_contacts "
                    "(id,company_id,customer_id,person_id,created_at,data) VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (company_id, customer_id, person_id) DO UPDATE SET data=EXCLUDED.data",
                    (c.id, c.company_id, c.customer_id, c.person_id, c.created_at,
                     Jsonb(c.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_customer_contacts(self, company_id: str, customer_id: Optional[str] = None,
                              limit: int = 500) -> List[CustomerContact]:
        if customer_id:
            rows = self._run(
                "SELECT data FROM customer_contacts WHERE company_id=%s AND customer_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (company_id, customer_id, limit), fetch="all")
        else:
            rows = self._run(
                "SELECT data FROM customer_contacts WHERE company_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (company_id, limit), fetch="all")
        return [CustomerContact.model_validate(r["data"]) for r in (rows or [])]

    # --- engagement (touchpoints) ---
    def save_touchpoints(self, rows: List[Touchpoint]) -> int:
        rows = list(rows)

        def op(cur):
            for t in rows:
                cur.execute(
                    "INSERT INTO touchpoints "
                    "(id,company_id,customer_id,person_id,url_key,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (company_id, person_id, url_key) DO UPDATE SET data=EXCLUDED.data",
                    (t.id, t.company_id, t.customer_id, t.person_id, (t.url or t.id),
                     t.created_at, Jsonb(t.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_touchpoints(self, company_id: str, person_id: Optional[str] = None,
                        limit: int = 500) -> List[Touchpoint]:
        if person_id:
            rows = self._run(
                "SELECT data FROM touchpoints WHERE company_id=%s AND person_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (company_id, person_id, limit), fetch="all")
        else:
            rows = self._run(
                "SELECT data FROM touchpoints WHERE company_id=%s "
                "ORDER BY created_at DESC LIMIT %s", (company_id, limit), fetch="all")
        return [Touchpoint.model_validate(r["data"]) for r in (rows or [])]

    # ======================================================================= #
    # Event outreach                                                          #
    # ======================================================================= #

    # --- events whose state changes (registration status, scanned) ---
    def upsert_raw_event(self, event: RawEvent) -> RawEvent:
        key_url = event.url or event.id   # same guard as _save_raw_url: blank urls must not collide
        existing = self._run("SELECT id FROM raw_events WHERE url=%s", (key_url,), fetch="one")
        if existing:
            event.id = existing["id"]
        self._run(
            "INSERT INTO raw_events (id,url,source,created_at,data) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (url) DO UPDATE SET data=EXCLUDED.data, source=EXCLUDED.source",
            (event.id, key_url, event.source, self._ts(event),
             Jsonb(event.model_dump(mode="json"))))
        return event

    def get_raw_event(self, event_id: str) -> Optional[RawEvent]:
        r = self._run("SELECT data FROM raw_events WHERE id=%s", (event_id,), fetch="one")
        return RawEvent.model_validate(r["data"]) if r else None

    def get_raw_event_by_url(self, url: str) -> Optional[RawEvent]:
        r = self._run("SELECT data FROM raw_events WHERE url=%s", (url,), fetch="one")
        return RawEvent.model_validate(r["data"]) if r else None

    def get_company_by_token(self, token: str) -> Optional[Client]:
        if not token:
            return None
        # The `data` column is the whole serialized Client, whose own `data` dict holds the
        # token — hence the nested path rather than a top-level key.
        r = self._run("SELECT data FROM companies WHERE data->'data'->>'workspace_token' = %s "
                      "LIMIT 1", (token,), fetch="one")
        return Client.model_validate(r["data"]) if r else None

    def get_person_by_linkedin(self, linkedin_url: str) -> Optional[Person]:
        key = (linkedin_url or "").strip().lower()
        if not key:
            return None
        r = self._run("SELECT data FROM people WHERE lower(linkedin_url)=%s LIMIT 1",
                      (key,), fetch="one")
        return Person.model_validate(r["data"]) if r else None

    # --- per-client event registrations ---
    def upsert_event_registration(self, reg: EventRegistration) -> EventRegistration:
        existing = self._run("SELECT id FROM event_registrations WHERE company_id=%s AND event_id=%s",
                             (reg.company_id, reg.event_id), fetch="one")
        if existing:
            reg.id = existing["id"]
        reg.updated_at = utcnow()
        self._run(
            "INSERT INTO event_registrations (id,company_id,event_id,status,created_at,data) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (company_id, event_id) DO UPDATE SET "
            "status=EXCLUDED.status, data=EXCLUDED.data",
            (reg.id, reg.company_id, reg.event_id, reg.approval_status, reg.created_at,
             Jsonb(reg.model_dump(mode="json"))))
        return reg

    def get_event_registration(self, company_id: str, event_id: str) -> Optional[EventRegistration]:
        r = self._run("SELECT data FROM event_registrations WHERE company_id=%s AND event_id=%s",
                      (company_id, event_id), fetch="one")
        return EventRegistration.model_validate(r["data"]) if r else None

    def get_event_registrations(self, company_id: str, limit: int = 500) -> List[EventRegistration]:
        rows = self._run("SELECT data FROM event_registrations WHERE company_id=%s "
                         "ORDER BY created_at DESC LIMIT %s", (company_id, limit), fetch="all")
        return [EventRegistration.model_validate(r["data"]) for r in (rows or [])]

    # --- connected accounts ---
    def upsert_connection(self, conn: Connection) -> Connection:
        existing = self._run("SELECT id FROM connections WHERE company_id=%s AND provider=%s",
                             (conn.company_id, conn.provider), fetch="one")
        if existing:
            conn.id = existing["id"]
        conn.updated_at = utcnow()
        self._run(
            "INSERT INTO connections (id,company_id,provider,status,updated_at,data) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (company_id, provider) DO UPDATE SET "
            "status=EXCLUDED.status, updated_at=EXCLUDED.updated_at, data=EXCLUDED.data",
            (conn.id, conn.company_id, conn.provider, conn.status, conn.updated_at,
             Jsonb(conn.model_dump(mode="json"))))
        return conn

    def get_connection(self, company_id: str, provider: str) -> Optional[Connection]:
        r = self._run("SELECT data FROM connections WHERE company_id=%s AND provider=%s",
                      (company_id, provider), fetch="one")
        return Connection.model_validate(r["data"]) if r else None

    def list_connections(self, company_id: str) -> List[Connection]:
        rows = self._run("SELECT data FROM connections WHERE company_id=%s ORDER BY provider",
                         (company_id,), fetch="all")
        return [Connection.model_validate(r["data"]) for r in (rows or [])]

    def delete_connection(self, company_id: str, provider: str) -> bool:
        def op(cur):
            cur.execute("DELETE FROM connections WHERE company_id=%s AND provider=%s",
                        (company_id, provider))
            return cur.rowcount > 0
        return self._cursor_op(op)

    # --- event contacts (list + queue) ---
    def save_event_contacts(self, rows: List[EventContact]) -> int:
        rows = list(rows)

        def op(cur):
            n = 0
            for e in rows:
                cur.execute(
                    "INSERT INTO event_contacts "
                    "(id,company_id,event_id,contact_key,person_id,status,priority,"
                    "lease_expires_at,created_at,email,verdict,email_status,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (company_id, event_id, contact_key) DO NOTHING",
                    (e.id, e.company_id, e.event_id, e.contact_key, e.person_id, e.status,
                     e.priority, e.lease_expires_at, e.created_at,
                     e.email or None, e.verdict or None, e.email_status or None,
                     Jsonb(e.model_dump(mode="json"))))
                n += cur.rowcount
            return n
        return self._cursor_op(op)

    def update_event_contacts(self, rows: List[EventContact]) -> int:
        rows = list(rows)

        def op(cur):
            for e in rows:
                e.updated_at = utcnow()
                cur.execute(
                    # The columns are written alongside the blob, never instead of it. The blob
                    # stays the record; these are an index into it, and a column that drifts from
                    # the data it mirrors is worse than no column at all.
                    "UPDATE event_contacts SET person_id=%s, status=%s, priority=%s, "
                    "lease_expires_at=%s, email=%s, verdict=%s, email_status=%s, data=%s "
                    "WHERE id=%s",
                    (e.person_id, e.status, e.priority, e.lease_expires_at,
                     e.email or None, e.verdict or None, e.email_status or None,
                     Jsonb(e.model_dump(mode="json")), e.id))
            return len(rows)
        return self._cursor_op(op)

    def get_event_contact(self, contact_id: str) -> Optional[EventContact]:
        r = self._run("SELECT data FROM event_contacts WHERE id=%s", (contact_id,), fetch="one")
        return EventContact.model_validate(r["data"]) if r else None

    def event_contact_counts(self, company_id: str) -> dict:
        """Every number the funnel and the reports need, as three small aggregates.

        These used to be computed by fetching all 10,930 contact rows and counting them in Python
        — on every funnel render, every 30 seconds, per open browser tab, plus once per report and
        once per enrichment batch. That read the entire table hundreds of times an hour and
        exhausted the database's monthly transfer quota in a day, which took the whole service
        offline: the API could not answer /health, so the in-process scheduler stopped with it.

        A GROUP BY returns tens of rows instead of eleven thousand, and answers the same questions.
        """
        buckets = self._run(
            "SELECT status, verdict, (email IS NOT NULL AND email <> '') AS has_email, "
            "       COUNT(*) AS n "
            "FROM event_contacts WHERE company_id=%s GROUP BY 1,2,3",
            (company_id,), fetch="all") or []

        # Why fit people were held back — only that slice, so the grouping stays small.
        held = self._run(
            "SELECT split_part(COALESCE(data->>'last_error',''), ':', 1) AS why, COUNT(*) AS n "
            "FROM event_contacts WHERE company_id=%s AND status='skipped' "
            "  AND verdict='target' GROUP BY 1 ORDER BY n DESC LIMIT 12",
            (company_id,), fetch="all") or []

        titles = self._run(
            "SELECT COALESCE(NULLIF(data->>'title',''),'unknown') AS title, COUNT(*) AS n "
            "FROM event_contacts WHERE company_id=%s AND verdict='reject' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 6",
            (company_id,), fetch="all") or []

        return {
            "buckets": [{"status": r["status"] or "", "verdict": r["verdict"] or "",
                         "has_email": bool(r["has_email"]), "n": int(r["n"])} for r in buckets],
            "held": {(r["why"] or "held back"): int(r["n"]) for r in held},
            "reject_titles": [(r["title"], int(r["n"])) for r in titles],
        }

    def contacts_per_event(self, company_id: str) -> dict:
        """event_id -> how many contacts we hold, without reading the contacts."""
        rows = self._run(
            "SELECT event_id, COUNT(*) AS n FROM event_contacts WHERE company_id=%s "
            "GROUP BY event_id", (company_id,), fetch="all") or []
        return {r["event_id"]: int(r["n"]) for r in rows}

    def get_event_contacts(self, company_id: str, event_id: Optional[str] = None,
                           status: Optional[str] = None, limit: int = 500) -> List[EventContact]:
        sql = "SELECT data FROM event_contacts WHERE company_id=%s"
        params: list = [company_id]
        if event_id:
            sql += " AND event_id=%s"
            params.append(event_id)
        if status:
            sql += " AND status=%s"
            params.append(status)
        sql += " ORDER BY priority DESC, created_at ASC LIMIT %s"
        params.append(limit)
        rows = self._run(sql, tuple(params), fetch="all")
        return [EventContact.model_validate(r["data"]) for r in (rows or [])]

    def count_event_contacts(self, company_id: str) -> dict:
        rows = self._run("SELECT status, COUNT(*) AS n FROM event_contacts WHERE company_id=%s "
                         "GROUP BY status", (company_id,), fetch="all")
        return {r["status"]: r["n"] for r in (rows or [])}

    def lease_event_contacts(self, company_id: str, leased_by: str, limit: int = 25,
                             lease_seconds: int = 600) -> List[EventContact]:
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)

        def op(cur):
            cur.execute(
                "SELECT data FROM event_contacts WHERE company_id=%s AND status='queued' "
                "ORDER BY priority DESC, created_at ASC LIMIT %s", (company_id, limit))
            claimed: List[EventContact] = []
            for r in cur.fetchall():
                e = EventContact.model_validate(r["data"])
                e.status, e.leased_by, e.lease_expires_at = "reading", leased_by, expires
                e.attempts += 1
                e.updated_at = now
                # Compare-and-swap on status: safe without an explicit transaction (the pool
                # runs autocommit), so a second worker can never double-lease a row.
                cur.execute(
                    "UPDATE event_contacts SET status='reading', lease_expires_at=%s, data=%s "
                    "WHERE id=%s AND status='queued'",
                    (expires, Jsonb(e.model_dump(mode="json")), e.id))
                if cur.rowcount:
                    claimed.append(e)
            return claimed
        return self._cursor_op(op)

    def release_expired_leases(self, company_id: Optional[str] = None) -> int:
        now = utcnow()

        def op(cur):
            sql = ("SELECT data FROM event_contacts WHERE status='reading' "
                   "AND lease_expires_at IS NOT NULL AND lease_expires_at < %s")
            params: list = [now]
            if company_id:
                sql += " AND company_id=%s"
                params.append(company_id)
            cur.execute(sql, tuple(params))
            freed = 0
            for r in cur.fetchall():
                e = EventContact.model_validate(r["data"])
                e.status, e.leased_by, e.lease_expires_at = "queued", "", None
                e.updated_at = now
                cur.execute(
                    "UPDATE event_contacts SET status='queued', lease_expires_at=NULL, data=%s "
                    "WHERE id=%s AND status='reading'",
                    (Jsonb(e.model_dump(mode="json")), e.id))
                freed += cur.rowcount
            return freed
        return self._cursor_op(op)

    # --- send ledger ---
    def save_outreach_messages(self, rows: List[OutreachMessage]) -> int:
        rows = list(rows)

        def op(cur):
            for m in rows:
                m.email_key = m.email_key or norm_email(m.email)
                cur.execute(
                    "INSERT INTO outreach_messages "
                    "(id,company_id,event_id,person_id,email_key,status,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, data=EXCLUDED.data",
                    (m.id, m.company_id, m.event_id, m.person_id, m.email_key, m.status,
                     m.created_at, Jsonb(m.model_dump(mode="json"))))
            return len(rows)
        return self._cursor_op(op)

    def get_outreach_messages(self, company_id: str, status: Optional[str] = None,
                              limit: int = 500) -> List[OutreachMessage]:
        sql = "SELECT data FROM outreach_messages WHERE company_id=%s"
        params: list = [company_id]
        if status:
            sql += " AND status=%s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        rows = self._run(sql, tuple(params), fetch="all")
        return [OutreachMessage.model_validate(r["data"]) for r in (rows or [])]

    def sent_addresses_since(self, company_id: str, since) -> set:
        """Addresses we actually sent to since `since`. Covered by ix_om_window.

        The bounce scan needs this to decide which address in a failure notice is ours — a notice
        quotes the daemon, the sender and sometimes a postmaster, and retiring the wrong one is
        silent and permanent. It used to build the set by loading every contact, which is 10,930
        rows to validate a handful of notices, and grows forever.
        """
        rows = self._run(
            "SELECT DISTINCT lower(email_key) AS e FROM outreach_messages "
            "WHERE company_id=%s AND status='sent' AND created_at >= %s",
            (company_id, since), fetch="all") or []
        return {r["e"] for r in rows if r["e"]}

    def contacts_by_email(self, company_id: str, emails) -> List[EventContact]:
        """Only the contacts holding one of these addresses."""
        wanted = [e.lower() for e in emails if e]
        if not wanted:
            return []
        rows = self._run(
            "SELECT data FROM event_contacts WHERE company_id=%s "
            "AND lower(email) = ANY(%s)", (company_id, wanted), fetch="all") or []
        return [EventContact.model_validate(r["data"]) for r in rows]

    def drafted_messages_by_contact(self, company_id: str) -> dict:
        """event_contact_id -> the drafted message. One read, not one per contact.

        `event_contact_id` lives inside the JSONB blob rather than being a column, so there is no
        way to ask for a single one — which is why the caller used to fetch every drafted message
        and scan it in Python, once per contact.
        """
        rows = self._run(
            "SELECT data FROM outreach_messages WHERE company_id=%s AND status='drafted' "
            "ORDER BY created_at DESC LIMIT 5000", (company_id,), fetch="all") or []
        out: dict = {}
        for r in rows:
            m = OutreachMessage.model_validate(r["data"])
            if m.event_contact_id and m.event_contact_id not in out and m.subject:
                out[m.event_contact_id] = m
        return out

    def count_sent_since(self, company_id: str, since) -> int:
        """How many were SENT since `since`, counted by Postgres."""
        r = self._run(
            "SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=%s "
            "AND status='sent' AND created_at >= %s", (company_id, since), fetch="one")
        return int(r["n"]) if r else 0

    def sent_messages_since(self, company_id: str, since, limit: int = 200) -> List[OutreachMessage]:
        """The messages sent since `since`, newest first — for listing who was written to."""
        rows = self._run(
            "SELECT data FROM outreach_messages WHERE company_id=%s AND status='sent' "
            "AND created_at >= %s ORDER BY created_at DESC LIMIT %s",
            (company_id, since, limit), fetch="all") or []
        return [OutreachMessage.model_validate(r["data"]) for r in rows]

    def has_contacted(self, company_id: str, email: str) -> bool:
        key = norm_email(email)
        if not key:
            return False
        # Only real outbound counts. A `skipped` row is an audit record, not contact — letting
        # it suppress would permanently block anyone skipped for a transient reason.
        r = self._run("SELECT 1 FROM outreach_messages WHERE company_id=%s AND email_key=%s "
                      "AND status IN ('drafted','sent') LIMIT 1", (company_id, key), fetch="one")
        return bool(r)

    def count_outreach_messages(self, company_id: str, since_minutes: int,
                                statuses: tuple = ("drafted", "sent")) -> int:
        since = utcnow() - timedelta(minutes=since_minutes)
        r = self._run("SELECT COUNT(*) AS n FROM outreach_messages WHERE company_id=%s "
                      "AND status = ANY(%s) AND created_at >= %s",
                      (company_id, list(statuses), since), fetch="one")
        return int(r["n"]) if r else 0

    # --- manual exclusions ---
    def save_do_not_contact(self, rows: List[DoNotContact]) -> int:
        rows = list(rows)

        def op(cur):
            n = 0
            for d in rows:
                cur.execute(
                    "INSERT INTO do_not_contact (id,company_id,value,created_at,data) "
                    "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (company_id, value) DO NOTHING",
                    (d.id, d.company_id, d.value, d.created_at,
                     Jsonb(d.model_dump(mode="json"))))
                n += cur.rowcount
            return n
        return self._cursor_op(op)

    def get_do_not_contact(self, company_id: str, limit: int = 2000) -> List[DoNotContact]:
        rows = self._run("SELECT data FROM do_not_contact WHERE company_id=%s "
                         "ORDER BY created_at DESC LIMIT %s", (company_id, limit), fetch="all")
        return [DoNotContact.model_validate(r["data"]) for r in (rows or [])]

    def is_do_not_contact(self, company_id: str, email: str) -> bool:
        key = norm_email(email)
        if not key:
            return False
        r = self._run("SELECT 1 FROM do_not_contact WHERE company_id=%s AND value IN (%s,%s) "
                      "LIMIT 1", (company_id, key, email_domain(key)), fetch="one")
        return bool(r)

    # --- app-wide settings ---
    def get_setting(self, key: str) -> Optional[str]:
        r = self._run("SELECT value FROM app_settings WHERE key=%s", (key,), fetch="one")
        return r["value"] if r else None

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO app_settings (key,value,updated_at) VALUES (%s,%s,%s) "
                  "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, "
                  "updated_at=EXCLUDED.updated_at", (key, value, utcnow()))

    def raw_scan(self, table: str, limit: int = 100000) -> List[dict]:
        rows = self._run(f"SELECT data FROM {table} LIMIT %s", (limit,), fetch="all")
        return [r["data"] for r in (rows or [])]
