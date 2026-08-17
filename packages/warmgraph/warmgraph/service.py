from __future__ import annotations

from typing import List, Optional

from warmgraph.agents import build_agent_registry
from warmgraph.agents.activities.competitive_intelligence import run_ci
from warmgraph.agents.scrapers import SCRAPER_CLASSES
from warmgraph.config import Settings, get_settings
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import CompetitiveIntelligenceReport
from warmgraph.storage import get_store


class WarmgraphService:
    """High-level engine for the agent platform. The FastAPI app, the MCP server, and the CLI
    are thin wrappers over this. Holds the store, model registry, scraper agents, and the
    AgentRegistry (every action + scraper is callable via `run_agent`)."""

    SECRET_KEY_SETTING = "encryption_key"

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.store = get_store(self.settings)
        self._bootstrap_encryption()
        self.registry = ModelRegistry(self.settings)
        # scrapers instantiated with ctx=self, then registered (must exist before build).
        self.scrapers = [cls(self) for cls in SCRAPER_CLASSES]
        self.agents = build_agent_registry(self)

    def _bootstrap_encryption(self) -> None:
        """Make credential encryption work with zero setup.

        `WG_SECRET_KEY` still wins when set (stronger: the key then lives somewhere the database
        does not). Otherwise one is generated on first use and persisted, so 'Connect Gmail' is
        just Sign in with Google rather than a key-generation chore.
        """
        from warmgraph.connections import crypto

        store = self.store

        def provide() -> str:
            if store is None:
                return ""
            try:
                existing = store.get_setting(self.SECRET_KEY_SETTING)
                if existing:
                    return existing
                created = crypto.generate_key()
                store.set_setting(self.SECRET_KEY_SETTING, created)
                return created
            except Exception:
                return ""      # never let key bootstrap take down the whole service

        crypto.bootstrap(provide)

    # --- Competitive intelligence (kept as a direct method for the existing CI endpoints) ---
    def competitive_intelligence(
        self, url: str, depth: str = "quick"
    ) -> CompetitiveIntelligenceReport:
        return run_ci(self.registry, self.settings, self.store, url, depth)

    def get_ci_report(self, report_id: str) -> Optional[CompetitiveIntelligenceReport]:
        return self.store.get_ci_report(report_id)

    # --- Customer profile: built once per URL, reused by every agent ---
    def get_or_build_profile(self, url: str, refresh: bool = False, relationship_id=None):
        from warmgraph.agents.activities.company_icp import build_profile
        return build_profile(self.registry, self.settings, self.store, url, refresh, relationship_id)

    # --- Generic agent dispatch (powers /agents/{name} + MCP auto-tools) ---
    def run_agent(self, name: str, payload: Optional[dict] = None) -> dict:
        return self.agents.run(name, payload)

    def list_agents(self) -> List[dict]:
        return self.agents.describe()
