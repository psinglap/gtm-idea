"""ICP + Winning-Category agent. Derives ideal-customer personas/segments and — the key
output — the specific category/segment where this company can WIN (where-to-play), plus the
pitch angle per persona. Reused by deep CI."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from warmgraph.agents.base import Agent
from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import (
    CompanyProfile,
    CompetitiveAnalysis,
    IcpAnalysis,
    IcpPersona,
    IcpSegment,
)
from warmgraph.profile import derive_profile
from warmgraph.scraper import crawl_site

TASK = "icp_analysis"

_SYSTEM = (
    "You are a senior B2B GTM strategist. Given a company, define WHO buys and WHERE it can win. "
    "Rules:\n"
    "- personas: the specific buyer/champion roles (expand abbreviations, e.g. TPM -> Technical "
    "Program Manager). For each: seniority, real pains, the buying TRIGGERS/signals to watch for, "
    "and the precise pitch_angle that lands for that persona.\n"
    "- segments: the best-fit account types calibrated to THIS company's stage (its next 10-50 "
    "customers, not an aspirational enterprise TAM) — name + firmographics + why.\n"
    "- winning_category: the ONE category/segment/wedge where this company can realistically WIN "
    "now (where-to-play). Be specific and honest.\n"
    "- how_to_target: the most effective way to reach these people.\n"
    "Infer conservatively; ground in what the company actually does."
)

_SCHEMA = """Return ONLY JSON:
{
  "personas": [{"role":"","seniority":"","pains":["..."],"triggers":["..."],"pitch_angle":""}],
  "segments": [{"name":"","firmographics":"","why":""}],
  "winning_category": "", "how_to_target": "", "summary": ""
}"""


def derive_icp(registry: ModelRegistry, settings: Settings, profile: CompanyProfile,
               competitive: Optional[CompetitiveAnalysis] = None) -> IcpAnalysis:
    if not registry.has_llm:
        return IcpAnalysis(summary="(no LLM configured)")
    comp = ""
    if competitive and competitive.direct_competitors:
        names = ", ".join(c.name for c in competitive.direct_competitors[:8])
        comp = f"Competitors: {names}\nWhitespace they miss: {competitive.whitespace}\n"
    user = (
        f"Company: {profile.name}\nWhat they do: {profile.what_they_do}\n"
        f"Category: {profile.category} / {profile.subcategory}\nStage: {profile.stage}\n"
        f"Value prop: {profile.value_proposition}\nDifferentiation: {profile.differentiation}\n"
        f"{comp}\n{_SCHEMA}"
    )
    raw = registry.complete(TASK, _SYSTEM, user, max_tokens=2600, want_json=True)
    d = extract_json(raw) or {}
    personas = [
        IcpPersona(
            role=str(p.get("role", "")), seniority=str(p.get("seniority", "")),
            pains=[str(x) for x in p.get("pains", []) if x],
            triggers=[str(x) for x in p.get("triggers", []) if x],
            pitch_angle=str(p.get("pitch_angle", "")),
        ) for p in d.get("personas", []) if p.get("role")
    ]
    segments = [
        IcpSegment(name=str(s.get("name", "")), firmographics=str(s.get("firmographics", "")),
                   why=str(s.get("why", ""))) for s in d.get("segments", []) if s.get("name")
    ]
    return IcpAnalysis(
        personas=personas, segments=segments,
        winning_category=str(d.get("winning_category", "")),
        how_to_target=str(d.get("how_to_target", "")), summary=str(d.get("summary", "")),
    )


class IcpInput(BaseModel):
    url: str


class IcpAgent(Agent):
    name = "icp_winning_category"
    description = "ICP personas/segments + the winning category (where-to-play) + per-persona pitch angle, from a company URL."
    InputModel = IcpInput
    OutputModel = IcpAnalysis

    def run(self, inp: IcpInput) -> IcpAnalysis:
        # read the STORED profile (built once) — no re-crawl, no recompute
        return self.ctx.get_or_build_profile(inp.url).icp
