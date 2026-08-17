"""Offline tests for the normalized relational schema (clients → intel → raw → signals →
customers → customer_list → people → customer_contacts). SQLite mirror; no network, no LLM.
Verifies: raw dedup by url, social dedup by (platform, external_id), client/prospect upsert
dedup (with name_key fallback), signal-fact embedding round-trip + filters, idempotent
customer_list replace ordered by stack_score, people-by-employer, unique customer_contacts."""
from __future__ import annotations

import tempfile

import pytest

from warmgraph.entities import (
    Client,
    CompanyIntel,
    CustomerContact,
    CustomerListRow,
    Person,
    Prospect,
    RawJobPosting,
    RawPerson,
    RawSocialPost,
    SignalFact,
)
from warmgraph.storage.sqlite_store import SqliteStore


@pytest.fixture
def store():
    return SqliteStore(tempfile.mktemp(suffix=".db"))


def test_client_upsert_dedup_by_domain(store):
    c = store.upsert_company(Client(domain="Example.com", name="Example"))
    c2 = store.upsert_company(Client(domain="example.com", name="Example Inc"))  # case-insensitive same
    assert c2.id == c.id
    assert store.get_company("example.com").name == "Example Inc"  # latest write wins
    assert store.get_company_by_id(c.id) is not None


def test_company_intel_one_per_domain(store):
    store.save_company_intel(CompanyIntel(domain="example.com", icp={"seg": "A"}))
    store.save_company_intel(CompanyIntel(domain="example.com", icp={"seg": "B"}))
    assert store.get_company_intel("example.com").icp["seg"] == "B"


def test_raw_job_dedup_by_url_but_not_blank(store):
    assert store.save_raw_job_postings([RawJobPosting(url="https://x/j1", title="a")]) == 1
    assert store.save_raw_job_postings([RawJobPosting(url="https://x/j1", title="dup")]) == 0
    # blank urls must NOT collapse into one another
    assert store.save_raw_job_postings([RawJobPosting(url="", title="p")]) == 1
    assert store.save_raw_job_postings([RawJobPosting(url="", title="q")]) == 1
    assert len(store.get_raw_job_postings()) == 3


def test_raw_social_dedup_by_platform_external_id(store):
    assert store.save_raw_social_posts([RawSocialPost(platform="linkedin", external_id="e1")]) == 1
    assert store.save_raw_social_posts([RawSocialPost(platform="linkedin", external_id="e1")]) == 0
    assert store.save_raw_social_posts([RawSocialPost(platform="twitter", external_id="e1")]) == 1


def test_prospect_upsert_by_domain_and_namekey(store):
    p = store.upsert_customer(Prospect(domain="Acme.com", name="Acme"))
    p2 = store.upsert_customer(Prospect(domain="acme.com", name="Acme Co"))
    assert p2.id == p.id
    # domainless prospects dedup on name_key
    d1 = store.upsert_customer(Prospect(name="Bloom", name_key="bloom"))
    d2 = store.upsert_customer(Prospect(name="Bloom", name_key="bloom"))
    assert d1.id == d2.id
    assert store.get_customer("acme.com").name == "Acme Co"


def test_signal_fact_embedding_roundtrip_and_filters(store):
    cust = store.upsert_customer(Prospect(domain="acme.com", name="Acme"))
    store.save_signal_facts([SignalFact(customer_id=cust.id, signal_type="hiring",
                                        source_url="https://x/j1", embedding=[0.1, 0.2, 0.3])])
    facts = store.get_signal_facts(signal_type="hiring")
    assert len(facts) == 1 and facts[0].embedding == [0.1, 0.2, 0.3]
    assert store.get_signal_facts(customer_id=cust.id)[0].customer_id == cust.id
    assert store.get_signal_facts(signal_type="fundraising") == []


def test_customer_list_replace_is_idempotent_and_ordered(store):
    client = store.upsert_company(Client(domain="example.com"))
    a = store.upsert_customer(Prospect(domain="a.com"))
    b = store.upsert_customer(Prospect(domain="b.com"))
    rows = [CustomerListRow(company_id=client.id, customer_id=a.id, stack_score=5, pref_score=0.9),
            CustomerListRow(company_id=client.id, customer_id=b.id, stack_score=9, pref_score=0.1)]
    store.replace_customer_list(client.id, rows)
    store.replace_customer_list(client.id, rows)  # replace, not append
    cl = store.get_customer_list(client.id)
    assert len(cl) == 2
    assert cl[0].stack_score == 9  # ordered by stack_score desc


def test_people_by_employer_and_unique_contacts(store):
    client = store.upsert_company(Client(domain="example.com"))
    cust = store.upsert_customer(Prospect(domain="acme.com"))
    store.save_people([Person(person="Ann", company_domain="Acme.com", title="Head of Growth")])
    ppl = store.get_people(company_domain="acme.com")
    assert len(ppl) == 1 and ppl[0].person == "Ann"
    pid = ppl[0].id
    store.save_customer_contacts([CustomerContact(company_id=client.id, customer_id=cust.id,
                                                  person_id=pid, role_match="buyer")])
    store.save_customer_contacts([CustomerContact(company_id=client.id, customer_id=cust.id,
                                                  person_id=pid, role_match="champion")])
    cc = store.get_customer_contacts(client.id, customer_id=cust.id)
    assert len(cc) == 1 and cc[0].role_match == "champion"  # unique triple, latest wins


def test_raw_people_persist(store):
    store.save_raw_people([RawPerson(person="Ann", company_hint="Acme", email="a@acme.com")])
    assert len(store.get_raw_people()) == 1
