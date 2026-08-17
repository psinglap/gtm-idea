from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Optional

from warmgraph.config import Settings


class ModelBackend(ABC):
    """Stable interface every task calls through. Today this is Claude; tomorrow a
    given task can be backed by a small fine-tuned model — callers don't change."""

    name: str = "base"

    @abstractmethod
    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, want_json: bool = False,
        temperature: float = 0.2,
    ) -> str: ...


# OpenAI-compatible providers: (base_url, default_model). All have free tiers / open
# models, or run locally (ollama). Override the model via WG_LLM_MODEL.
PRESETS = {
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-oss-120b:free"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "cerebras": ("https://api.cerebras.ai/v1", "gpt-oss-120b"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "ollama": ("http://localhost:11434/v1", "llama3.1"),  # local, no key, no cost
}


# Auto-selection order when WG_LLM_PROVIDER is unset (best-for-this-task first).
PREFERENCE = ["anthropic", "gemini", "groq", "cerebras", "openrouter", "mistral", "together", "ollama"]


def build_backend(settings: Settings, provider: str) -> Optional[ModelBackend]:
    """Construct a backend for ONE named provider (the unit the bench runs per-provider)."""
    provider = (provider or "").lower()
    if provider == "anthropic":
        if not settings.has_claude:
            return None
        from warmgraph.llm.claude import ClaudeBackend

        return ClaudeBackend(settings)

    if provider == "gemini":
        key = settings.provider_key("gemini")
        if not key:
            return None
        from warmgraph.llm.gemini import GeminiBackend

        model = settings.llm_model if (settings.llm_provider == "gemini" and settings.llm_model) \
            else PRESETS["gemini"][1]
        return GeminiBackend(settings, model)

    if provider in PRESETS:
        from warmgraph.llm.openai_compat import OpenAICompatBackend

        base, model = PRESETS[provider]
        if settings.llm_provider == provider and settings.llm_base_url:
            base = settings.llm_base_url
        if settings.llm_provider == provider and settings.llm_model:
            model = settings.llm_model
        key = settings.provider_key(provider)
        if provider == "ollama":
            key = key or "ollama"
        elif not key:
            return None
        return OpenAICompatBackend(base, key, model, name=provider)

    # Fully custom OpenAI-compatible endpoint.
    if settings.llm_base_url:
        from warmgraph.llm.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(
            settings.llm_base_url, settings.llm_api_key, settings.llm_model or "default",
            name="custom",
        )
    return None


def _make_default(settings: Settings) -> Optional[ModelBackend]:
    provider = (settings.llm_provider or "auto").lower()
    if provider != "auto":
        return build_backend(settings, provider)
    enabled = set(settings.enabled_providers())
    for p in PREFERENCE:
        if p in enabled:
            backend = build_backend(settings, p)
            if backend:
                return backend
    return None  # no LLM configured -> callers use heuristics


def _build_chain(settings: Settings, provider: Optional[str]) -> list:
    """Ordered list of backends. The first is primary; the rest are failover targets used when
    the primary rate-limits (429) or errors — so a burst of calls spreads across free providers."""
    if provider:
        b = build_backend(settings, provider)
        return [b] if b else []
    chain: list = []
    forced = (settings.llm_provider or "auto").lower()
    enabled = settings.enabled_providers()
    order = ([forced] if forced != "auto" else []) + [p for p in PREFERENCE if p in enabled]
    seen = set()
    for p in order:
        if p in seen:
            continue
        seen.add(p)
        b = build_backend(settings, p)
        if b:
            chain.append(b)
    return chain


class ModelRegistry:
    """Maps task-name -> backend, with automatic provider FAILOVER. The primary backend is the
    configured one (e.g. Cerebras); on rate-limit/error the next configured free provider
    (Gemini/Groq/...) is tried. `register()` routes a single use-case to its own small model later.
    """

    # The few high-value, once-per-customer inference calls — routed to a strong model (Gemini 2.5
    # Flash) for better reasoning + reliable JSON. High-volume tagging/classify stays on the fast chain.
    # social_leads_extract needs world knowledge to map a social handle -> the author's employer
    # (e.g. a known founder's company), so it rides the strong model too.
    _STRONG_TASKS = ("company_profile", "competitive_analysis", "icp_analysis", "frameworks",
                     "signal_contexts", "competitor_dossier", "social_leads_extract")

    def __init__(self, settings: Settings, provider: Optional[str] = None):
        self.settings = settings
        self._chain: list = _build_chain(settings, provider)
        self._default: Optional[ModelBackend] = self._chain[0] if self._chain else None
        self._overrides: Dict[str, ModelBackend] = {}
        # default per-task routing: send the inference calls to Gemini 2.5 Flash when available
        strong = build_backend(settings, "gemini")
        if strong:
            for task in self._STRONG_TASKS:
                self._overrides[task] = strong

    @property
    def has_llm(self) -> bool:
        return bool(self._chain) or bool(self._overrides)

    @property
    def provider_name(self) -> str:
        return self._default.name if self._default else "heuristic"

    def register(self, task: str, backend: ModelBackend) -> None:
        self._overrides[task] = backend

    def backend_for(self, task: str) -> Optional[ModelBackend]:
        return self._overrides.get(task, self._default)

    def complete(
        self, task: str, system: str, user: str, *, max_tokens: int = 1024,
        want_json: bool = False, temperature: float = 0.2,
    ) -> Optional[str]:
        if task in self._overrides:
            ov = self._overrides[task]
            backends = [ov] + [b for b in self._chain if b is not ov]  # override first, then failover
        else:
            backends = list(self._chain)
        for backend in backends:
            try:
                out = backend.complete(system, user, max_tokens=max_tokens,
                                       want_json=want_json, temperature=temperature)
            except Exception:
                continue  # rate-limited / errored -> try the next provider
            if not (out and out.strip()):
                continue
            # JSON tasks: if the output doesn't parse (reasoning model returned prose/truncated
            # JSON), fail over to the next provider instead of accepting garbage.
            if want_json:
                from warmgraph.jsonutil import extract_json
                if not extract_json(out):
                    continue
            if backend in self._chain and self._chain[0] is not backend:
                self._chain.remove(backend)
                self._chain.insert(0, backend)  # promote the working provider
            return out
        return None
