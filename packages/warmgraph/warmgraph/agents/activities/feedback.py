"""Preference-learning layer — turns human approve/reject feedback into four signals the listing
uses, all keyed by subject_domain so each customer's list learns its OWN ICP taste:

  1. SUPPRESS   — a rejected company never resurfaces.
  2. RULES      — reject reasons roll up into explicit exclusion rules injected into every
                  extraction prompt (the agents stop generating that kind of lead at the source).
  3. RERANK     — approved vs rejected embeddings form a preference vector; new candidates are
                  scored by similarity-to-liked minus similarity-to-rejected.
  4. FEW-SHOT   — a few approved/rejected examples (+ reasons) seed the extraction prompt so the
                  LLM mirrors the user's judgement.

Brute-force cosine centroids for now (works on SQLite + Postgres); swap to pgvector at scale."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from warmgraph.llm.embeddings import cosine
from warmgraph.models import REJECT_CATEGORIES, LeadFeedback

_MAX_RULES = 8
_MAX_EXAMPLES = 6


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _centroid(vecs: List[List[float]]) -> List[float]:
    vecs = [v for v in vecs if v]
    if not vecs:
        return []
    n = len(vecs[0])
    out = [0.0] * n
    for v in vecs:
        if len(v) == n:
            for i, x in enumerate(v):
                out[i] += x
    return [x / len(vecs) for x in out]


@dataclass
class Preferences:
    subject_domain: str = ""
    rejected_keys: set = field(default_factory=set)     # company_domain|company (suppress)
    approved_keys: set = field(default_factory=set)
    approved_centroid: List[float] = field(default_factory=list)
    rejected_centroid: List[float] = field(default_factory=list)
    exclusion_rules: List[str] = field(default_factory=list)
    approved_examples: List[str] = field(default_factory=list)   # "Company — why"
    rejected_examples: List[str] = field(default_factory=list)
    count: int = 0

    @property
    def has_feedback(self) -> bool:
        return self.count > 0

    def is_suppressed(self, company: str, company_domain: str) -> bool:
        return bool({_norm(company_domain), _norm(company)} & self.rejected_keys) and not (
            {_norm(company_domain), _norm(company)} & self.approved_keys)

    def is_approved(self, company: str, company_domain: str) -> bool:
        return bool({_norm(company_domain), _norm(company)} & self.approved_keys)

    def score(self, emb: List[float]) -> float:
        """Preference score in ~[-1,1]: closer to what you approved, farther from what you rejected."""
        if not emb:
            return 0.0
        a = cosine(emb, self.approved_centroid) if self.approved_centroid else 0.0
        r = cosine(emb, self.rejected_centroid) if self.rejected_centroid else 0.0
        return a - r


def _rule_for(fb: LeadFeedback) -> str:
    canned = REJECT_CATEGORIES.get(fb.reason_category, "")
    detail = (fb.reason_text or "").strip()
    if canned and detail:
        return f"{canned} (e.g. {detail})"
    return detail or canned


def build_preferences(feedback: List[LeadFeedback]) -> Preferences:
    p = Preferences(count=len(feedback))
    if not feedback:
        return p
    p.subject_domain = feedback[0].subject_domain
    appr_emb, rej_emb, rules = [], [], []
    seen_rule = set()
    for fb in feedback:
        keys = {_norm(fb.company_domain), _norm(fb.company)} - {""}
        if fb.decision == "reject":
            p.rejected_keys |= keys
            if fb.embedding:
                rej_emb.append(fb.embedding)
            r = _rule_for(fb)
            if r and _norm(r) not in seen_rule:
                seen_rule.add(_norm(r))
                rules.append(r)
            if len(p.rejected_examples) < _MAX_EXAMPLES:
                why = fb.reason_text or REJECT_CATEGORIES.get(fb.reason_category, fb.reason_category)
                p.rejected_examples.append(f"{fb.company} — {why}".strip(" —"))
        elif fb.decision == "approve":
            p.approved_keys |= keys
            if fb.embedding:
                appr_emb.append(fb.embedding)
            if len(p.approved_examples) < _MAX_EXAMPLES:
                p.approved_examples.append(f"{fb.company}{(' — ' + fb.reason_text) if fb.reason_text else ''}")
    p.approved_centroid = _centroid(appr_emb)
    p.rejected_centroid = _centroid(rej_emb)
    p.exclusion_rules = rules[:_MAX_RULES]
    return p


def load_preferences(store, subject_domain: str) -> Preferences:
    if store is None:
        return Preferences()
    try:
        fb = store.get_feedback(subject_domain)
    except Exception:
        fb = []
    return build_preferences(fb)


def prompt_block(pref: Preferences) -> str:
    """Text appended to an extraction prompt so the agent applies the user's learned taste
    (exclusion rules + few-shot approved/rejected examples). Empty when there's no feedback."""
    if not pref.has_feedback:
        return ""
    parts: List[str] = ["\n\n--- LEARNED FROM THIS CUSTOMER'S FEEDBACK (obey strictly) ---"]
    if pref.exclusion_rules:
        parts.append("REJECT any lead where: " + "; ".join(pref.exclusion_rules) + ".")
    if pref.approved_examples:
        parts.append("Good leads the user APPROVED: " + "; ".join(pref.approved_examples[:_MAX_EXAMPLES]) + ".")
    if pref.rejected_examples:
        parts.append("Bad leads the user REJECTED (do not surface these or their look-alikes): "
                     + "; ".join(pref.rejected_examples[:_MAX_EXAMPLES]) + ".")
    return "\n".join(parts)


def feedback_prompt_block(store, subject_domain: str) -> str:
    """Convenience for extraction agents: load prefs + render the prompt block in one call."""
    return prompt_block(load_preferences(store, subject_domain))
