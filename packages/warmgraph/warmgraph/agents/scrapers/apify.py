"""Tiny Apify helper — run an actor synchronously and return its dataset items.
Apify = the affordable, pay-per-result way to scrape Twitter/LinkedIn/etc. (no firehose contract)."""
from __future__ import annotations

from typing import Any, List

import httpx


def run_actor(token: str, actor: str, run_input: dict, timeout: float = 120.0) -> List[Any]:
    # actor id uses '~' between user and name in the REST path
    actor_path = actor.replace("/", "~")
    url = f"https://api.apify.com/v2/acts/{actor_path}/run-sync-get-dataset-items"
    r = httpx.post(url, params={"token": token}, json=run_input, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else data.get("items", [])


def first(item: dict, *keys, default=""):
    for k in keys:
        v = item.get(k)
        if v:
            return v
    return default
