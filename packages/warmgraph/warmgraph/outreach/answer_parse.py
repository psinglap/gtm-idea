"""Map one free-text reply onto the registration questions it answers.

You get a list of questions and a single box. You write "we're pre-seed, 3 people, and yes I'm
technical" and this works out which answer belongs to which question.

The one rule: **it must refuse rather than guess.** These answers are submitted to real event
hosts who read them. A wrong answer attached to the wrong question is worse than no answer, and
it is invisible unless someone checks — which is why the caller shows the mapping for
confirmation before anything is saved, and why anything uncertain comes back blank.
"""
from __future__ import annotations

from typing import Dict, List

from warmgraph.jsonutil import extract_json
from warmgraph.outreach.registration import match_option, normalise

TASK = "registration_answer_parse"

_SYSTEM = (
    "You match a person's free-text reply to the specific questions it answers.\n"
    "RULES:\n"
    "- Only answer a question if the reply CLEARLY addresses it. If you are unsure, leave it "
    "out. A missing answer costs one more question; a wrong answer is submitted to a real "
    "event host under this person's name.\n"
    "- Use the person's own words. Do not embellish, expand, or invent detail they did not "
    "give.\n"
    "- Keep answers short and in the form the question expects (a number for 'how many', "
    "yes/no for a yes/no question).\n"
    "- When a question lists CHOICES, the answer must be one of them copied EXACTLY, character "
    "for character. These are click-only dropdowns; anything else cannot be entered at all. If "
    "the reply does not clearly pick one, leave the question out.\n"
    "- Never answer a question the reply does not mention.\n"
    "Return STRICT JSON only."
)


def parse_answers(registry, reply: str, questions: List[dict]) -> Dict[str, str]:
    """{question_key: answer} for the questions the reply actually addresses.

    `questions` are `{key, label}` as surfaced in the UI. Returns {} when there is no LLM, an
    empty reply, or nothing could be matched — never a partial guess.
    """
    text = (reply or "").strip()
    if not text or not questions or not getattr(registry, "has_llm", False):
        return {}

    listing = "\n".join(
        f'{i + 1}. [{q.get("key")}] {q.get("label")}'
        + (f'\n   choices (copy one exactly): {" | ".join(q.get("options") or [])}'
           if q.get("options") else "")
        for i, q in enumerate(questions))
    user = (
        f"Questions:\n{listing}\n\n"
        f"Their reply:\n{text}\n\n"
        'Return ONLY JSON: {"answers":[{"key":"<the key in brackets>","answer":"<their answer>"}]}'
        " — include ONLY questions the reply clearly answers."
    )
    raw = registry.complete(TASK, _SYSTEM, user, max_tokens=800, want_json=True)
    parsed = extract_json(raw) or {}

    by_key = {q.get("key"): q for q in questions if q.get("key")}
    out: Dict[str, str] = {}
    for row in parsed.get("answers", []):
        if not isinstance(row, dict):
            continue
        key, answer = row.get("key"), str(row.get("answer") or "").strip()
        # Only keys we actually asked about: a model inventing a key would otherwise write a
        # junk entry into the answer bank that is then reused forever.
        if key not in by_key or not answer:
            continue
        options = by_key[key].get("options") or []
        if options:
            # Told to copy an option exactly; held to it here. A near-miss is unusable anyway —
            # the widget only accepts a click on a listed row — so it stays an open question.
            answer = match_option(answer, options) or ""
            if not answer:
                continue
        out[key] = answer
    return out


def unanswered(questions: List[dict], mapped: Dict[str, str]) -> List[dict]:
    """Questions still open after a parse — shown as blank rows so the gap is visible."""
    return [q for q in questions if not mapped.get(q.get("key"))]


def as_bank_entries(questions: List[dict], mapped: Dict[str, str]) -> Dict[str, str]:
    """Keys for the answer bank. Stored under the normalised question text so the same question
    at a different event, worded slightly differently, still hits this answer."""
    by_key = {q.get("key"): q for q in questions}
    out: Dict[str, str] = {}
    for key, answer in (mapped or {}).items():
        q = by_key.get(key)
        out[normalise(q.get("label", "")) if q else key] = answer
    return out
