from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional


def _read_env_file(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass


def _load_dotenv() -> None:
    """Find and load the repo .env regardless of launch dir (so the MCP server, launched
    by Claude from any cwd, still gets the keys). Real env vars take precedence."""
    candidates = []
    d = os.getcwd()
    for _ in range(5):  # cwd and parents
        candidates.append(os.path.join(d, ".env"))
        d = os.path.dirname(d)
    # repo root relative to this file: packages/warmgraph/warmgraph/config.py -> ../../../.env
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.normpath(os.path.join(here, "..", "..", "..", ".env")))
    for path in candidates:
        if os.path.exists(path):
            _read_env_file(path)
            return


_load_dotenv()

# Per-provider env var names — lets ALL providers be configured at once (for the bake-off).
PROVIDER_ENV = {
    "groq": ("GROQ_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "cerebras": ("CEREBRAS_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
}


def _env(name: str, default: str = "") -> Callable[[], str]:
    """Read env at INSTANTIATION, not at class-definition time.

    A plain `x: str = os.getenv(...)` default is evaluated once, when this module is first
    imported — so anything that sets an env var afterwards (a test, a CLI flag, a worker
    bootstrapping its own config) is silently ignored and `get_settings()` keeps handing back
    the values captured at import. `default_factory` makes each Settings() read the current
    environment, which is what every caller already assumes.
    """
    return lambda: os.getenv(name, default)


def _env_or_none(*names: str) -> Callable[[], Optional[str]]:
    def read() -> Optional[str]:
        for name in names:
            value = os.getenv(name)
            if value:
                return value
        return None
    return read


@dataclass
class Settings:
    """Runtime settings, sourced from env. Kept tiny and dependency-free on purpose."""

    store_backend: str = field(default_factory=_env("WG_STORE", "sqlite"))
    db_path: str = field(default_factory=_env("WG_DB_PATH", "warmgraph.db"))
    database_url: Optional[str] = field(default_factory=_env_or_none("DATABASE_URL"))

    anthropic_api_key: Optional[str] = field(default_factory=_env_or_none("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=_env("WG_CLAUDE_MODEL", "claude-sonnet-4-6"))

    # Provider-agnostic LLM (free tiers, open models, or local Ollama). See llm/registry.py
    # for presets. Set WG_LLM_PROVIDER=groq|gemini|openrouter|together|cerebras|mistral|ollama.
    llm_provider: str = field(default_factory=_env("WG_LLM_PROVIDER", "auto"))
    llm_base_url: Optional[str] = field(default_factory=_env_or_none("WG_LLM_BASE_URL"))
    llm_api_key: Optional[str] = field(default_factory=_env_or_none("WG_LLM_API_KEY"))
    llm_model: str = field(default_factory=_env("WG_LLM_MODEL", ""))

    github_token: Optional[str] = field(default_factory=_env_or_none("GITHUB_TOKEN"))
    tavily_api_key: Optional[str] = field(default_factory=_env_or_none("TAVILY_API_KEY"))
    apollo_api_key: Optional[str] = field(default_factory=_env_or_none("APOLLO_API_KEY"))
    firecrawl_api_key: Optional[str] = field(default_factory=_env_or_none("FIRECRAWL_API_KEY"))
    user_agent: str = field(
        default_factory=_env("WG_USER_AGENT", "warmgraph-bot/0.1 (+https://warmgraph.dev)"))

    # Scraper-agent sources (signals layer)
    producthunt_token: Optional[str] = field(default_factory=_env_or_none("PRODUCTHUNT_TOKEN"))
    reddit_client_id: Optional[str] = field(default_factory=_env_or_none("REDDIT_CLIENT_ID"))
    reddit_client_secret: Optional[str] = field(default_factory=_env_or_none("REDDIT_CLIENT_SECRET"))
    # Apify = the affordable pay-per-result unlock for Twitter + LinkedIn (+ event sources)
    apify_token: Optional[str] = field(default_factory=_env_or_none("APIFY_TOKEN"))
    apify_twitter_actor: str = field(
        default_factory=_env("APIFY_TWITTER_ACTOR", "apidojo/tweet-scraper"))
    apify_linkedin_actor: str = field(
        default_factory=_env("APIFY_LINKEDIN_ACTOR", "harvestapi/linkedin-post-search"))

    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_tavily(self) -> bool:
        return bool(self.tavily_api_key)

    @property
    def has_firecrawl(self) -> bool:
        return bool(self.firecrawl_api_key)

    @property
    def has_apify(self) -> bool:
        return bool(self.apify_token)

    @property
    def api_keys(self) -> list:
        """Valid API keys for the HTTP API (WG_API_KEYS, comma-separated). Read live so
        prod can enforce while local dev/tests (unset) stay open."""
        return [k.strip() for k in os.getenv("WG_API_KEYS", "").split(",") if k.strip()]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_keys)

    def provider_key(self, provider: str) -> Optional[str]:
        """API key for a specific provider, from its dedicated env var (or the generic
        WG_LLM_API_KEY if WG_LLM_PROVIDER selects it)."""
        for var in PROVIDER_ENV.get(provider, ()):
            v = os.getenv(var)
            if v:
                return v
        if self.llm_provider == provider and self.llm_api_key:
            return self.llm_api_key
        return None

    def enabled_providers(self) -> list:
        """All providers we currently have credentials for — drives the bake-off."""
        provs = [p for p in PROVIDER_ENV if self.provider_key(p)]
        if os.getenv("WG_ENABLE_OLLAMA"):
            provs.append("ollama")  # local, opt-in (must be running)
        if self.has_claude:
            provs.append("anthropic")
        return provs


def get_settings() -> Settings:
    return Settings()
