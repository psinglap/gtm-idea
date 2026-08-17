"""Idempotent migration: legacy JSON-blob tables (keyed by `subject_domain`) → the normalized
relational schema (clients → intel → raw → signals → customers → customer_list → people).

Mapping (see plan `when-i-clicked-on-staged-bee.md`):
    profiles      → companies (client) + company_intel
    posts         → raw_social_posts
    events        → raw_events
    company_leads → customers (prospect, upsert by domain) + signals (SignalFact)
    accounts      → customers (prospect) + customer_list (per client)
    lead_feedback → projected onto customer_list.status (approve→approved / reject→rejected)

The old tables are LEFT IN PLACE (read-only) — drop them only after verifying the cutover.
Idempotent: clients/prospects upsert by domain; signals get a stable id derived from the source
lead id; customer_list is replaced per client; re-running converges to the same state.

Usage:
    python scripts/migrate_normalize.py --dry-run      # count what WOULD migrate, write nothing
    python scripts/migrate_normalize.py                # migrate (uses WG_STORE / DATABASE_URL)
    python scripts/migrate_normalize.py --limit 500    # cap rows per table (testing)
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Dict, List

from warmgraph.config import get_settings
from warmgraph.models import Account, CompanyLead, Event, LeadFeedback, Post, Profile
from warmgraph.storage import get_store, mirror


def reconcile_legacy_signals(store) -> bool:
    """Idempotent Postgres-only fix for the table-name collision: the OLD `signals` table (social
    Signal model: id/subject_domain/created_at/data) squats the name the normalized SignalFact table
    needs. Preserve its rows into `social_signals`, move the old table aside as a backup, and let
    init_schema recreate the normalized `signals`. No-op on SQLite / once already normalized."""
    run = getattr(store, "_run", None)
    if run is None:   # SQLite creates the normalized `signals` directly — nothing to reconcile
        return False
    cols = [r["column_name"] for r in run(
        "SELECT column_name FROM information_schema.columns WHERE table_name='signals'",
        (), fetch="all") or []]
    if not cols or "customer_id" in cols:
        return False   # table absent (fresh) or already normalized
    run("INSERT INTO social_signals (id,subject_domain,created_at,data) "
        "SELECT id,subject_domain,created_at,data FROM signals ON CONFLICT (id) DO NOTHING")
    run("ALTER TABLE signals RENAME TO signals_legacy_backup")
    store.init_schema()   # recreate `signals` with the normalized columns
    return True


def migrate(store, dry_run: bool = False, limit: int = 100000) -> Dict[str, int]:
    """Backfill the normalized tables from the legacy blobs, reusing the SAME mapping the live
    agents dual-write through (`warmgraph.storage.mirror`) — so a backfilled row is byte-identical
    to one the agents would have written. Idempotent; leaves the legacy tables untouched."""
    counts: Dict[str, int] = defaultdict(int)
    if not dry_run and reconcile_legacy_signals(store):
        counts["reconciled_legacy_signals"] = 1

    def scan(table, model):
        rows = []
        for d in store.raw_scan(table, limit=limit):
            try:
                rows.append(model.model_validate(d))
            except Exception:
                counts[f"{table}:skipped"] += 1
        return rows

    # 1) profiles → companies + company_intel
    profiles = scan("profiles", Profile)
    counts["companies"] = counts["company_intel"] = len(profiles)
    # 2) posts → raw_social_posts ; 3) events → raw_events ; 4) company_leads → customers + signals
    posts = scan("posts", Post)
    events = scan("events", Event)
    leads = scan("company_leads", CompanyLead)
    counts["raw_social_posts"] = len(posts)
    counts["raw_events"] = len(events)
    counts["signals"] = len(leads)
    # 5) lead_feedback (projected onto customer_list.status by subject_domain)
    feedback: Dict[str, List[LeadFeedback]] = defaultdict(list)
    for f in scan("lead_feedback", LeadFeedback):
        feedback[(f.subject_domain or "").lower()].append(f)
        counts["lead_feedback"] += 1
    # 6) accounts → customers + customer_list (grouped per client)
    by_client: Dict[str, List[Account]] = defaultdict(list)
    for a in scan("accounts", Account):
        by_client[(a.subject_domain or "").lower()].append(a)
    counts["customer_list"] = sum(len(v) for v in by_client.values())

    if dry_run:
        return dict(counts)

    for p in profiles:
        mirror.mirror_profile(store, p)
    mirror.mirror_posts(store, posts)
    mirror.mirror_events(store, events)
    mirror.mirror_company_leads(store, leads)
    for domain, accts in by_client.items():
        mirror.mirror_accounts(store, domain, accts, feedback.get(domain, []))

    return dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate legacy blob tables → normalized schema.")
    ap.add_argument("--dry-run", action="store_true", help="count only; write nothing")
    ap.add_argument("--limit", type=int, default=100000, help="max rows per source table")
    args = ap.parse_args()

    store = get_store(get_settings())
    backend = type(store).__name__
    print(f"[migrate] store={backend} dry_run={args.dry_run}")
    counts = migrate(store, dry_run=args.dry_run, limit=args.limit)
    print("[migrate] " + ("would migrate:" if args.dry_run else "migrated:"))
    for k in sorted(counts):
        print(f"    {k:24} {counts[k]}")
    if not counts:
        print("    (no legacy rows found)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
