"""Phase B — dual-write bridge (warmgraph.storage.mirror). Verifies the legacy blob objects the
agents already produce get mirrored into the normalized tables, that the mapping is idempotent
(stable signal ids), that feedback projects onto customer_list.status, and that dual_write never
propagates a failure into the live path."""
from __future__ import annotations

import tempfile

import pytest

from warmgraph.models import Account, CompanyLead, Event, LeadFeedback, Post, Profile
from warmgraph.storage import mirror
from warmgraph.storage.sqlite_store import SqliteStore


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


def test_mirror_profile_creates_client_and_intel(store):
    p = Profile(url="https://example.com", domain="example.com", industry="martech")
    cid = mirror.mirror_profile(store, p)
    assert store.get_company("example.com").id == cid
    assert store.get_company_intel("example.com") is not None


def test_mirror_company_leads_upserts_customer_and_signal_idempotent(store):
    leads = [CompanyLead(subject_domain="example.com", company="Bloom", company_domain="bloom.com",
                         signal_type="hiring", source_url="https://x/j1", rationale="hiring IMM",
                         relevance="high", embedding=[0.1, 0.2], id="L1")]
    assert mirror.mirror_company_leads(store, leads) == 1
    assert mirror.mirror_company_leads(store, leads) == 1  # re-run
    cust = store.get_customer("bloom.com")
    assert cust is not None
    sigs = store.get_signal_facts(signal_type="hiring")
    assert len(sigs) == 1  # stable id "sig-L1" → no duplicate on re-run
    assert sigs[0].customer_id == cust.id and sigs[0].embedding == [0.1, 0.2]


def test_mirror_posts_and_events(store):
    assert mirror.mirror_posts(store, [Post(subject_domain="m", platform="linkedin",
                                            external_id="e1", text="hi", url="https://li/1")]) == 1
    assert mirror.mirror_posts(store, []) == 0
    assert mirror.mirror_events(store, [Event(subject_domain="m", name="Summit",
                                             url="https://luma/x", platform="luma")]) == 1
    assert len(store.get_raw_social_posts()) == 1
    assert len(store.get_raw_events()) == 1


def test_mirror_accounts_projects_feedback_status_and_orders(store):
    accts = [Account(subject_domain="example.com", company="Bloom", company_domain="bloom.com",
                     stack_score=7, relevance=0.8),
             Account(subject_domain="example.com", company="Red Bull", company_domain="redbull.com",
                     stack_score=3)]
    feedback = [LeadFeedback(subject_domain="example.com", company="Bloom",
                             company_domain="bloom.com", decision="approve"),
                LeadFeedback(subject_domain="example.com", company="Red Bull",
                             company_domain="redbull.com", decision="reject")]
    assert mirror.mirror_accounts(store, "example.com", accts, feedback) == 2
    client = store.get_company("example.com")
    cl = store.get_customer_list(client.id)
    assert [r.stack_score for r in cl] == [7, 3]  # ordered by stack_score desc
    status = {store.get_customer_by_id(r.customer_id).name: r.status for r in cl}
    assert status == {"Bloom": "approved", "Red Bull": "rejected"}


def test_serve_customer_list_recomputes_stack_and_orders(store):
    from warmgraph.agents.activities.customer_list import serve_customer_list
    from warmgraph.entities import Client, CustomerListRow, Prospect, SignalFact
    client = store.upsert_company(Client(domain="example.com"))
    # two-signal prospect but a STALE stored stack_score of 1 (as a backfilled dup would leave it)
    two = store.upsert_customer(Prospect(domain="two.com", name="Two"))
    one = store.upsert_customer(Prospect(domain="one.com", name="One"))
    store.save_signal_facts([
        SignalFact(customer_id=two.id, signal_type="hiring", source_url="h"),
        SignalFact(customer_id=two.id, signal_type="fundraising", source_url="f"),
        SignalFact(customer_id=one.id, signal_type="hiring", source_url="h2")])
    store.replace_customer_list(client.id, [
        CustomerListRow(company_id=client.id, customer_id=two.id, stack_score=1),   # stale/low
        CustomerListRow(company_id=client.id, customer_id=one.id, stack_score=1)])
    served = serve_customer_list(store, "example.com")
    assert [a.company for a in served] == ["Two", "One"]   # 2-signal ranks above 1-signal
    assert served[0].stack_score == 2                       # recomputed from assembled signals


def test_dual_write_swallows_errors(store):
    def boom(*a, **k):
        raise RuntimeError("normalized write blew up")

    # must return None, not raise — the legacy path must never be affected
    assert mirror.dual_write("boom", boom, store) is None
