from __future__ import annotations

from warmgraph.config import Settings
from warmgraph.llm.registry import ModelBackend


class ClaudeBackend(ModelBackend):
    """Anthropic-backed model. Used to bootstrap + label; per-task small models can
    replace it later via ModelRegistry.register()."""

    name = "claude"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = None  # lazy

    def _get_client(self):
        if self._client is None:
            import anthropic  # imported lazily so the package works without it installed

            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)
        return self._client

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, want_json: bool = False,
        temperature: float = 0.2,
    ) -> str:
        if want_json:
            system = system + "\n\nRespond with ONLY valid JSON, no prose, no code fences."
        resp = self._get_client().messages.create(
            model=self.settings.anthropic_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()
