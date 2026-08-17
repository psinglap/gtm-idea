"""Company profile derivation — the subject of a CI report.

URL's crawled text -> one LLM call -> structured CompanyProfile (heuristic fallback if no
LLM). Intentionally small: CI only needs to understand the company well enough to map its
competitors; the heavy ICP/discovery layers are separate (and not built yet).
"""
from __future__ import annotations

from urllib.parse import urlparse

from warmgraph.config import Settings
from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import CompanyProfile

TASK = "company_profile"

_SYSTEM = (
    "You are a B2B analyst. Read a company's website text and produce a precise, realistic "
    "profile of what the company does, its category, business model, pricing, value prop, "
    "differentiation, and inferred stage. Infer conservatively; if unknown, say so."
)

_SCHEMA = """Return ONLY JSON:
{
  "name": "", "one_liner": "", "what_they_do": "(2-3 sentences)",
  "category": "", "subcategory": "(e.g. MarTech -> Influencer Marketing Platform)",
  "product_capabilities": ["..."], "business_model": "(B2B SaaS / D2C / marketplace ...)",
  "pricing_model": "", "value_proposition": "", "differentiation": "",
  "stage": "(pre-seed/seed/Series A/growth - inferred)", "stage_evidence": "", "geography": ""
}"""


def _heuristic(url: str, text: str) -> CompanyProfile:
    domain = urlparse(url if url.startswith("http") else "https://" + url).netloc
    brand = domain.replace("www.", "").split(".")[0] if domain else "the company"
    return CompanyProfile(
        name=brand,
        what_they_do=text[:240] if not text.startswith("[") else "",
        stage="unknown (no LLM key set)",
    )


def derive_profile(
    registry: ModelRegistry, settings: Settings, url: str, text: str
) -> CompanyProfile:
    if registry.has_llm:
        try:
            user = f"Company URL: {url}\n\nWebsite content:\n{text}\n\n{_SCHEMA}"
            raw = registry.complete(TASK, _SYSTEM, user, max_tokens=1600, want_json=True)
            d = extract_json(raw)
            return CompanyProfile(
                name=str(d.get("name", "")), one_liner=str(d.get("one_liner", "")),
                what_they_do=str(d.get("what_they_do", "")), category=str(d.get("category", "")),
                subcategory=str(d.get("subcategory", "")),
                product_capabilities=[str(x) for x in d.get("product_capabilities", []) if x],
                business_model=str(d.get("business_model", "")),
                pricing_model=str(d.get("pricing_model", "")),
                value_proposition=str(d.get("value_proposition", "")),
                differentiation=str(d.get("differentiation", "")), stage=str(d.get("stage", "")),
                stage_evidence=str(d.get("stage_evidence", "")), geography=str(d.get("geography", "")),
            )
        except Exception:
            pass
    return _heuristic(url, text)
