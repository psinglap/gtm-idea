"""Warmgraph core — the headless Competitive Intelligence engine.

Layers (CI):
  config       settings/env (LLM providers + DB)
  models       CompanyProfile, Competitor, CompetitiveAnalysis, CompetitiveIntelligenceReport
  scraper      crawl a company's site (Firecrawl/httpx)
  search       Tavily web search (competitor grounding/verification)
  llm          model registry (provider-agnostic) + backends
  profile      URL -> CompanyProfile
  competitive  competitive landscape analysis
  storage      Store interface; SQLite (local/test) + Postgres/Neon (prod)
  service      WarmgraphService — used by apps/api, mcp, scripts
"""

__version__ = "0.1.0"
