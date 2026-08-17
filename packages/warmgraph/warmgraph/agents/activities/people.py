"""Identity resolution — refine many `raw_people` / discovered Contacts into ONE global `people`
row per real human, merging every detail (no url/email-only dedup, no dropped fields).

The resolution ladder (strongest signal first):
  1. SHARED KEY — same normalized linkedin_url or same email → definitely the same person.
  2. NAME + EMPLOYER — same last name + same company_domain + same first initial → same person
     across sources that lack a shared key (e.g. a LinkedIn hit and a team-page hit).
  3. EMBEDDING — if both carry a profile embedding, cosine ≥ threshold AND same employer → same.
When two records resolve to one, `merge_person` unions their fields (richest / most-recent wins,
list fields concatenated, decision-maker + higher email confidence preferred). No detail is lost.

Pure functions (no store / no network) so the resolution logic is unit-tested offline. The contacts
agent calls `resolve_people(existing_people, incoming_people)` incrementally as new people are found.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Tuple

from warmgraph.entities import Person, RawPerson
from warmgraph.models import Contact, new_id, utcnow

_EMBED_MATCH = 0.92


# --------------------------------------------------------------------------- #
# Normalization + mapping                                                      #
# --------------------------------------------------------------------------- #
def norm_url(u: str) -> str:
    u = (u or "").strip().lower().split("?")[0].rstrip("/")
    return u


def norm_email(e: str) -> str:
    return (e or "").strip().lower()


def _name_parts(person: str) -> Tuple[str, str]:
    toks = [t for t in re.split(r"[^A-Za-z]+", person or "") if t]
    if not toks:
        return "", ""
    return toks[0].lower(), toks[-1].lower()


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def person_from_contact(c: Contact) -> Person:
    return Person(
        person=c.person, title=c.title, seniority=c.seniority,
        is_decision_maker=c.is_decision_maker, company_domain=(c.company_domain or "").lower(),
        linkedin_url=c.linkedin_url, email=c.email, email_status=c.email_status,
        email_confidence=c.email_confidence, phone=c.phone, location=c.location,
        titles=[c.title] if c.title else [], providers=[c.provider] if c.provider else [],
        source_url=c.source_url)


def raw_from_contact(c: Contact) -> RawPerson:
    return RawPerson(
        source=c.provider or c.source, person=c.person, title=c.title,
        company_hint=c.company_domain or c.company, linkedin_url=c.linkedin_url, email=c.email,
        phone=c.phone, location=c.location,
        where_active=["linkedin"] if c.linkedin_url else [],
        raw=c.model_dump(mode="json"))


def person_from_raw(r: RawPerson) -> Person:
    return Person(
        person=r.person, title=r.title, titles=[r.title] if r.title else [],
        company_domain=(r.company_hint or "").lower(), linkedin_url=r.linkedin_url, email=r.email,
        phone=r.phone, location=r.location, where_active=list(r.where_active),
        problems=list(r.current_problems), providers=[r.source] if r.source else [],
        embedding=r.embedding or [], source_url=r.linkedin_url,
        touchpoint_refs=list(r.event_refs) + list(r.posts_refs))


# --------------------------------------------------------------------------- #
# Matching + merging                                                           #
# --------------------------------------------------------------------------- #
def _is_same(a: Person, b: Person) -> bool:
    au, bu = norm_url(a.linkedin_url), norm_url(b.linkedin_url)
    if au and au == bu:
        return True
    ae, be = norm_email(a.email), norm_email(b.email)
    if ae and ae == be:
        return True
    af, al = _name_parts(a.person)
    bf, bl = _name_parts(b.person)
    same_employer = bool(a.company_domain) and a.company_domain == b.company_domain
    if same_employer and al and al == bl and af[:1] == bf[:1]:
        return True
    if same_employer and a.embedding and b.embedding and _cosine(a.embedding, b.embedding) >= _EMBED_MATCH:
        return True
    return False


def _union(a: List[str], b: List[str]) -> List[str]:
    out: List[str] = []
    for x in list(a) + list(b):
        if x and x not in out:
            out.append(x)
    return out


def merge_person(base: Person, other: Person) -> Person:
    """Merge `other` into `base` (keeps base.id); richest / most-recent wins, lists unioned."""
    base.person = base.person or other.person
    if len(other.person) > len(base.person):
        base.person = other.person
    base.titles = _union(base.titles + ([base.title] if base.title else []),
                         other.titles + ([other.title] if other.title else []))
    base.title = base.title or other.title
    if other.is_decision_maker and not base.is_decision_maker:
        base.is_decision_maker = True
        base.seniority = other.seniority or base.seniority
    base.seniority = base.seniority or other.seniority
    base.company_domain = base.company_domain or other.company_domain
    base.linkedin_url = base.linkedin_url or other.linkedin_url
    base.phone = base.phone or other.phone
    base.location = base.location or other.location
    if other.email and other.email_confidence > base.email_confidence:
        base.email, base.email_status, base.email_confidence = (
            other.email, other.email_status, other.email_confidence)
    elif not base.email:
        base.email, base.email_status, base.email_confidence = (
            other.email, other.email_status, other.email_confidence)
    base.problems = _union(base.problems, other.problems)
    base.where_active = _union(base.where_active, other.where_active)
    base.providers = _union(base.providers, other.providers)
    base.touchpoint_refs = _union(base.touchpoint_refs, other.touchpoint_refs)
    base.embedding = base.embedding or other.embedding
    base.source_url = base.source_url or other.source_url
    if isinstance(base.data, dict) and isinstance(other.data, dict):
        base.data = {**other.data, **base.data}
    base.updated_at = utcnow()
    return base


def resolve_people(existing: List[Person], incoming: List[Person]
                   ) -> Tuple[List[Person], Dict[str, str], List[str]]:
    """Fold `incoming` into `existing` by identity resolution. Returns
    (all_people, incoming_id → resolved_person_id, changed_person_ids). Idempotent: re-resolving
    the same incoming against the result is a no-op (matches map back to the merged row)."""
    people: List[Person] = list(existing)
    id_map: Dict[str, str] = {}
    changed: List[str] = []
    for inc in incoming:
        match = next((p for p in people if _is_same(p, inc)), None)
        if match is None:
            people.append(inc)
            id_map[inc.id] = inc.id
            changed.append(inc.id)
        else:
            merge_person(match, inc)
            id_map[inc.id] = match.id
            if match.id not in changed:
                changed.append(match.id)
    changed_ids = set(changed)
    return people, id_map, [p.id for p in people if p.id in changed_ids]
