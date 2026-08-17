"""Person-level prospect + pitch engine (restored). From the relevant social posts, pick the authors
who are GENUINE prospects (expressing the pain / seeking a solution / unhappy with a competitor — not
vendors or our own competitors) and write a short, specific, founder-voice reply that references their
actual post. Annotates the posts in place (recommended_pitch + tier) and returns CustomerLeads.

The pitch is customer-specific (it names our product), so it is set on the in-memory report posts and
persisted in customer_leads — NEVER written back into the shared `posts` corpus (which stays reusable)."""
from __future__ import annotations

from typing import List

from warmgraph.jsonutil import extract_json
from warmgraph.models import CustomerLead, Post, Profile

_SYSTEM = (
    "You are a founder's GTM analyst. From real posts where people discuss a problem, pick the authors "
    "who are GENUINE PROSPECTS for our company (expressing the pain, seeking a solution, or unhappy with "
    "a competitor) — skip vendors, our own competitors, and pure commentary. For each, write a SHORT, "
    "specific, founder-voice outreach reply that references their actual post (no generic templates). "
    "tier: 'tier 1' (hot — explicit pain/intent), 'tier 2' (relevant), 'tier 3' (nurture). Infer company "
    "only if reasonably implied; else leave blank. Be honest — return fewer, better prospects, not filler."
)
_SCHEMA = ('Return ONLY JSON:\n'
           '{"leads":[{"person":"","company":"","evidence":"(quote their post)","source_url":"",'
           '"tier":"tier 1","pitch":""}]}')

_PROSPECT_SIGNALS = {"seeking_solution", "comparing", "complaining", "asking_advice", ""}


def pitch_posts(reg, profile: Profile, posts: List[Post]) -> List[CustomerLead]:
    """Annotate prospect posts with recommended_pitch + tier; return the matching CustomerLeads."""
    if not posts or not reg.has_llm:
        return []
    cand = [p for p in posts
            if p.author and (p.text or p.title) and p.platform != "news"
            and p.signal_type in _PROSPECT_SIGNALS]
    cand.sort(key=lambda p: p.relevance, reverse=True)
    sample = cand[:40]
    if not sample:
        return []

    company = profile.profile
    by_url = {p.url: p for p in sample if p.url}
    lines = [
        f"{i}. [{p.platform}] @{p.author} | pain: {p.problem_theme or '—'} | "
        f"{(p.text or p.title)[:180]} | {p.url}"
        for i, p in enumerate(sample)
    ]
    user = (
        f"Our company: {company.name} — {company.what_they_do} ({company.category}).\n"
        f"Who we sell to: {company.value_proposition}\n\n"
        f"Candidate prospects (each already flagged as discussing the problem):\n"
        + "\n".join(lines) + f"\n\n{_SCHEMA}"
    )
    d = extract_json(reg.complete("prospects", _SYSTEM, user, max_tokens=2600, want_json=True)) or {}

    leads: List[CustomerLead] = []
    for L in d.get("leads", []):
        person, pitch = str(L.get("person", "")), str(L.get("pitch", ""))
        if not person and not pitch:
            continue
        url = str(L.get("source_url", ""))
        tier = str(L.get("tier", ""))
        src = by_url.get(url)
        if src is not None:
            src.recommended_pitch = pitch
            src.tier = tier
        leads.append(CustomerLead(
            subject_domain=profile.domain, person=person,
            person_handle=(src.author_handle if src else ""),
            company=str(L.get("company", "")), source="post", source_url=url,
            evidence=str(L.get("evidence", "")), signal_types=["problem_discussion"], tier=tier,
            intent={"tier 1": 0.9, "tier 2": 0.6, "tier 3": 0.3}.get(tier.lower(), 0.5),
            recommended_pitch=pitch,
        ))
    return leads
