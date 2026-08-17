"""ContactProvider interface — every enrichment source (free or paid) implements this, so the
waterfall can chain them uniformly. Mirrors the ModelBackend abstraction in llm/registry.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

from warmgraph.models import Contact


@dataclass
class Target:
    """A person archetype to find at a company (from the ICP)."""
    title: str
    seniority: str          # 'buyer' (senior decision-maker) | 'champion' (hands-on)
    is_decision_maker: bool


@dataclass
class ProviderCtx:
    settings: object        # warmgraph.config.Settings
    registry: object        # warmgraph.llm.registry.ModelRegistry


class ContactProvider:
    name = "base"

    def available(self, settings) -> bool:
        """True if this provider can run (e.g. its API key is set). free_infer is always available."""
        return False

    def find(self, ctx: ProviderCtx, company: str, domain: str, targets: List[Target]) -> List[Contact]:
        """Return real people at `company` matching the target archetypes, with whatever contact
        details this provider can supply."""
        return []

    def verify_email(self, email: str) -> Tuple[str, float]:
        """('verified'|'guessed'|'unknown', confidence 0..1). Providers that can verify override this."""
        return ("unknown", 0.0)
