from __future__ import annotations

import json
import re


def _try(s: str):
    try:
        return json.loads(s)
    except Exception:
        return None


def _repair(s: str) -> str:
    """Balance braces/brackets + close a dangling string — recovers truncated LLM JSON."""
    s = s.rstrip()
    depth_curly = depth_sq = 0
    in_str = esc = False
    for ch in s:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth_curly += 1
            elif ch == "}":
                depth_curly -= 1
            elif ch == "[":
                depth_sq += 1
            elif ch == "]":
                depth_sq -= 1
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    s += "]" * max(0, depth_sq) + "}" * max(0, depth_curly)
    return s


def extract_json(text: str) -> dict:
    """Robust JSON extraction — open models add prose/```json fences or truncate. Never raises;
    returns {} if nothing parseable."""
    text = (text or "").strip()
    if "```" in text:
        parts = text.split("```")
        text = max(parts, key=len)
        if text.lstrip()[:4].lower() == "json":
            text = text.lstrip()[4:]
    start = text.find("{")
    if start == -1:
        return {}
    text = text[start:]
    end = text.rfind("}")
    if end != -1:
        r = _try(text[: end + 1])
        if r is not None:
            return r
    r = _try(_repair(text))
    return r if isinstance(r, dict) else {}
