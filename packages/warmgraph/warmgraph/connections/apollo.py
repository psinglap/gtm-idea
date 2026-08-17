"""Apollo connection — API key validation + the one enrichment call the pipeline makes.

Apollo has no OAuth, so the credential is a pasted API key stored Fernet-encrypted like any
other secret. Each person brings their own key and spends their own credits.

The pipeline only ever calls People Enrichment keyed on a LinkedIn URL, and only for attendees
the ICP judge already marked `target` — credits are spent on people we would actually email.
"""
from __future__ import annotations

from typing import Optional

import httpx

from warmgraph.connections import crypto
from warmgraph.entities import Connection
from warmgraph.models import utcnow

API = "https://api.apollo.io/v1"
_TIMEOUT = httpx.Timeout(30.0)


class ApolloError(RuntimeError):
    pass


def _headers(api_key: str) -> dict:
    return {"Content-Type": "application/json", "Cache-Control": "no-cache",
            "X-Api-Key": api_key}


def validate(api_key: str) -> bool:
    """Cheap credential check so a bad paste fails at Connect time, not at 3am in the cron."""
    if not api_key:
        return False
    try:
        r = httpx.get(f"{API}/auth/health", timeout=_TIMEOUT, headers=_headers(api_key))
    except httpx.HTTPError:
        return False
    return r.status_code == 200


def connect(company_id: str, api_key: str) -> Connection:
    ok = validate(api_key)
    return Connection(
        company_id=company_id, provider="apollo",
        status="connected" if ok else "error",
        account_label="Apollo API key",
        secret=crypto.encrypt(api_key),
        last_ok_at=utcnow() if ok else None,
        last_error="" if ok else "Apollo rejected this API key.",
    )


def match_by_linkedin(api_key: str, linkedin_url: str, name: str = "") -> Optional[dict]:
    """People Enrichment by LinkedIn URL -> the person payload, or None when Apollo has no match.

    Costs ~1 credit per successful match. `reveal_personal_emails` is left OFF on purpose: we
    want the work address, and personal emails cost more and land worse.
    """
    if not (api_key and linkedin_url):
        return None
    # LINKEDIN URL ONLY — never the name, even though the caller has one.
    #
    # A name is not an identity. Sending it as well means that when the URL misses, Apollo can
    # still match on the name and return SOMEONE ELSE of the same name — whose title, employer
    # and email then get written onto this contact and eventually emailed. Verified live: a
    # name-only lookup for a person Apollo does not hold returns a hollow invented record
    # ({"name": ..., "title": null, "email": null}), so the name buys nothing on a miss and
    # risks a wrong person on a hit. The URL is the only key that IS the identity.
    payload = {"linkedin_url": linkedin_url}
    try:
        r = httpx.post(f"{API}/people/match", timeout=_TIMEOUT, headers=_headers(api_key),
                       json=payload)
    except httpx.HTTPError as e:
        raise ApolloError(f"Apollo request failed: {e}") from e
    if r.status_code == 401:
        raise ApolloError("Apollo rejected the API key (401) — reconnect Apollo.")
    if r.status_code == 429:
        raise ApolloError("Apollo rate limit / out of credits (429).")
    if r.status_code != 200:
        raise ApolloError(f"Apollo people/match failed ({r.status_code}): {r.text[:160]}")
    return (r.json() or {}).get("person") or None


BULK_MAX = 10          # Apollo's documented cap per bulk_match request


def bulk_match_by_linkedin(api_key: str, linkedin_urls: list) -> list:
    """Up to BULK_MAX LinkedIn URLs -> a list of person payloads (None where Apollo has no match),
    positionally aligned with the input.

    Same credit cost as matching one at a time — a credit per person MATCHED, misses are free —
    but one HTTP round trip per ten people instead of ten. At 130 attendees per event that is the
    difference between a handful of requests and a hundred and thirty.
    """
    urls = [u for u in (linkedin_urls or []) if u][:BULK_MAX]
    if not (api_key and urls):
        return []
    try:
        r = httpx.post(f"{API}/people/bulk_match", timeout=_TIMEOUT, headers=_headers(api_key),
                       json={"details": [{"linkedin_url": u} for u in urls]})
    except httpx.HTTPError as e:
        raise ApolloError(f"Apollo request failed: {e}") from e
    if r.status_code == 401:
        raise ApolloError("Apollo rejected the API key (401) — reconnect Apollo.")
    if r.status_code == 429:
        raise ApolloError("Apollo rate limit / out of credits (429).")
    if r.status_code != 200:
        raise ApolloError(f"Apollo bulk_match failed ({r.status_code}): {r.text[:160]}")
    matches = (r.json() or {}).get("matches") or []
    # Pad rather than zip: a short list would silently shift every later person's data onto the
    # wrong contact, which is exactly the kind of error nobody notices until an email is wrong.
    return list(matches) + [None] * (len(urls) - len(matches))


def person_fields(person: dict) -> dict:
    """Flatten Apollo's payload to the handful of fields the pipeline stores.

    `email_status` matters: Apollo returns guesses as well as verified addresses, and sending to
    a guess is how a sending reputation gets burned. The caller keeps only 'verified'.
    """
    org = person.get("organization") or {}
    return {
        "email": (person.get("email") or "").strip(),
        "email_status": (person.get("email_status") or "").strip(),
        "title": (person.get("title") or "").strip(),
        # Apollo returns a headline ("Co-founder/CTO at Soulside | AI/ML Engineering Leader")
        # and it is the single most judgeable field it gives us. Dropping it here meant every
        # Apollo-enriched contact looked profileless to the ICP judge.
        "headline": (person.get("headline") or "").strip(),
        # A CATCH-ALL domain accepts mail to any address, so "verified" degrades to "the server
        # said yes" — it does NOT mean the mailbox belongs to this person. Apollo reports this
        # and we ignored it: a verified-looking address at a catch-all domain came back verified, did not exist,
        # never bounced, and landed in a colleague's inbox who replied "There is no Gregory
        # here." A silent wrong-recipient is worse than a bounce, because nothing detects it.
        "email_domain_catchall": bool(person.get("email_domain_catchall")),
        "seniority": (person.get("seniority") or "").strip(),
        "first_name": (person.get("first_name") or "").strip(),
        "last_name": (person.get("last_name") or "").strip(),
        "company_name": (org.get("name") or "").strip(),
        "company_domain": (org.get("primary_domain") or org.get("website_url") or "").strip(),
        "company_industry": (org.get("industry") or "").strip(),
        "location": ", ".join(x for x in [person.get("city"), person.get("country")] if x),
        "linkedin_url": (person.get("linkedin_url") or "").strip(),
    }
