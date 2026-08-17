"""Offline unit tests (no network) for the Competitive Intelligence engine."""
from __future__ import annotations

import os
import tempfile

import pytest

from warmgraph.config import Settings
from warmgraph.llm.registry import build_backend
from warmgraph.models import (
    CompanyProfile,
    CompetitiveAnalysis,
    Competitor,
    CompetitiveIntelligenceReport,
)
from warmgraph.storage.sqlite_store import SqliteStore


@pytest.fixture()
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = SqliteStore(path)
    yield s
    os.remove(path)


def test_ci_report_roundtrip(store):
    report = CompetitiveIntelligenceReport(
        url="https://serro.ai", depth="quick",
        profile=CompanyProfile(name="Serro", category="Eng Management"),
        competitive=CompetitiveAnalysis(
            direct_competitors=[Competitor(name="LinearB", tier="funded scale-up")],
            crowdedness_score=4, positioning="AI program intelligence",
        ),
        source="cerebras",
    )
    store.save_ci_report(report)
    got = store.get_ci_report(report.id)
    assert got is not None
    assert got.url == "https://serro.ai"
    assert got.competitive.direct_competitors[0].name == "LinearB"
    assert got.competitive.crowdedness_score == 4
    assert store.get_ci_report("nope") is None


def test_provider_wiring(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    s = Settings()
    assert "groq" in s.enabled_providers()
    backend = build_backend(s, "groq")
    assert backend is not None and backend.name == "groq"

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert build_backend(Settings(), "groq") is None


def test_api_key_auth():
    from warmgraph.auth import key_is_valid

    # auth disabled when no keys configured
    assert key_is_valid(None, []) is True
    assert key_is_valid("anything", []) is True
    # enforced when keys configured
    assert key_is_valid("secret", ["secret"]) is True
    assert key_is_valid("secret", ["k1", "secret", "k2"]) is True
    assert key_is_valid("wrong", ["secret"]) is False
    assert key_is_valid(None, ["secret"]) is False
    assert key_is_valid("", ["secret"]) is False


def test_competitive_build_parses(store):
    from warmgraph.competitive import _competitor

    c = _competitor({"name": "GRIN", "positioning": "creator mgmt", "tier": "funded scale-up",
                     "strengths": ["discovery"], "weaknesses": ["pricey"]})
    assert c.name == "GRIN"
    assert c.tier == "funded scale-up"
    assert c.strengths == ["discovery"]


def test_posts_dedup(store):
    from warmgraph.models import Post

    a = Post(subject_domain="x.com", platform="hackernews", external_id="1", title="a")
    b = Post(subject_domain="x.com", platform="hackernews", external_id="2", title="b")
    dup = Post(subject_domain="x.com", platform="hackernews", external_id="1", title="dup")
    assert store.save_posts([a, b]) == 2
    assert store.save_posts([dup]) == 0          # (platform, external_id) already present
    assert len(store.get_posts("x.com")) == 2
    assert len(store.get_posts("x.com", platform="hackernews")) == 2


def test_freshness_filter():
    from warmgraph.dates import is_stale, parse_date

    assert parse_date("March 11, 2018") is not None
    assert is_stale("March 11 2018") is True       # years old -> stale
    assert is_stale("Feb 2023") is True
    assert is_stale("2019") is True
    assert is_stale("") is False                    # undated -> not provably stale (kept)
    assert is_stale("garbage") is False
    # a date within the last ~month is never stale
    from warmgraph.dates import today_str
    assert is_stale(today_str()) is False


def test_agent_registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        from warmgraph.service import WarmgraphService

        svc = WarmgraphService(Settings(store_backend="sqlite", db_path=path))
        names = [a["name"] for a in svc.list_agents()]
        for expected in ["competitive_intelligence", "icp_winning_category", "social_listening",
                         "events", "customer_list", "scrape_hackernews", "scrape_reddit",
                         "scrape_twitter", "scrape_linkedin"]:
            assert expected in names
        with pytest.raises(KeyError):
            svc.run_agent("does_not_exist", {})
    finally:
        os.remove(path)
