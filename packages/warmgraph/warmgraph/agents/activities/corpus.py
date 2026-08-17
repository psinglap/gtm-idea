"""Shared-corpus RAG helpers for company-level signals (hiring / fundraising):
retrieve by semantic similarity from the global corpus; embed + tag at ingest. Brute-force cosine for
now (works on SQLite + Postgres); swap to pgvector HNSW at scale without changing callers."""
from __future__ import annotations

from typing import Callable, List

from warmgraph.llm.embeddings import cosine, get_embedder
from warmgraph.models import CompanyLead


def retrieve_company_leads(store, query_emb: List[float], signal_type: str,
                           is_stale_fn: Callable, threshold: float = 0.60,
                           k: int = 30) -> List[CompanyLead]:
    """Pull this customer's relevant leads from the SHARED corpus (≤3mo, cosine ≥ threshold)."""
    if store is None or not query_emb:
        return []
    recent = store.get_recent_company_leads(signal_type, limit=400)
    scored = [(cosine(query_emb, L.embedding), L) for L in recent
              if L.embedding and not is_stale_fn(L.signal_date, 100)]
    scored = [(c, L) for c, L in scored if c >= threshold]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [L for _, L in scored[:k]]


def embed_and_tag_leads(settings, leads: List[CompanyLead], industry: str, signal_type: str,
                        domain_of: Callable) -> List[CompanyLead]:
    """At ingest: tag industry/company_domain + embed each lead (role/company + rationale + industry)."""
    embedder = get_embedder(settings)
    for L in leads:
        L.industry = industry
        if L.website:
            try:
                L.company_domain = domain_of(L.website)
            except Exception:
                pass
        if embedder:
            txt = (f"{L.role} {L.rationale} {industry}" if signal_type == "hiring"
                   else f"{L.company} {L.rationale} {industry}")
            try:
                L.embedding = embedder.embed_one(txt[:1000])
            except Exception:
                pass
    return leads
