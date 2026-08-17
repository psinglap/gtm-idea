"""Event ICP judge — decide which event attendees are worth emailing.

TWO GATES, both cheap to state and expensive to get wrong:

  1. **A profile.** Never judge on a name — see has_profile_text().
  2. **An email.** Judging costs an LLM call, and a verdict on someone we cannot email is a
     verdict nobody will ever act on. Enrichment runs before this, so a contact without an
     address simply is not ready: it waits rather than being judged speculatively.

THE HARD GATE: this agent only ever looks at rows in `profiled` status, i.e. rows where the
browser worker actually read the person's LinkedIn profile. A Luma display name plus a one-line
bio cannot identify a person, and judging on that produces confident nonsense. No profile text,
no verdict — the row waits in the queue instead.

The prompt is ported from `luma-icp-scout/lib/judge.js`, which was tuned against live events.
Its three hard-won rules are kept verbatim in spirit:
  1. judge on the CURRENT / most-recent role only — never penalise a founder for a past internship
  2. use ONLY facts in the profile — never invent a title, company, seniority or company size
  3. when the text is too thin to tell, answer NO

The ICP comes from the stored company profile (`derive_profile` + `derive_icp`), so there is no
second ICP to maintain, and the user's own approve/reject history is fed back in as few-shot
examples exactly as `fewShot()` does in the extension.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel

from warmgraph.agents.base import Agent
from warmgraph.entities import EventContact
from warmgraph.jsonutil import extract_json
from warmgraph.outreach import profile_cache
from warmgraph.outreach import icp_rules
from warmgraph.storage import mirror

TASK = "event_icp_judge"

# WHO COUNTS AS A TARGET IS CONFIGURATION, NOT CODE — see warmgraph.outreach.icp_rules.
#
# These lists used to live here. They are the first thing anyone running this has to change, and
# having them in a module meant the one unavoidable edit was the least discoverable one. They are
# now `config/icp.json` (copy `config/icp.example.json`), with `WG_ICP_FILE` to point elsewhere.
#
# Read at call time, not at import, so editing the file does not need a restart — and so a test
# can point at its own file without reloading this module.
#
# The names are kept because a per-workspace override already reads them, and because the shipped
# example still has to come from somewhere when no file exists.
def _rules():
    return icp_rules.load()


DEFAULT_TARGET_ROLES = icp_rules.BUILTIN_TARGET_ROLES
NOT_TARGET_ROLES = icp_rules.BUILTIN_NOT_TARGET_ROLES


def system_prompt(never_targets=None) -> str:
    """The judge's instructions. The absolute exclusions are injected from config rather than
    written in here, because a rule the user can see in a file and cannot change is worse than no
    rule: it looks configurable and is not."""
    never = never_targets if never_targets is not None else _rules().never_targets
    lines = [
        "You qualify event attendees against the user's ideal-customer profile (ICP).",
        "",
    ]
    if never:
        # Stated first, in absolute terms. Enforcing an exclusion only at the delivery gate still
        # spends an Apollo credit and a judgement on someone who can never be written to — and
        # reads, to anyone watching, as the rule not existing.
        lines.append("NEVER a target, whatever else is true about them:")
        lines += [f"  - {n}" for n in never]
        lines.append("")
    lines += [
        "STRICT RULES:",
        "- The test is the FUNCTION of their current role, NOT whether they could buy today. "
        "Never reject someone for lacking a budget for what you sell.",
        "- Industry is irrelevant. AI, infrastructure, B2B, consumer — a founder is a founder.",
        "- TECHNICAL founders are targets and among the strongest: someone who can build a "
        "product but has no distribution is exactly who this is for. Never reject a founder for "
        "being technical.",
        "- Reject only when NONE of the target criteria are met at all.",
        "- Judge each attendee ONLY on their CURRENT / most-recent role (headline + latest "
        "position). Do NOT penalise PAST internships or junior roles if they are now a founder, "
        "growth, or marketing person.",
        "- Use ONLY facts stated in the profile. NEVER invent job titles, companies, seniority, "
        "or company size.",
        "- If the profile is too thin to tell, is_target must be false.",
        "- Only mark is_target true when the current role clearly matches the ICP. The reason "
        "must quote real evidence from their current role.",
        "Return STRICT JSON only.",
    ]
    return "\n".join(lines)


_SCHEMA = ('Return ONLY JSON: {"results":[{"i":<index>,"is_target":<bool>,"score":<0-10>,'
           '"reason":"<short why, grounded in their profile>"}]} — one entry per attendee.')

# Batch size for the LLM call. Small enough that one bad row can't poison a whole event, big
# enough that a 300-person guest list isn't 300 round trips.
BATCH = 10


def icp_statement(profile, target_roles: Optional[List[str]] = None) -> str:
    """Natural-language ICP for the judge, built from the stored profile plus the explicit role
    floor. The derived ICP alone is too soft to stop the judge approving anyone senior."""
    icp = getattr(profile, "icp", None)
    cp = getattr(profile, "profile", None)
    parts: List[str] = []
    # Precedence: an explicit per-workspace override, then the configured file, then the shipped
    # example. The exclusions come from the same source as the targets — mixing a user's target
    # list with the example's exclusion list would produce an ICP neither of them wrote.
    rules = _rules()
    roles = target_roles or rules.target_roles
    parts.append("A person is ONLY a target if their CURRENT role is one of these, or clearly "
                 "equivalent:")
    parts += [f"  - {r}" for r in roles]
    if rules.not_target_roles:
        parts.append("These are explicitly NOT targets, however senior or adjacent they look:")
        parts += [f"  - {r}" for r in rules.not_target_roles]
    parts.append("Anyone else is not a target, however senior.")
    if cp is not None:
        parts.append(f"Our company: {cp.name} — {cp.what_they_do}")
        if cp.value_proposition:
            parts.append(f"Value proposition: {cp.value_proposition}")
    if icp is not None:
        for p in (icp.personas or [])[:6]:
            pains = "; ".join(p.pains[:3])
            parts.append(f"- Target persona: {p.role} ({p.seniority})"
                         + (f" — pains: {pains}" if pains else ""))
        for s in (icp.segments or [])[:4]:
            parts.append(f"- Target company type: {s.name} — {s.firmographics}")
        if icp.winning_category:
            parts.append(f"Where we win: {icp.winning_category}")
    return "\n".join(parts).strip() or "(no ICP available)"


def few_shot(feedback) -> str:
    """The user's own approve/reject history — this is how the judge learns their taste over
    time, rather than staying frozen at whatever the prompt says."""
    if not feedback:
        return ""
    good, bad = [], []
    for f in feedback:
        line = f"  {f.lead_text or f.company}"
        if f.reason_text:
            line += f" (user's reason: {f.reason_text})"
        (good if f.decision == "approve" else bad).append(line)
    if not good and not bad:
        return ""
    out = ["\n\nThe user labelled these before, with their reasons. Learn the pattern and apply "
           "the SAME judgment to similar profiles:"]
    out += [f"GOOD FIT:\n{x}" for x in good[-8:]]
    out += [f"NOT A FIT:\n{x}" for x in bad[-8:]]
    return "\n".join(out)


def person_text(c: EventContact) -> str:
    """The blob the judge sees, labelling CURRENT vs possibly-past so rule 1 is enforceable."""
    parts = []
    if c.name:
        parts.append(f"Name: {c.name}")
    if c.linkedin_headline:
        parts.append(f"Current headline: {c.linkedin_headline}")
    # Apollo's structured title/employer, when that is where the profile came from. Without
    # this the judge saw a name and an event bio and nothing about the person's actual job.
    if c.title or c.company_name:
        role = " at ".join(x for x in [(c.title or "").strip(), (c.company_name or "").strip()] if x)
        parts.append(f"Current role: {role}")
    if c.luma_bio:
        parts.append(f"Event bio: {c.luma_bio}")
    if c.linkedin_text:
        parts.append("Full profile (may include PAST/junior roles — do not penalise those): "
                     + c.linkedin_text[:1500])
    return "\n".join(parts)


def has_profile_text(c: EventContact) -> bool:
    """The gate itself. A name alone is never enough.

    A profile can come from LinkedIn (headline / full text) OR from Apollo (title + employer).
    "Investment Partner at Infinity Labs" is exactly what this judge needs, and where it came
    from is irrelevant. Checking only the LinkedIn fields sent 49 perfectly judgeable people
    back to `queued` — which, now that enrichment runs first, would have re-billed Apollo for
    data already sitting on the row.
    """
    return bool((c.linkedin_headline or "").strip()
                or (c.linkedin_text or "").strip()
                or ((c.title or "").strip() and (c.company_name or "").strip()))


def judge_batch(registry, contacts: List[EventContact], icp: str, shots: str) -> List[dict]:
    """One LLM call for up to BATCH people. Returns raw verdict dicts keyed by index."""
    listing = "\n\n".join(f"#{i}: {person_text(c)}" for i, c in enumerate(contacts))
    user = f"ICP:\n{icp}{shots}\n\nAttendees:\n{listing}\n\n{_SCHEMA}"
    raw = registry.complete(TASK, system_prompt(), user, max_tokens=1800, want_json=True)
    d = extract_json(raw) or {}
    return [r for r in d.get("results", []) if isinstance(r, dict)]


def apply_verdicts(contacts: List[EventContact], results: List[dict], judged_by: str) -> None:
    """Merge verdicts back onto the rows. Anyone the model skipped stays unjudged rather than
    being defaulted to reject — a missing answer is not a 'no'."""
    by_index = {int(r["i"]): r for r in results if str(r.get("i", "")).lstrip("-").isdigit()}
    for i, c in enumerate(contacts):
        r = by_index.get(i)
        if r is None:
            c.last_error = "judge returned no verdict"
            continue
        is_target = bool(r.get("is_target"))
        c.verdict = "target" if is_target else "reject"
        c.status = "judged" if is_target else "rejected"
        try:
            c.score = float(r.get("score") or 0)
        except (TypeError, ValueError):
            c.score = 0.0
        c.reason = str(r.get("reason", ""))[:400]
        c.judged_by = judged_by
        c.last_error = ""


class EventIcpJudgeInput(BaseModel):
    url: str
    limit: int = 200            # how many profiled rows to judge this pass
    event_id: Optional[str] = None


class EventIcpJudgeReport(BaseModel):
    subject_domain: str
    judged: int = 0
    targets: int = 0
    rejected: int = 0
    skipped_no_profile: int = 0


class EventIcpJudgeAgent(Agent):
    name = "event_icp_judge"
    description = ("Judge event attendees against the company's ICP using their READ LinkedIn "
                   "profile. Rows without profile text are never judged — they stay queued.")
    InputModel = EventIcpJudgeInput
    OutputModel = EventIcpJudgeReport

    def run(self, inp: EventIcpJudgeInput) -> EventIcpJudgeReport:
        store, reg = self.ctx.store, self.ctx.registry
        profile = self.ctx.get_or_build_profile(inp.url)
        domain = profile.domain
        report = EventIcpJudgeReport(subject_domain=domain)
        if store is None:
            return report

        cid = mirror.client_id_for(store, domain)
        # BOTH ways a contact acquires a profile. "profiled" is the LinkedIn worker's output;
        # "enriched" is Apollo's, and Apollo is now the primary path — it answers ~86% of people.
        # Reading only "profiled" meant every Apollo-enriched contact went straight to delivery
        # unjudged, so the ICP was never applied to the people it was written for. It reported
        # judged: 0 on every run, which looked like an empty queue rather than a skipped stage.
        pending = [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                       status="profiled", limit=inp.limit)]
        pending += [c for c in store.get_event_contacts(cid, event_id=inp.event_id,
                                                        status="enriched", limit=inp.limit)]
        if not pending:
            return report

        # AN EMAIL IS A PRECONDITION FOR JUDGING, not a consequence of it.
        #
        # Judging costs an LLM call, and a verdict on someone we cannot email is a verdict
        # nobody will ever act on. On one event that was 58 of 111 people — over half the pass
        # spent deciding about people with no way to reach them. Enrichment runs first now, so
        # the ones without an address are simply not ready yet: they wait here, and get judged
        # the moment an email turns up, rather than being judged speculatively or dropped.
        pending = [c for c in pending if (c.email or "").strip()]
        if not pending:
            return report

        # Defensive: a row can only reach `profiled` via enrichment or the LinkedIn worker, but
        # if one ever gets here without a profile, send it back rather than judging on a name.
        ready = [c for c in pending if has_profile_text(c)]
        stray = [c for c in pending if not has_profile_text(c)]
        for c in stray:
            c.status = "queued"
            c.last_error = "no LinkedIn text — cannot judge"
        if stray:
            store.update_event_contacts(stray)
            report.skipped_no_profile = len(stray)

        if not ready or not reg.has_llm:
            return report

        client = store.get_company_by_id(cid)
        roles = (client.data or {}).get("target_roles") if client else None
        icp = icp_statement(profile, roles or None)
        shots = few_shot(store.get_feedback(domain, limit=40))
        judged_by = reg.provider_name or "llm"

        for start in range(0, len(ready), BATCH):
            batch = ready[start:start + BATCH]
            try:
                results = judge_batch(reg, batch, icp, shots)
            except Exception as e:                     # one bad batch must not kill the run
                for c in batch:
                    c.last_error = f"judge failed: {e}"[:300]
                store.update_event_contacts(batch)
                continue
            apply_verdicts(batch, results, judged_by)
            store.update_event_contacts(batch)
            for c in batch:                     # remember, so the next event is free
                profile_cache.remember_verdict(store, cid, c)
            report.judged += sum(1 for c in batch if c.verdict)
            report.targets += sum(1 for c in batch if c.verdict == "target")
            report.rejected += sum(1 for c in batch if c.verdict == "reject")

        return report
