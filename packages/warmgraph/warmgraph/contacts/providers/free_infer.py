"""Free contact provider — $0, no keys.

Discovery: public LinkedIn `/in` search (same free technique team_signal uses) → real person + title +
profile URL + city for the target archetypes at a company. Email: pattern inference (best-guess
`first.last@domain`, the most common B2B format), labelled 'guessed'. This is the always-on fallback;
paid providers (Apollo/Hunter/…) add verified email/phone on top via the same interface."""
from __future__ import annotations

import re
import unicodedata
from typing import List, Tuple

from warmgraph.contacts.providers.base import ContactProvider, ProviderCtx, Target
from warmgraph.jsonutil import extract_json
from warmgraph.models import Contact
from warmgraph.search import web_search

_PEOPLE_SITES = ["linkedin.com"]

_EXTRACT_SYSTEM = (
    "You extract DECISION-MAKER contacts from LinkedIn profile search results. Given a target COMPANY "
    "and the buying-role archetypes we want, find the REAL people who work AT that company in those "
    "roles. Rules:\n"
    "- Use ONLY the results shown; 'i' is the result index. NEVER invent a person, title, or URL.\n"
    "- A LinkedIn result reads 'Full Name - <Title> - <Company> | LinkedIn'. Extract the person's FULL "
    "NAME, their title, and the profile URL (the /in/ link).\n"
    "- Keep ONLY people who currently work at the target company (not other companies, not recruiters "
    "listing the role, not the company page itself). Match each to the closest archetype.\n"
    "- seniority: 'buyer' for senior owners (Head/VP/Director/Chief), 'champion' for hands-on "
    "(Manager/Lead/Specialist/Coordinator). Capture city/location if shown, else ''.\n"
    "- Return at most one BEST person per archetype. Dedupe by person."
)
_EXTRACT_SCHEMA = """Return ONLY JSON:
{"contacts":[{"i":0,"person":"","title":"","seniority":"buyer","location":""}]}"""


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode("ascii")


def _name_parts(person: str) -> Tuple[str, str]:
    toks = [t for t in re.split(r"[^A-Za-z]+", _ascii(person)) if t]
    if not toks:
        return "", ""
    return toks[0].lower(), toks[-1].lower()


def infer_email(person: str, domain: str) -> Tuple[str, str, float]:
    """Best-guess business email. first.last@domain is the most common B2B format; unverified → 'guessed'.
    (A paid provider or the verify step upgrades status/confidence later.)"""
    first, last = _name_parts(person)
    dom = (domain or "").lower().replace("www.", "").strip()
    if not first or not dom:
        return "", "unknown", 0.0
    local = f"{first}.{last}" if last else first
    return f"{local}@{dom}", "guessed", 0.4


class FreeInferProvider(ContactProvider):
    name = "free_infer"

    def available(self, settings) -> bool:
        return True   # no key needed

    def find(self, ctx: ProviderCtx, company: str, domain: str, targets: List[Target]) -> List[Contact]:
        results: List[dict] = []
        seen = set()
        for t in targets:
            for q in (f'"{t.title}" "{company}"', f'{t.title} {company} linkedin'):
                for r in web_search(q, ctx.settings, max_results=6, include_domains=_PEOPLE_SITES):
                    u = r.get("url", "")
                    if u and "/in/" in u and u not in seen:
                        seen.add(u)
                        results.append(r)
        contacts = self._extract(ctx, company, domain, targets, results)
        for c in contacts:
            c.provider = self.name
            c.source = "LinkedIn"
            if domain and c.person:
                c.email, c.email_status, c.email_confidence = infer_email(c.person, domain)
        return contacts

    def _extract(self, ctx, company, domain, targets, results) -> List[Contact]:
        if not results or not ctx.registry.has_llm:
            return []
        batch = results[:16]
        lines = [f"{i}. {(r.get('title') or '')[:110]} :: {(r.get('content') or '')[:120]} :: {r.get('url','')}"
                 for i, r in enumerate(batch)]
        arche = "; ".join(f"{t.seniority}: {t.title}" for t in targets)
        user = (f"Target company: {company}\nArchetypes we want: {arche}\n\n"
                f"LinkedIn results:\n" + "\n".join(lines) + f"\n\n{_EXTRACT_SCHEMA}")
        d = extract_json(ctx.registry.complete("decision_maker_extract", _EXTRACT_SYSTEM, user,
                                               max_tokens=1200, want_json=True)) or {}
        out: List[Contact] = []
        by_person = set()
        for item in d.get("contacts", []):
            try:
                src = batch[int(item.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            person = str(item.get("person", "")).strip()
            if not person or person.lower() in by_person:
                continue
            by_person.add(person.lower())
            seniority = str(item.get("seniority", "")).strip().lower()
            title = str(item.get("title", "")).strip()
            out.append(Contact(
                company=company, company_domain=domain, person=person, title=title,
                seniority="buyer" if seniority == "buyer" else "champion",
                is_decision_maker=(seniority == "buyer"),
                role_match=title, linkedin_url=src.get("url", ""),
                location=str(item.get("location", "")), source_url=src.get("url", ""),
            ))
        return out
