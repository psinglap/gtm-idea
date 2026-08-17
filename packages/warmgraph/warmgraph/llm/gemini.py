from __future__ import annotations

import time
from typing import Optional

import httpx

from warmgraph.config import Settings
from warmgraph.llm.registry import ModelBackend

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiBackend(ModelBackend):
    """Native Gemini (generateContent) — grants the free tier that the OpenAI-compat
    shim does not. Uses system_instruction + responseMimeType=json for clean output."""

    name = "gemini"

    def __init__(self, settings: Settings, model: str):
        self.settings = settings
        self.model = model
        self.api_key = settings.provider_key("gemini")

    def complete(
        self, system: str, user: str, *, max_tokens: int = 1024, want_json: bool = False,
        temperature: float = 0.2,
    ) -> str:
        gen_cfg = {"maxOutputTokens": max_tokens, "temperature": temperature}
        if want_json:
            gen_cfg["responseMimeType"] = "application/json"
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen_cfg,
        }
        url = f"{GEMINI_BASE}/{self.model}:generateContent"
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            resp = httpx.post(url, headers=headers, json=body, timeout=60.0)
            if resp.status_code in (429, 500, 503):
                last_exc = httpx.HTTPStatusError(
                    f"{resp.status_code} {resp.text[:200]}", request=resp.request, response=resp
                )
                time.sleep(2 * (attempt + 1))
                continue
            resp.raise_for_status()
            cands = resp.json().get("candidates", [])
            if not cands:
                return ""
            parts = cands[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts).strip()
        raise last_exc
