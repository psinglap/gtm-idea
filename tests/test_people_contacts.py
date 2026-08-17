"""Phase C — identity resolution (people.py) + the normalized contact write path.

Identity resolution is the crux: the SAME human found across sources (LinkedIn hit, team-page hit,
a guessed email) must collapse into ONE enriched `people` row with no field dropped, while genuinely
different people stay separate. These are pure/offline (no network, no LLM)."""
from __future__ import annotations

import tempfile

import pytest

from warmgraph.agents.activities.contacts import ContactsAgent, targets_from_icp
from warmgraph.agents.activities.people import (
    merge_person,
    person_from_contact,
    raw_from_contact,
    resolve_people,
)
from warmgraph.entities import Person
from warmgraph.models import Account, Contact, Profile
from warmgraph.storage.sqlite_store import SqliteStore


def P(**kw):
    return Person(**kw)


def test_shared_linkedin_merges():
    a = [P(person="Ann Lee", linkedin_url="https://linkedin.com/in/annlee", company_domain="acme.com")]
    b = [P(person="Ann Lee", linkedin_url="https://LinkedIn.com/in/annlee/", email="ann@acme.com",
           company_domain="acme.com")]  # trailing slash + case differ, same profile
    merged, idmap, changed = resolve_people(a, b)
    assert len(merged) == 1
    assert merged[0].email == "ann@acme.com"          # detail from the second source kept
    assert idmap[b[0].id] == a[0].id


def test_shared_email_merges_without_linkedin():
    a = [P(person="Bob", email="bob@acme.com", company_domain="acme.com")]
    b = [P(person="Bob Smith", email="bob@acme.com", company_domain="acme.com", title="VP Growth")]
    merged, idmap, _ = resolve_people(a, b)
    assert len(merged) == 1 and merged[0].title == "VP Growth"
    assert merged[0].person == "Bob Smith"            # richer name wins


def test_name_plus_employer_merges_without_shared_key():
    a = [P(person="Carol Nguyen", company_domain="acme.com", title="Marketing Lead")]
    b = [P(person="C Nguyen", company_domain="acme.com", title="Head of Marketing",
           is_decision_maker=True, seniority="buyer")]
    merged, _, _ = resolve_people(a, b)
    assert len(merged) == 1
    assert merged[0].is_decision_maker is True         # decision-maker wins
    assert set(merged[0].titles) >= {"Marketing Lead", "Head of Marketing"}  # titles unioned


def test_different_people_and_different_employer_stay_separate():
    a = [P(person="Dan Cruz", company_domain="acme.com")]
    b = [P(person="Dan Cruz", company_domain="globex.com"),   # same name, different employer
         P(person="Eve Ford", company_domain="acme.com")]     # different person
    merged, _, _ = resolve_people(a, b)
    assert len(merged) == 3


def test_resolve_is_idempotent():
    existing = [P(person="Ann", linkedin_url="https://linkedin.com/in/ann", company_domain="a.com")]
    incoming = [P(person="Ann", linkedin_url="https://linkedin.com/in/ann", company_domain="a.com")]
    merged, _, _ = resolve_people(existing, incoming)
    merged2, idmap2, _ = resolve_people(merged, incoming)     # re-resolve against the result
    assert len(merged2) == 1                                  # no duplicate row created
    assert idmap2[incoming[0].id] == merged[0].id             # maps back to the same person


def test_contact_mapping_roundtrip():
    c = Contact(company="Acme", company_domain="acme.com", person="Ann Lee", title="Head of Growth",
                seniority="buyer", is_decision_maker=True, linkedin_url="https://linkedin.com/in/ann",
                email="ann@acme.com", email_status="guessed", email_confidence=0.4, provider="free_infer")
    p = person_from_contact(c)
    assert p.company_domain == "acme.com" and p.is_decision_maker and p.titles == ["Head of Growth"]
    r = raw_from_contact(c)
    assert r.company_hint == "acme.com" and "linkedin" in r.where_active


def test_targets_from_icp_defaults_and_derivation():
    prof = Profile(url="x", domain="x.com")
    prof.icp.personas = []
    assert targets_from_icp(prof)[0].is_decision_maker is True   # sensible default
    from warmgraph.models import IcpPersona
    prof.icp.personas = [IcpPersona(role="Head of Marketing", seniority="VP"),
                         IcpPersona(role="Marketing Coordinator", seniority="IC")]
    ts = targets_from_icp(prof)
    dm = {t.title: t.is_decision_maker for t in ts}
    assert dm["Head of Marketing"] is True and dm["Marketing Coordinator"] is False


def test_mirror_prospect_contacts_writes_normalized_and_dedups():
    store = SqliteStore(tempfile.mktemp(suffix=".db"))
    from warmgraph.storage import mirror
    cid = mirror.client_id_for(store, "example.com")
    acct = Account(subject_domain="example.com", company="Acme", company_domain="acme.com")
    contacts = [
        Contact(company="Acme", company_domain="acme.com", person="Ann Lee", title="Head of Growth",
                seniority="buyer", is_decision_maker=True, linkedin_url="https://linkedin.com/in/ann"),
        Contact(company="Acme", company_domain="acme.com", person="Ann Lee", title="Head of Growth",
                seniority="buyer", is_decision_maker=True, linkedin_url="https://linkedin.com/in/ann/"),
    ]  # same person twice → must collapse
    n = ContactsAgent._mirror_prospect_contacts(store, cid, acct, contacts)
    assert n == 1                                        # one customer_contact (deduped person)
    assert len(store.get_people(company_domain="acme.com")) == 1
    prospect = store.get_customer("acme.com")
    ccs = store.get_customer_contacts(cid, customer_id=prospect.id)
    assert len(ccs) == 1 and ccs[0].is_decision_maker
    assert len(store.get_raw_people()) == 2             # raw keeps both source rows
    # re-run is idempotent (person + contact not duplicated)
    ContactsAgent._mirror_prospect_contacts(store, cid, acct, contacts)
    assert len(store.get_people(company_domain="acme.com")) == 1
    assert len(store.get_customer_contacts(cid, customer_id=prospect.id)) == 1
