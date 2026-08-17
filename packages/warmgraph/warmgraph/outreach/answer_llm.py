"""Answer the registration questions a profile cannot, from what we know about the company.

Most forms ask two kinds of thing. Some are facts — headcount, ARR, how much you raised, whether
you have applied for a tax credit. Others are discretionary — what you would like to ask the
speaker, what you hope to get out of the evening, which of fifteen "bingo card" claims describe
you. The first kind has a right answer that only the founder knows. The second kind has no wrong
answer as long as it is grounded in what the company actually is.

Blocking an event on the second kind is what made a run of six deliver three. So a model writes
those, from the answer bank as context, and the first kind still goes to a human.

  **The split is the whole design.** It is not "hard questions to the human, easy ones to the
  model" — it is "questions with a verifiable answer to the person who knows it, questions with
  a discretionary answer to the model". A model inventing "$1mn ARR" is a lie to a host that
  neither we nor they can detect. A model writing a sensible question for a voice-agents speaker
  is just doing the work.

Refusing is always available and always safe: an unanswered question goes to the box, exactly as
before. Nothing here can make a question *more* answered than it was — it only fills gaps that
`plan_answers` left, and choice questions are still gated by `match_option`.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

from warmgraph.jsonutil import extract_json
from warmgraph.outreach.registration import match_option, normalise

TASK = "registration_answer_write"

# Same key the browser uses for the terms checkbox and the signature pad. Consent is one
# decision by the user, not three.
ACCEPT_TERMS_KEY = "accept event terms"

# Questions a model must never answer, however confident it sounds. Each has a single true value
# that lives only in the founder's head, and a wrong one is submitted under her name.
#
# This deliberately overlaps `registration._QUANTITATIVE`: that guard stops a KEYWORD match
# answering a number question with a company name, this one stops a MODEL inventing the number.
# Different failure, same question, and the cost of listing it twice is nothing.
_PRIVATE_FACT = re.compile(
    r"\b(arr|mrr|revenue|turnover|profit|valuation|raised|raising|funding|fundrais\w*|"
    r"runway|burn|budget|salary|comp(ensation)?|"
    r"how (many|much)|headcount|team size|number of (employees|people)|"
    r"have you (ever |already )?(applied|filed|registered|used|tried|heard)|"
    r"do you (currently )?(have|own|manage|lead|hold)|are you (currently|a) |"
    r"visa|citizenship|immigration|passport|dietary|allerg|accessib|pronoun|"
    r"phone|address|date of birth|dob)\b", re.I)

_SYSTEM = (
    "You fill in event registration answers on behalf of a founder, using ONLY the facts given "
    "about her company. These go to real event hosts who read them.\n\n"
    "ANSWER a question when it is discretionary — an opinion, an intention, a question for a "
    "speaker, what she hopes to get from the event, which listed statements describe her "
    "company. There is no wrong answer to these as long as it follows from the company facts.\n\n"
    "REFUSE a question when it has one true answer you were not told: revenue, headcount, money "
    "raised, whether she has done some specific thing before, anything about her person. "
    "Omit it. Omitting costs one prompt to her; inventing it puts a false statement in front of "
    "a host under her name.\n\n"
    "STYLE: first person, plain, specific to the company. One or two sentences. No marketing "
    "language, no em-dashes, no exclamation marks. Never invent a metric, customer, or claim "
    "that is not in the facts.\n\n"
    "When a question lists CHOICES, copy one EXACTLY, character for character — they are "
    "click-only dropdowns and anything else cannot be entered. If several are true, pick the one "
    "best supported by the facts.\n"
    "Return STRICT JSON only."
)


def context_lines(answers: Dict[str, str], icp: str = "") -> str:
    """The company facts a model is allowed to reason from — the answer bank, which is exactly
    the set of things the founder has already confirmed true."""
    lines = []
    for key in ("company", "building", "role", "website", "linkedin"):
        if answers.get(key):
            lines.append(f"- {key}: {answers[key]}")
    # Everything else she has answered before, minus the operational keys.
    for key, value in sorted((answers or {}).items()):
        if key in ("company", "building", "role", "website", "linkedin", "name", "first_name",
                   "last_name", "email", "referral", "accept event terms") or not value:
            continue
        lines.append(f"- {key}: {value}")
    if icp:
        lines.append(f"- who she wants to meet: {icp}")
    return "\n".join(lines)


# Some forms take consent as a TEXT FIELD rather than a checkbox: 'Type "I agree" to accept the
# privacy policy', 'Type "I agree" to confirm you will not bring regulated patient data'. A model
# happily writes "I agree" — it reads like a trivial instruction — but it is agreeing to a policy
# and making a commitment about the user's conduct, under her name, to a host who relies on it.
# Consent is never authored here. It is gated by the user's explicit permission, and the phrase
# is copied out of the label rather than composed. See consent_phrase().
_TYPED_CONSENT = re.compile(
    r'\btype\s+["“\']?\s*(i\s+agree|i\s+consent|i\s+accept|yes)\b'
    r'|\bconsent\b.*\btype\b|\btype\b.*\bto\s+(confirm|accept|agree|consent)\b', re.I)


def is_typed_consent(question: str) -> bool:
    """A consent the form wants typed out. Never a model's to write."""
    return bool(_TYPED_CONSENT.search(question or ""))


