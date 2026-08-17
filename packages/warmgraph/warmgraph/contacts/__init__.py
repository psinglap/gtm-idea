"""Contact enrichment — turn a qualified COMPANY into the right PERSON to reach out to.

A provider WATERFALL (mirrors llm/registry.py): each provider implements the same interface and is
enabled by having a key; the orchestrator tries them in order and keeps the best result. Phase 1 ships
the always-on FREE provider (LinkedIn discovery + email pattern-inference); paid providers
(Apollo/Hunter/LeadMagic/FindyMail) drop into the same interface later with just a key."""
from warmgraph.contacts.waterfall import build_providers, enrich_company

__all__ = ["build_providers", "enrich_company"]
