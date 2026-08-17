"""Offline unit tests for the eval scorers (no network, no LLM). Synthetic accounts + feedback."""
from __future__ import annotations

import math

import pytest

from warmgraph.evals import scorers as S
from warmgraph.models import Account, AccountSignal, CustomerListReport, LeadFeedback
from warmgraph.storage.sqlite_store import SqliteStore


def sig(url="https://example.com/job/1", date="", stype="hiring"):
    return AccountSignal(signal_type=stype, source="example.com", source_url=url, date=date, text="signal")


def acct(company, domain="", signals=None):
    return Account(company=company, company_domain=domain, signals=signals if signals is not None else [sig()])


def report(*accounts):
    return CustomerListReport(subject_domain="example.com", accounts=list(accounts))


@pytest.fixture
def store(tmp_path):
    return SqliteStore(str(tmp_path / "eval.db"))


# --- grounding -------------------------------------------------------------- #

def test_grounding_all_have_url_passes():
    r = report(acct("A", "a.com"), acct("B", "b.com"))
    m = S.grounding_rate(r, resolve=False)   # resolve=False = offline
    assert m.value == 1.0 and m.passed and m.hard


def test_grounding_missing_url_fails():
    r = report(acct("A", "a.com", [sig(url="")]), acct("B", "b.com", [sig()]))
    m = S.grounding_rate(r, resolve=False, min_rate=0.9)
    assert m.value == 0.5 and not m.passed


def test_grounding_uses_resolver_when_enabled(monkeypatch):
    monkeypatch.setattr(S, "_website_resolves", lambda u: "good" in u)
    r = report(acct("A", "a.com", [sig(url="https://good.com/1")]),
               acct("B", "b.com", [sig(url="https://dead.com/2")]))
    m = S.grounding_rate(r, resolve=True, min_rate=0.9)
    assert m.value == 0.5 and not m.passed


# --- dedup ------------------------------------------------------------------ #

def test_dedup_same_name_two_domains_leaks():
    # the real Bloom Nutrition bug: one company under two domains
    r = report(acct("Bloom Nutrition", "bloomnutrition.com"),
               acct("Bloom Nutrition", "bloomnu.com"),
               acct("Other", "other.com"))
    m = S.dedup_leaks(r)
    assert m.value == 1.0 and not m.passed and m.hard


def test_dedup_clean_passes():
    r = report(acct("A", "a.com"), acct("B", "b.com"))
    assert S.dedup_leaks(r).passed


# --- freshness -------------------------------------------------------------- #

def test_freshness_flags_old_dated_signal():
    r = report(acct("Old", "old.com", [sig(date="2020-01-01")]),
               acct("New", "new.com", [sig(date="")]))   # undated = current
    m = S.freshness_violations(r, max_violations=0, days=100)
    assert m.value == 1.0 and not m.passed


# --- rejected leak (hard gate) --------------------------------------------- #

def test_rejected_leak_fails_when_rejected_present():
    r = report(acct("RejectCo", "reject.com"), acct("Good", "good.com"))
    fb = [LeadFeedback(subject_domain="example.com", company="RejectCo",
                       company_domain="reject.com", decision="reject", reason_category="agency-vendor")]
    m = S.rejected_leak(r, fb)
    assert m.value == 1.0 and not m.passed and m.hard


def test_approve_overrides_reject():
    r = report(acct("Flip", "flip.com"))
    fb = [LeadFeedback(subject_domain="m", company="Flip", company_domain="flip.com", decision="reject"),
          LeadFeedback(subject_domain="m", company="Flip", company_domain="flip.com", decision="approve")]
    assert S.rejected_leak(r, fb).passed   # approve wins → not a leak


# --- precision -------------------------------------------------------------- #

def test_precision_at_k():
    r = report(acct("Ap1", "ap1.com"), acct("Ap2", "ap2.com"),
               acct("Rej", "rej.com"), acct("Unlabeled", "un.com"))
    fb = [LeadFeedback(subject_domain="m", company="Ap1", company_domain="ap1.com", decision="approve"),
          LeadFeedback(subject_domain="m", company="Ap2", company_domain="ap2.com", decision="approve"),
          LeadFeedback(subject_domain="m", company="Rej", company_domain="rej.com", decision="reject")]
    m = S.precision_at_k(r, fb, k=20)
    assert round(m.value, 2) == 0.67   # 2 approved of 3 labeled


def test_precision_no_labels_is_na():
    m = S.precision_at_k(report(acct("X", "x.com")), [], k=20)
    assert math.isnan(m.value) and m.passed


# --- end-to-end through the store (offline sqlite) ------------------------- #

def test_score_report_reads_feedback_from_store(store):
    store.save_feedback([LeadFeedback(subject_domain="example.com", company="RejectCo",
                                      company_domain="reject.com", decision="reject")])
    r = report(acct("RejectCo", "reject.com"), acct("Good", "good.com"))
    fb = store.get_feedback("example.com")
    metrics = S.score_report(r, fb, thresholds={"min_accounts": 1}, resolve=False)
    by = {m.name: m for m in metrics}
    assert not by["rejected_leak"].passed     # the stored reject is caught
    assert by["dedup"].passed and by["grounding"].passed
