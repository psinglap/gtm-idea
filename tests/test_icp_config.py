"""Who counts as a target is configuration. These tests are about the ways that can go wrong.

The rule that matters most: a file that exists and cannot be used is an ERROR. Falling back to
the shipped example there would mean a missing comma silently reinstates somebody else's ICP —
every verdict wrong, nothing complaining, and the only symptom is a target list that looks
subtly off weeks later.
"""
from __future__ import annotations

import json

import pytest

from warmgraph.outreach import icp_rules


def _write(tmp_path, doc):
    p = tmp_path / "icp.json"
    p.write_text(json.dumps(doc) if isinstance(doc, (dict, list)) else doc)
    return str(p)


def test_the_shipped_example_is_used_when_there_is_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv(icp_rules.ENV_VAR, "")
    monkeypatch.chdir(tmp_path)
    rules = icp_rules.load()
    assert rules.is_builtin
    assert rules.source == "built-in example"
    assert any("founder" in r.lower() for r in rules.target_roles)


def test_a_file_replaces_the_example_entirely(tmp_path):
    """Not merged. Mixing a user's targets with the example's exclusions produces an ICP that
    neither of them wrote."""
    path = _write(tmp_path, {
        "target_roles": ["Hospital procurement lead", "Clinical director"],
        "not_target_roles": ["Medical students"],
        "never_targets": ["Anyone at a competitor"],
    })
    rules = icp_rules.load(path)
    assert rules.target_roles == ["Hospital procurement lead", "Clinical director"]
    assert rules.not_target_roles == ["Medical students"]
    assert rules.never_targets == ["Anyone at a competitor"]
    assert not rules.is_builtin and rules.source == path
    # nothing from the example survives
    joined = " ".join(rules.target_roles + rules.not_target_roles).lower()
    assert "creator" not in joined and "founding engineer" not in joined


def test_malformed_json_is_an_error_not_a_fallback(tmp_path):
    path = _write(tmp_path, "{ this is not json ")
    with pytest.raises(icp_rules.IcpConfigError) as e:
        icp_rules.load(path)
    assert "icp.json" in str(e.value)


def test_an_empty_target_list_is_an_error(tmp_path):
    """With no positive criterion the judge rejects everyone, which reads as "my ICP is too
    strict" rather than "my file is empty"."""
    path = _write(tmp_path, {"target_roles": [], "not_target_roles": ["x"]})
    with pytest.raises(icp_rules.IcpConfigError) as e:
        icp_rules.load(path)
    assert "nobody would qualify" in str(e.value)


def test_a_missing_file_that_was_asked_for_is_an_error(tmp_path):
    """A typo in WG_ICP_FILE must not silently reinstate the example ICP."""
    with pytest.raises(icp_rules.IcpConfigError) as e:
        icp_rules.load(str(tmp_path / "nope.json"))
    assert "not found" in str(e.value)


def test_wrong_types_are_rejected(tmp_path):
    path = _write(tmp_path, {"target_roles": "Founders"})      # a string, not a list
    with pytest.raises(icp_rules.IcpConfigError):
        icp_rules.load(path)


def test_the_env_var_points_the_loader_elsewhere(monkeypatch, tmp_path):
    path = _write(tmp_path, {"target_roles": ["Only pastry chefs"]})
    monkeypatch.setenv(icp_rules.ENV_VAR, path)
    assert icp_rules.load().target_roles == ["Only pastry chefs"]


def test_the_configured_rules_actually_reach_the_judge(monkeypatch, tmp_path):
    """The point of all of the above. A config file the judge does not read is decoration."""
    from warmgraph.agents.activities.event_icp_judge import icp_statement, system_prompt

    path = _write(tmp_path, {
        "target_roles": ["Hospital procurement lead"],
        "not_target_roles": ["Medical students"],
        "never_targets": ["Anyone employed by a competitor"],
    })
    monkeypatch.setenv(icp_rules.ENV_VAR, path)

    statement = icp_statement(None)
    assert "Hospital procurement lead" in statement
    assert "Medical students" in statement
    assert "creator-partnerships" not in statement, "the example ICP must be gone"

    prompt = system_prompt()
    head = prompt[:prompt.index("STRICT RULES")]
    assert "Anyone employed by a competitor" in head
    assert "UNIVERSITY" not in head, "the example's absolute exclusions must be gone too"


def test_the_example_file_in_the_repo_is_valid(monkeypatch):
    """Shipping a broken example would fail every user at step one."""
    rules = icp_rules.load("config/icp.example.json")
    assert rules.target_roles and rules.not_target_roles and rules.never_targets
