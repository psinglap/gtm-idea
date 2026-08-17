"""Deterministic scorers for a CustomerListReport — the offline eval harness.

Each scorer MEASURES a quality dimension the agents' runtime guards try to enforce, so a regression is
caught by a number dropping instead of a human eyeballing 55 companies. Ground truth for precision is
the human `lead_feedback` (approve/reject). Scorers are pure + deterministic (the only non-determinism
is the optional HTTP resolves-check in grounding, gated behind `resolve=`)."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from warmgraph.agents.activities.hiring_leads import _website_resolves
from warmgraph.models import Account, CustomerListReport, LeadFeedback


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def _acct_keys(a: Account) -> Set[str]:
    return {_norm(a.company_domain), _norm(a.company)} - {""}


def _fb_keys(feedback: List[LeadFeedback]) -> Tuple[Set[str], Set[str]]:
    """(rejected_keys, approved_keys) — an approve overrides a prior reject (matches Preferences)."""
    rej: Set[str] = set()
    appr: Set[str] = set()
    for fb in feedback:
        keys = {_norm(fb.company_domain), _norm(fb.company)} - {""}
        if fb.decision == "reject":
            rej |= keys
        elif fb.decision == "approve":
            appr |= keys
    return rej - appr, appr


@dataclass
class Metric:
    name: str
    value: float          # the measured number (rate, count, or nan when N/A)
    passed: bool
    detail: str = ""
    hard: bool = False    # a hard gate — its failure fails the whole eval run


# --------------------------------------------------------------------------- #
# Scorers                                                                       #
# --------------------------------------------------------------------------- #


def grounding_rate(report: CustomerListReport, *, min_rate: float = 0.9,
                   resolve: bool = True, cap: int = 120) -> Metric:
    """Fraction of signals grounded in a real source: has a source_url AND (optionally) it resolves.
    Catches hallucinated / dead links. HARD gate — leads with no real source are the worst failure."""
    sigs = [s for a in report.accounts for s in (a.signals or [])]
    if not sigs:
        return Metric("grounding", 1.0, True, "no signals", hard=True)
    with_url = [s for s in sigs if s.source_url]
    resolved_urls: Set[str] = set()
    checked = {s.source_url for s in with_url}
    if len(checked) > cap:
        checked = set(list(checked)[:cap])
    if resolve and checked:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for u, ok in zip(checked, ex.map(_website_resolves, checked)):
                if ok:
                    resolved_urls.add(u)
    else:
        resolved_urls = set(checked)
    # a signal is grounded if it has a url that either resolved or wasn't in the checked sample
    grounded = sum(1 for s in with_url if s.source_url in resolved_urls or s.source_url not in checked)
    rate = grounded / len(sigs)
    missing = len(sigs) - len(with_url)
    return Metric("grounding", round(rate, 3), rate >= min_rate,
                  f"{grounded}/{len(sigs)} signals grounded" + (f", {missing} missing a URL" if missing else ""),
                  hard=True)


def freshness_violations(report: CustomerListReport, *, max_violations: int = 0,
                         days: int = 100) -> Metric:
    """Count of DATED signals older than the window. Undated signals (e.g. team/social) are current."""
    from warmgraph.dates import is_stale
    bad = [(a.company, s.date) for a in report.accounts for s in (a.signals or [])
           if is_stale(s.date, days)]
    detail = "; ".join(f"{c} ({d})" for c, d in bad[:5]) or f"all signals within {days}d"
    return Metric("freshness", float(len(bad)), len(bad) <= max_violations, detail, hard=True)


def dedup_leaks(report: CustomerListReport, *, max_leaks: int = 0) -> Metric:
    """Distinct companies appearing more than once (same normalized name OR same domain).
    Catches e.g. Bloom Nutrition under bloomnutrition.com AND bloomnu.com. HARD gate."""
    by_name: dict = defaultdict(list)
    by_dom: dict = defaultdict(list)
    for a in report.accounts:
        if _norm(a.company):
            by_name[_norm(a.company)].append(a)
        if _norm(a.company_domain):
            by_dom[_norm(a.company_domain)].append(a)
    leaks = [f"name '{v[0].company}' ×{len(v)}" for v in by_name.values() if len(v) > 1]
    leaks += [f"domain '{k}' ×{len(v)}" for k, v in by_dom.items() if len(v) > 1]
    return Metric("dedup", float(len(leaks)), len(leaks) <= max_leaks,
                  "; ".join(leaks[:5]) or "no duplicates", hard=True)


def recall(report: CustomerListReport, *, min_accounts: int = 1) -> Metric:
    """How many companies surfaced — a collapsed list is a (soft) regression."""
    n = len(report.accounts)
    return Metric("recall", float(n), n >= min_accounts, f"{n} companies (min {min_accounts})", hard=False)


def rejected_leak(report: CustomerListReport, feedback: List[LeadFeedback], *,
                  max_leaks: int = 0) -> Metric:
    """A company the user REJECTED reappearing in the list. HARD gate — must be 0."""
    rej, _ = _fb_keys(feedback)
    if not rej:
        return Metric("rejected_leak", 0.0, True, "no rejections recorded yet", hard=True)
    leaked = [a.company for a in report.accounts if _acct_keys(a) & rej]
    return Metric("rejected_leak", float(len(leaked)), len(leaked) <= max_leaks,
                  ("leaked: " + ", ".join(leaked[:5])) if leaked else "no rejected companies present",
                  hard=True)


def precision_at_k(report: CustomerListReport, feedback: List[LeadFeedback], *,
                   k: int = 20, min_precision: Optional[float] = None) -> Metric:
    """Of the top-K companies that the user has LABELED, the share they approved. Sharpens as more
    feedback accrues; soft (informational) unless min_precision is set."""
    rej, appr = _fb_keys(feedback)
    top = report.accounts[:k]
    a_hits = sum(1 for a in top if _acct_keys(a) & appr)
    r_hits = sum(1 for a in top if _acct_keys(a) & rej)
    labeled = a_hits + r_hits
    name = f"precision@{k}"
    if not labeled:
        return Metric(name, float("nan"), True, f"no labeled companies in top-{k} yet — label more",
                      hard=False)
    prec = a_hits / labeled
    passed = True if min_precision is None else prec >= min_precision
    return Metric(name, round(prec, 3), passed, f"{a_hits}/{labeled} labeled top-{k} approved", hard=False)


def score_report(report: CustomerListReport, feedback: List[LeadFeedback], *,
                 thresholds: Optional[dict] = None, resolve: bool = True) -> List[Metric]:
    """Run the full scorecard for one subject. `thresholds` comes from evals/golden.json."""
    t = thresholds or {}
    return [
        grounding_rate(report, min_rate=t.get("min_grounding", 0.9), resolve=resolve),
        freshness_violations(report, max_violations=t.get("max_freshness_violations", 0)),
        dedup_leaks(report, max_leaks=t.get("max_dedup_leaks", 0)),
        recall(report, min_accounts=t.get("min_accounts", 1)),
        rejected_leak(report, feedback),
        precision_at_k(report, feedback, k=t.get("k", 20), min_precision=t.get("min_precision")),
    ]
