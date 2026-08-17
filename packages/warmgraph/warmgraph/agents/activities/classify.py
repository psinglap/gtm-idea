"""Relevance + signal classifier for social listening. Batched into SMALL LLM calls (~12 posts
each) so each call returns reliably (a single 30-post call truncates on reasoning models). A free
keyword pre-filter + promo-drop heuristic cut noise before the LLM. ~2-4 LLM calls per run total."""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from warmgraph.jsonutil import extract_json
from warmgraph.llm.registry import ModelRegistry
from warmgraph.models import Post

_STOP = {"the", "and", "for", "with", "that", "this", "tool", "tools", "software", "platform",
         "management", "based", "your", "our", "are", "you", "they", "from", "into", "best"}

_PROMO = ("show hn", "i built", "i made", "i've built", "i created", "we built", "we launched",
          "launching", "[launch]", "introducing ")


def keywords(ctx: Dict) -> set:
    blob = " ".join([ctx.get("category", ""), ctx.get("pains", "")] + ctx.get("competitors", []))
    kw = {w for w in re.findall(r"[a-z][a-z0-9+.-]{2,}", blob.lower()) if w not in _STOP}
    kw |= {c.lower() for c in ctx.get("competitors", []) if c}
    return kw


def matches(p: Post, kw: set) -> bool:
    hay = f"{p.title} {p.text}".lower()
    return any(k in hay for k in kw)


def looks_promo(p: Post) -> bool:
    """Cheap filter for vendor self-promo / profile pages (noise, not prospects)."""
    t = (p.title or "").lower()
    if any(t.startswith(x) or t[:25].find(x) != -1 for x in _PROMO):
        return True
    if "/ posts / x" in t or t.endswith("- twitter") or t.endswith("/ x"):
        return True
    return False


def _majority(items: List[str]) -> str:
    items = [i for i in items if i]
    return max(set(items), key=items.count) if items else ""


_SYSTEM = (
    "You are a B2B social-listening analyst. From a list of posts, keep ONLY the ones genuinely "
    "RELEVANT to the company's category/problem/ICP — people discussing the problem, seeking or "
    "comparing solutions, or complaining about a competitor. IGNORE vendor self-promo, product "
    "launches, listicles, and unrelated posts. For each kept post give sentiment ABOUT THE PROBLEM, "
    "the pain-point, signal_type, and competitors mentioned, plus a short rollup. Relevance > volume."
)
_SCHEMA = """Return ONLY JSON. 'i' = post index. signal_type ∈ {seeking_solution,complaining,
comparing,asking_advice,hiring,sharing}. sentiment ∈ {positive,negative,neutral}.
{"relevant":[{"i":0,"sentiment":"negative","pain":"","signal_type":"seeking_solution","competitors":["Name"]}],
 "pain_points":["..."],"themes":["..."],"competitor_sentiment":{"Name":"..."},"problem_sentiment":""}"""


def _classify_batch(registry: ModelRegistry, ctx: Dict, batch: List[Post]) -> dict:
    lines = [f"{i}. [{p.platform}] {(p.title or '')[:70]} :: {(p.text or '')[:150]}"
             for i, p in enumerate(batch)]
    competitors = ", ".join(ctx.get("competitors", [])) or "(none known)"
    user = (
        f"Our company: {ctx.get('name','')} — {ctx.get('what','')}\n"
        f"Category: {ctx.get('category','')}\nCompetitors: {competitors}\n"
        f"ICP pains: {ctx.get('pains','')}\n\n{len(batch)} posts:\n" + "\n".join(lines)
        + f"\n\n{_SCHEMA}"
    )
    return extract_json(registry.complete("social_classify", _SYSTEM, user,
                                          max_tokens=1800, want_json=True)) or {}


def classify_posts(registry: ModelRegistry, ctx: Dict, posts: List[Post],
                   chunk: int = 6, cap: int = 80) -> Tuple[List[Post], dict]:
    """Batched classification. Returns (kept_posts_with_fields, overall_dict)."""
    posts = [p for p in posts[:cap] if (p.title or p.text)]
    if not posts or not registry.has_llm:
        keep = [p for p in posts if not looks_promo(p)]
        for p in keep:
            p.relevance = 0.4
        return keep, {}

    kept: List[Post] = []
    pains, themes, sents, comp = [], [], [], {}
    for start in range(0, len(posts), chunk):
        batch = posts[start:start + chunk]
        d = _classify_batch(registry, ctx, batch)
        if not d:  # this batch's call degraded — keep non-promo posts at low confidence
            for p in batch:
                if not looks_promo(p):
                    p.relevance = 0.4
                    kept.append(p)
            continue
        for item in d.get("relevant", []):
            try:
                p = batch[int(item.get("i"))]
            except (TypeError, ValueError, IndexError):
                continue
            p.relevance = 1.0
            p.sentiment = str(item.get("sentiment", ""))
            p.problem_theme = str(item.get("pain", ""))
            p.signal_type = str(item.get("signal_type", ""))
            p.competitors_mentioned = [str(c) for c in item.get("competitors", []) if c]
            kept.append(p)
        pains += [str(x) for x in d.get("pain_points", []) if x]
        themes += [str(x) for x in d.get("themes", []) if x]
        for k, v in (d.get("competitor_sentiment") or {}).items():
            comp.setdefault(k, v)
        if d.get("problem_sentiment"):
            sents.append(str(d["problem_sentiment"]))

    overall = {
        "pain_points": list(dict.fromkeys(pains))[:6],
        "themes": list(dict.fromkeys(themes))[:6],
        "competitor_sentiment": comp,
        "problem_sentiment": _majority(sents),
    }
    return kept, overall
