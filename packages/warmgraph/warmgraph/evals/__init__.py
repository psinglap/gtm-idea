"""Offline eval harness for the GTM agents — deterministic scorers over a CustomerListReport that
measure the quality dimensions the runtime guards enforce (grounding, freshness, dedup, recall) plus
precision against human approve/reject feedback. Reused by scripts/eval.py + tests/test_scorers.py."""
from warmgraph.evals.scorers import (
    Metric,
    dedup_leaks,
    freshness_violations,
    grounding_rate,
    precision_at_k,
    recall,
    rejected_leak,
    score_report,
)

__all__ = ["Metric", "grounding_rate", "freshness_violations", "dedup_leaks", "recall",
           "rejected_leak", "precision_at_k", "score_report"]