def consent_phrase(question: str) -> Optional[str]:
    """The exact phrase the label asks for — copied, never composed.

    Returns None when the label does not spell one out, so an unrecognised consent stays an open
    question rather than being answered with a guess at what it wants.
    """
    m = re.search(r'type\s+["“\']?\s*([^"”\'.,]+?)\s*["”\']?\s*(?:to\b|$)',
                  question or "", re.I)
    if not m:
        return None
    phrase = " ".join(m.group(1).split())
    return phrase if 1 <= len(phrase) <= 40 else None


def may_answer(question: str) -> bool:
    """False for questions with one true answer we were not told, and for consent."""
    q = question or ""
    return not (_PRIVATE_FACT.search(q) or _TYPED_CONSENT.search(q))


def write_answers(registry, questions: List[dict], answers: Dict[str, str],
                  icp: str = "", event: str = "") -> Dict[str, str]:
    """{question_id: answer} for the open questions a model may legitimately write.

    `questions` are the `unanswered` rows from `plan_answers`. `event` is the event's own title,
    and it matters more than it looks: asked "what would you like to ask the speaker?" with no
    event context, the model produced a perfectly sensible question about customer acquisition
    for a talk on voice-agent benchmarking. Grounded only in the company, every event gets the
    same question — which is exactly what a host notices.

    Returns {} with no LLM. Anything refused, or any choice answer that is not one of the offered
    options, is simply absent — the caller then reports it as open, which is the pre-existing
    behaviour.
    """
    if not questions or not getattr(registry, "has_llm", False):
        return {}

    allowed = [q for q in questions if may_answer(q.get("label", ""))]
    if not allowed:
        return {}

    listing = "\n".join(
        f'{i + 1}. [{q.get("id")}] {q.get("label")}'
        + (f'\n   choices (copy one exactly): {" | ".join(q.get("options") or [])}'
           if q.get("options") else "")
        for i, q in enumerate(allowed))

    user = ((f"The event: {event}\n\n" if event else "")
            + f"Company facts:\n{context_lines(answers, icp)}\n\n"
            f"Questions:\n{listing}\n\n"
            + ("Anything addressed to a speaker or about why she is attending must be specific "
               "to THIS event's subject, not generic.\n\n" if event else "")
            + 'Return ONLY JSON: {"answers":[{"id":"<the id in brackets>","answer":"<answer>"}]}'
            " — omit any question you cannot answer from the facts above.")
    try:
        raw = registry.complete(TASK, _SYSTEM, user, max_tokens=900, want_json=True,
                                temperature=0.2)
    except Exception:
        return {}                       # fails CLOSED: the question stays open, nothing is faked

    by_id = {q.get("id"): q for q in allowed}
    out: Dict[str, str] = {}
    for row in (extract_json(raw) or {}).get("answers", []):
        if not isinstance(row, dict):
            continue
        qid, answer = row.get("id"), str(row.get("answer") or "").strip()
        q = by_id.get(qid)
        if not q or not answer:
            continue
        options = q.get("options") or []
        if options:
            # Same gate as a human answer. A model told to copy an option exactly is still held
            # to it, so a near-miss becomes an open question rather than an unusable submission.
            answer = match_option(answer, options) or ""
            if not answer:
                continue
        out[qid] = answer
    return out


def fill_open(registry, filled: Dict[str, str], open_qs: List[dict],
              answers: Dict[str, str], icp: str = "", event: str = "") -> tuple:
    """(filled, still_open) — `plan_answers` output with the discretionary gaps written in.

    The one call sites should use, so the ordering is never got wrong: the answer bank always
    wins, and a model only ever sees what the bank could not answer.
    """
    written = dict(write_answers(registry, open_qs, answers, icp, event))

    # Typed consent, only if the user has granted it, and only ever the phrase the label itself
    # asks for. Same permission that governs the terms checkbox and the signature pad — consent
    # given once, in chat, rather than inferred per form.
    if str(answers.get(ACCEPT_TERMS_KEY, "")).strip().lower() == "yes":
        for q in open_qs:
            label = q.get("label", "")
            if not is_typed_consent(label):
                continue
            phrase = consent_phrase(label)
            if phrase:
                written[q.get("id")] = phrase

    if not written:
        return filled, open_qs
    merged = {**filled, **written}
    return merged, [q for q in open_qs if q.get("id") not in written]


def is_llm_written(question: str) -> Optional[bool]:
    """Whether this question is one a model may write, for showing provenance in the UI."""
    return may_answer(question)


__all__ = ["ACCEPT_TERMS_KEY", "context_lines", "may_answer", "is_typed_consent",
           "consent_phrase", "write_answers", "fill_open", "normalise"]
