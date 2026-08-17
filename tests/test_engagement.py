"""Phase D — engagement touchpoints. Verifies the touchpoints store (dedup on
(company_id, person_id, url)) and the pure matcher build_touchpoints: a contact is matched across
their own posts, the events they attend, and their company's hiring/funding — each producing a
grounded suggested action. Offline (no network, no LLM)."""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone

import pytest

from warmgraph.agents.activities.engagement import build_touchpoints
from warmgraph.entities import (
    Person,
    RawEvent,
    RawFundingNews,
    RawJobPosting,
    RawSocialPost,
    Touchpoint,
)
from warmgraph.storage.sqlite_store import SqliteStore


def test_touchpoints_store_dedups_on_company_person_url():
    store = SqliteStore(tempfile.mktemp(suffix=".db"))
    t1 = Touchpoint(company_id="c1", customer_id="p1", person_id="per1",
                    url="https://li/post/1", type="post-comment", suggested_action="reply")
    t2 = Touchpoint(company_id="c1", customer_id="p1", person_id="per1",
                    url="https://li/post/1", type="post-comment", suggested_action="reply v2")
    store.save_touchpoints([t1])
    store.save_touchpoints([t2])   # same (company, person, url) -> replace, not duplicate
    got = store.get_touchpoints("c1", person_id="per1")
    assert len(got) == 1 and got[0].suggested_action == "reply v2"


def test_build_touchpoints_matches_post_event_and_company_signal():
    person = Person(id="per1", person="Ann Lee", company_domain="acme.com",
                    touchpoint_refs=["https://luma.com/creatorsummit"])
    posts = [
        RawSocialPost(platform="linkedin", author="Ann Lee", author_handle="annlee",
                      text="We keep struggling to reconcile influencer data across tools.",
                      url="https://li/post/1",
                      posted_at=datetime(2026, 7, 1, tzinfo=timezone.utc)),
        RawSocialPost(platform="linkedin", author="Someone Else", text="unrelated",
                      url="https://li/post/2"),
    ]
    events = [RawEvent(source="luma", url="https://luma.com/creatorsummit", title="Creator Summit")]
    jobs = [RawJobPosting(source="greenhouse", url="https://x/j1",
                          title="Influencer Marketing Manager", company_hint="Acme")]
    funding = [RawFundingNews(source="techcrunch", url="https://tc/acme",
                              title="Acme raises $12M Series A", published_at="2026-06-20")]

    tps = build_touchpoints("c1", "p1", person, posts, events, jobs, funding)
    kinds = {t.type for t in tps}
    assert kinds == {"post-comment", "event", "company-signal"}
    # the post touchpoint is grounded in the person's own words
    post_tp = next(t for t in tps if t.type == "post-comment")
    assert "reconcile influencer data" in post_tp.evidence
    assert post_tp.url == "https://li/post/1"
    # exactly one hiring + one funding company-signal (each capped)
    assert sum(t.type == "company-signal" for t in tps) == 2
    # everything attributed to the person
    assert all(t.person_id == "per1" and t.company_id == "c1" for t in tps)


def test_build_touchpoints_empty_when_nothing_matches():
    person = Person(id="per2", person="Zed Nobody", company_domain="ghost.com")
    tps = build_touchpoints("c1", "p1", person,
                            [RawSocialPost(platform="x", author="Other", url="u")],
                            [RawEvent(url="e")], [RawJobPosting(url="j", company_hint="Other")],
                            [RawFundingNews(url="f", title="Other raises")])
    assert tps == []
