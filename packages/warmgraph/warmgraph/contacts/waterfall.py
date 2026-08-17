"""Provider waterfall — try each ContactProvider in order (free_infer always on; paid providers
drop in by key later) and keep the people they find, deduped. First provider that yields anyone
wins (cheapest-capable first); later providers only run when earlier ones come up empty."""
from __future__ import annotations

from typing import List, Optional

from warmgraph.contacts.providers.base import ContactProvider, ProviderCtx, Target
from warmgraph.contacts.providers.free_infer import FreeInferProvider
from warmgraph.models import Contact


def build_providers(settings) -> List[ContactProvider]:
    """Enabled providers, cheapest-capable first. free_infer is always available (no key)."""
    candidates: List[ContactProvider] = [FreeInferProvider()]
    return [p for p in candidates if p.available(settings)]


def enrich_company(ctx: ProviderCtx, company: str, domain: str, targets: List[Target],
                   providers: Optional[List[ContactProvider]] = None) -> List[Contact]:
    """Find real people at `company` for the target archetypes. Runs the waterfall; stops at the
    first provider that returns anyone. Dedupes on linkedin url (else person name)."""
    providers = providers if providers is not None else build_providers(ctx.settings)
    found: dict = {}
    for provider in providers:
        try:
            people = provider.find(ctx, company, domain, targets)
        except Exception:
            people = []
        for c in people:
            key = (c.linkedin_url or c.person or "").strip().lower()
            if key and key not in found:
                found[key] = c
        if found:
            break   # cheapest capable provider satisfied the request
    return list(found.values())
