"""Contacts agent — turn the ranked customer list into the actual PEOPLE to reach out to.

For each qualified prospect (from the customer list) it derives the person archetypes from the
client's ICP personas (buyer / champion), runs the contact provider waterfall (free LinkedIn
discovery + email-pattern inference today; paid providers by key later), and writes:
  • legacy `contacts` (the existing Contact DTO — unchanged serving path), AND
  • the normalized layer (dual-write): `raw_people` (bronze) → `people` (identity-resolved, global)
    → `customer_contacts` (this client ↔ this prospect ↔ this person).
People are de-duplicated across prospects/sources by identity resolution (see people.py), so the
same human found twice becomes one enriched `people` row.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel

from warmgraph.agents.activities.people import (
    person_from_contact,
    raw_from_contact,
    resolve_people,
)
from warmgraph.agents.base import Agent
from warmgraph.contacts.providers.base import ProviderCtx, Target
from warmgraph.contacts.waterfall import build_providers, enrich_company
from warmgraph.entities import CustomerContact, Prospect
from warmgraph.models import Contact
from warmgraph.storage import mirror

_BUYER_HINTS = ("buyer", "head", "vp", "vice president", "director", "chief", "c-level", "exec",
                "owner", "founder", "lead")


class ContactsInput(BaseModel):
    url: str
    limit: int = 10           # how many top prospects to enrich per run (bounds cost)
    per_company: int = 3      # max contacts kept per prospect


class ContactsReport(BaseModel):
    subject_domain: str
    contacts: List[Contact] = []


def targets_from_icp(profile) -> List[Target]:
    """Person archetypes to find, derived from the ICP personas (buyer = decision-maker)."""
    out: List[Target] = []
    icp = getattr(profile, "icp", None)
    for p in (getattr(icp, "personas", []) or []):
        if not p.role:
            continue
        sr = (p.seniority or "").lower()
        is_dm = any(h in sr for h in _BUYER_HINTS) or any(h in (p.role or "").lower() for h in _BUYER_HINTS)
        out.append(Target(title=p.role, seniority="buyer" if is_dm else "champion",
                          is_decision_maker=is_dm))
    if not out:
        out = [Target(title="Head of Marketing", seniority="buyer", is_decision_maker=True)]
    return out[:4]


class ContactsAgent(Agent):
    name = "contacts"
    description = ("For each qualified prospect in the customer list, find the real DECISION-MAKERS "
                   "to contact (LinkedIn discovery + email-pattern inference, free) → identity-resolved "
                   "global people + per-client customer_contacts.")
    InputModel = ContactsInput
    OutputModel = ContactsReport

    def run(self, inp: ContactsInput) -> ContactsReport:
        s, reg, store = self.ctx.settings, self.ctx.registry, self.ctx.store
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        targets = targets_from_icp(profile)
        pctx = ProviderCtx(settings=s, registry=reg)
        providers = build_providers(s)

        # prospects come from the (already-built) customer list; enrich the top `limit`.
        accounts = store.get_accounts(domain) if store is not None else []
        accounts = accounts[: inp.limit]

        cid = mirror.dual_write("contacts:client", mirror.client_id_for, store, domain) if store else None
        all_contacts: List[Contact] = []
        for a in accounts:
            company, cdom = a.company, (a.company_domain or "").lower()
            people = enrich_company(pctx, company, cdom, targets, providers)[: inp.per_company]
            for c in people:
                c.subject_domain = domain
                c.company = c.company or company
                c.company_domain = c.company_domain or cdom
            all_contacts.extend(people)
            if store is not None and people:
                mirror.dual_write("contacts:normalized", self._mirror_prospect_contacts,
                                  store, cid, a, people)

        if store is not None and all_contacts:
            store.save_contacts(all_contacts)          # legacy serving path (unchanged)
        return ContactsReport(subject_domain=domain, contacts=all_contacts)

    @staticmethod
    def _mirror_prospect_contacts(store, cid, account, contacts: List[Contact]) -> int:
        """Dual-write one prospect's contacts into raw_people → people (resolved) → customer_contacts."""
        cdom = (account.company_domain or "").lower()
        prospect = store.upsert_customer(Prospect(
            domain=cdom, name=account.company, name_key=(account.company or "").strip().lower(),
            website=account.website, industry=account.industry, location=account.location))
        store.save_raw_people([raw_from_contact(c) for c in contacts])

        incoming = [person_from_contact(c) for c in contacts]
        existing = store.get_people(company_domain=cdom) if cdom else store.get_people()
        merged, id_map, changed = resolve_people(existing, incoming)
        changed_set = set(changed)
        store.save_people([p for p in merged if p.id in changed_set])

        rows: List[CustomerContact] = []
        seen = set()
        for inc, c in zip(incoming, contacts):
            pid = id_map.get(inc.id, inc.id)
            if pid in seen:
                continue
            seen.add(pid)
            rows.append(CustomerContact(
                company_id=cid, customer_id=prospect.id, person_id=pid,
                is_decision_maker=c.is_decision_maker, role_match=c.role_match or c.title))
        store.save_customer_contacts(rows)
        return len(rows)
