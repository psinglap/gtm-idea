from __future__ import annotations

import time
from typing import Optional

import httpx

from warmgraph.llm.registry import ModelBackend


class OpenAICompatBackend(ModelBackend):
    """One backend for every OpenAI-compatible endpoint: Groq, OpenRouter, Together,
    Cerebras, Mistral, Google Gemini (OpenAI-compat), and local Ollama.

    All of these expose POST {base_url}/chat/completions, so a single code path covers
    the free tiers and open models — no per-provider SDKs.
    """

    def __init__(self, base_url: str, api_key: Optional[str], model: str, name: str = "openai_compat"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.name = name

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, want_json: bool = False,
        temperature: float = 0.2,
    ) -> str:
        if want_json:
            system = system + "\n\nRespond with ONLY valid JSON, no prose, no code fences."
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        url = f"{self.base_url}/chat/completions"
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            resp = httpx.post(url, headers=headers, json=payload, timeout=60.0)
            if resp.status_code in (429, 500, 502, 503, 529):
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:200]}", request=resp.request, response=resp
                )
                time.sleep(2 * (attempt + 1))  # 2s, 4s backoff
                continue
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or [{}]
            msg = choices[0].get("message") or {}
            content = msg.get("content") or msg.get("reasoning") or ""
            return content.strip()
        raise last_exc  # exhausted retries
