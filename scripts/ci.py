"""Competitive Intelligence CLI (standalone tool).

Usage:
    python scripts/ci.py [company_url] [--deep]

Same engine the REST API (POST /competitive-intelligence) and the MCP tool
(competitive_intelligence) call — all thin wrappers over WarmgraphService.
"""
from __future__ import annotations

import sys

from warmgraph.service import WarmgraphService


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    depth = "deep" if "--deep" in sys.argv else "quick"
    url = args[0] if args else "https://serro.ai"

    svc = WarmgraphService()
    print(f"Competitive intelligence: {url}  (depth={depth}, model={svc.registry.provider_name})\n")
    report = svc.competitive_intelligence(url, depth)
    p, comp = report.profile, report.competitive

    print(f"report id: {report.id}")
    print(f"\n== SUBJECT ==\n  {p.name}: {p.what_they_do}")
    print(f"  category: {p.category} / {p.subcategory} | stage: {p.stage}")

    print("\n== COMPETITIVE LANDSCAPE ==")
    print(f"  crowdedness: {comp.crowdedness_score}/5 — {comp.market_crowdedness}")
    print(f"  unique advantage: {comp.our_unique_advantage}")
    print(f"  moat: {comp.our_moat}")
    print(f"  whitespace: {comp.whitespace}")
    print(f"  competitors target vs us: {comp.competitor_targets_vs_ours}")
    print(f"  pricing: {comp.pricing_landscape}")
    print(f"  positioning: {comp.positioning}")

    print("\n  COMPETITORS (direct):")
    for c in comp.direct_competitors:
        mark = "verified" if c.verified else "unverified"
        print(f"   - {c.name} [{c.tier or '?'}] [{mark}] — {c.positioning}")
        if c.target_customers or c.size_note:
            print(f"       targets: {c.target_customers}  {('| ' + c.size_note) if c.size_note else ''}")


if __name__ == "__main__":
    main()
