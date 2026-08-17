"""Offline golden eval harness — score customer_list runs so you don't hand-vet every time.

For each subject in evals/golden.json it builds the customer list, runs the deterministic scorers
(grounding / freshness / dedup / recall / precision-vs-your-feedback / rejected-leak), prints a
scorecard, and EXITS NON-ZERO if any HARD gate fails (so it can gate a commit / CI).

Usage:
    python scripts/eval.py                 # score from the warm corpus (cheap, repeatable)
    python scripts/eval.py --fresh         # force a live re-scrape of every signal first
    python scripts/eval.py --json          # machine-readable output
    python scripts/eval.py --url X.com     # score a single ad-hoc URL instead of the golden set
"""
from __future__ import annotations

import json
import math
import os
import sys

from warmgraph.agents.activities.social_listening import domain_of
from warmgraph.evals.scorers import score_report
from warmgraph.models import CustomerListReport
from warmgraph.service import WarmgraphService

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GOLDEN = os.path.join(_ROOT, "evals", "golden.json")
_SIGNAL_AGENTS = ("fundraising_leads", "hiring_leads", "team_signal", "social_leads")


def _load_subjects(url: str | None) -> list:
    if url:
        return [{"url": url if "://" in url else "https://" + url}]
    with open(_GOLDEN, "r", encoding="utf-8") as fh:
        return json.load(fh).get("subjects", [])


def _fmt(v: float) -> str:
    return "—" if isinstance(v, float) and math.isnan(v) else (f"{v:.3f}" if isinstance(v, float) else str(v))


def run_subject(svc: WarmgraphService, subj: dict, fresh: bool) -> tuple:
    url = subj["url"]
    domain = domain_of(url)
    if fresh:  # re-scrape each signal into the shared corpus before scoring
        for name in _SIGNAL_AGENTS:
            try:
                svc.run_agent(name, {"url": url, "limit": 40, "force": True})
            except Exception:
                pass
    out = svc.run_agent("customer_list", {"url": url, "limit": subj.get("limit", 60)})
    report = CustomerListReport.model_validate(out)
    feedback = svc.store.get_feedback(domain) if svc.store is not None else []
    metrics = score_report(report, feedback, thresholds=subj, resolve=True)
    return report, metrics


def main() -> None:
    fresh = "--fresh" in sys.argv
    as_json = "--json" in sys.argv
    url = None
    if "--url" in sys.argv:
        i = sys.argv.index("--url")
        url = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    svc = WarmgraphService()
    subjects = _load_subjects(url)
    all_ok = True
    report_out: list = []

    for subj in subjects:
        report, metrics = run_subject(svc, subj, fresh)
        subj_ok = all(m.passed for m in metrics if m.hard)
        all_ok = all_ok and subj_ok
        report_out.append({
            "url": subj["url"], "accounts": len(report.accounts), "passed": subj_ok,
            "metrics": [{"name": m.name, "value": m.value, "passed": m.passed, "hard": m.hard,
                         "detail": m.detail} for m in metrics],
        })
        if not as_json:
            print(f"\n=== {subj['url']}  ({len(report.accounts)} companies) "
                  f"[{'PASS' if subj_ok else 'FAIL'}] ===")
            for m in metrics:
                mark = "✓" if m.passed else "✗"
                gate = "HARD" if m.hard else "soft"
                print(f"  {mark} {m.name:<14} {_fmt(m.value):>7}  [{gate}]  {m.detail}")

    if as_json:
        print(json.dumps({"passed": all_ok, "subjects": report_out}, indent=2))
    else:
        print(f"\n{'✓ ALL GATES PASSED' if all_ok else '✗ EVAL FAILED — a hard gate regressed'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
