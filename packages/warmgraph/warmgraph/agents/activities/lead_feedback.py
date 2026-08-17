"""`lead_feedback` — records a human approve/reject judgement on leads (the eval loop's write path).

Each item is embedded (for the preference reranker) and stored keyed by subject_domain. The next
customer_list build reads these back via the learning layer (suppress / rules / rerank / few-shot).
Returns a compact summary + the current learned state so the caller can show what changed."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from warmgraph.agents.activities.feedback import build_preferences
from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.agents.base import Agent
from warmgraph.llm.embeddings import get_embedder
from warmgraph.models import LeadFeedback


class FeedbackItem(BaseModel):
    company: str
    company_domain: str = ""
    signal_type: str = ""            # signal judged, or 'account' for the whole company
    decision: str                    # 'approve' | 'reject'
    reason_category: str = ""        # one of models.REJECT_CATEGORIES (for rejects)
    reason_text: str = ""            # the user's detailed analysis
    lead_text: str = ""              # snapshot of what was judged (rationale/role)


class LeadFeedbackInput(BaseModel):
    url: str
    items: List[FeedbackItem] = Field(default_factory=list)


class LeadFeedbackReport(BaseModel):
    subject_domain: str = ""
    saved: int = 0
    approved: int = 0
    rejected: int = 0
    exclusion_rules: List[str] = Field(default_factory=list)   # what the agents will now avoid
    suppressed: List[str] = Field(default_factory=list)        # companies that won't resurface


class LeadFeedbackAgent(Agent):
    name = "lead_feedback"
    description = ("Record human approve/reject judgements on leads (with reasons). Embeds + stores "
                   "them so the next customer_list build learns this customer's ICP taste "
                   "(suppress rejects, exclusion rules, preference rerank, few-shot).")
    InputModel = LeadFeedbackInput
    OutputModel = LeadFeedbackReport

    def run(self, inp: LeadFeedbackInput) -> LeadFeedbackReport:
        store = self.ctx.store
        domain = domain_of(inp.url)
        embedder = get_embedder(self.ctx.settings)

        records: List[LeadFeedback] = []
        for it in inp.items:
            decision = (it.decision or "").strip().lower()
            if decision not in ("approve", "reject"):
                continue
            fb = LeadFeedback(
                subject_domain=domain, company=it.company.strip(),
                company_domain=(it.company_domain or "").strip().lower(),
                signal_type=it.signal_type, decision=decision,
                reason_category=it.reason_category, reason_text=it.reason_text,
                lead_text=it.lead_text or it.company,
            )
            if embedder:
                txt = f"{fb.company} {it.signal_type} {fb.lead_text}".strip()
                try:
                    fb.embedding = embedder.embed_one(txt[:1000])
                except Exception:
                    pass
            records.append(fb)

        if store is not None and records:
            store.save_feedback(records)

        # reflect the CUMULATIVE learned state (all feedback for this domain, not just this batch)
        all_fb = store.get_feedback(domain) if store is not None else records
        pref = build_preferences(all_fb)
        return LeadFeedbackReport(
            subject_domain=domain, saved=len(records),
            approved=sum(1 for r in records if r.decision == "approve"),
            rejected=sum(1 for r in records if r.decision == "reject"),
            exclusion_rules=pref.exclusion_rules,
            suppressed=sorted({k for k in pref.rejected_keys if k})[:50],
        )
