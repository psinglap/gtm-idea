"""Who counts as a target — as configuration, not as source code.

These rules decide who gets written to, so they are the first thing anyone running this has to
change, and they used to live as Python lists inside the judge. That made the one thing every
user must edit the one thing they had to open an editor and find a module for, and it shipped an
opinionated ICP that silently applied to whoever installed it.

So: a JSON file. `config/icp.json` if it exists, `$WG_ICP_FILE` if you point somewhere else, and
the shipped example if neither. `config/icp.example.json` is a copy-and-edit starting point.

A malformed or unreadable file is an ERROR, never a silent fall back to the example. A judge
quietly applying somebody else's ICP because a comma was missing is exactly the failure this
module exists to prevent: it would look like it was working, and every verdict would be wrong.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# The example. Deliberately a real, coherent ICP rather than placeholder text, because a judge
# needs something that works in order to demonstrate anything — but it is one company's ICP and
# almost certainly not yours. `source` tells you which set is in force.
BUILTIN_TARGET_ROLES = [
    "Founder / co-founder / CEO — any industry, consumer or B2B, any stage",
    "TECHNICAL founder — CTO or engineer who co-founded the company. A priority, not an "
    "exclusion: they can build the product but not the distribution",
    "Growth / marketing / brand / content / social / creator-partnerships, any seniority",
    "GTM roles — sales, business development, revenue, partnerships",
    "Investor / VC / venture partner / angel",
    "Founding GTM / growth / marketing hire at a startup, including 'Founding Member' where the "
    "role is commercial rather than engineering",
]

BUILTIN_NOT_TARGET_ROLES = [
    "Individual contributor at an established company with no founder, GTM, growth or "
    "early-startup signal — e.g. a staff engineer, data scientist or designer at a large firm",
    "Student, intern, recruiter, academic researcher, press",
    "'Founding Engineer', 'first engineer' or any founding technical IC who is not a co-founder — "
    "they build the product, they do not own distribution. This does NOT apply to a technical "
    "co-founder or CTO, who remain among the strongest targets",
    "Anyone at a university — student, PhD, postdoc, researcher, faculty, or an .edu address",
]

# Absolute exclusions, stated first and separately in the prompt. The difference from
# `not_target_roles` is that these are not weighed against anything: no amount of other signal
# makes them a target. Enforcing them only at the delivery gate still spends an Apollo credit and
# a judgement on someone who can never be written to.
BUILTIN_NEVER_TARGETS = [
    "ANYONE AT A UNIVERSITY. Student, PhD, postdoc, researcher, professor, staff, or an .edu or "
    ".ac address. Not a buyer. Free AI events are full of them and they look like everyone else "
    "on the list.",
    "FOUNDING ENGINEER, first engineer, or any founding technical IC who is not a co-founder. "
    "They build the product; they do not decide how it goes to market. A technical CO-FOUNDER or "
    "CTO is a different person and remains a strong target.",
    "Recruiters, press, and interns.",
]

DEFAULT_FILENAME = "icp.json"
ENV_VAR = "WG_ICP_FILE"


class IcpConfigError(RuntimeError):
    """A file was found and could not be used. Never swallowed — see the module docstring."""


@dataclass
class IcpRules:
    target_roles: List[str] = field(default_factory=lambda: list(BUILTIN_TARGET_ROLES))
    not_target_roles: List[str] = field(default_factory=lambda: list(BUILTIN_NOT_TARGET_ROLES))
    never_targets: List[str] = field(default_factory=lambda: list(BUILTIN_NEVER_TARGETS))
    # "built-in example" or the path it came from. Reported by /outreach/settings so you can tell
    # at a glance whether your file is actually being read — a config file in the wrong place is
    # indistinguishable from no config file at all until something says which one is live.
    source: str = "built-in example"

    @property
    def is_builtin(self) -> bool:
        return self.source == "built-in example"


def _strings(raw, key: str, where: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise IcpConfigError(f"{where}: '{key}' must be a list of strings")
    out = [x.strip() for x in raw if x and x.strip()]
    return out


def _candidate_path(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env = (os.getenv(ENV_VAR) or "").strip()
    if env:
        return Path(env)
    # Walk up from this file to find a repo-root `config/icp.json`, so it works the same whether
    # you run from the repo root, from apps/api, or from a cron working directory.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / DEFAULT_FILENAME
        if candidate.exists():
            return candidate
    return None


def load(path: Optional[str] = None) -> IcpRules:
    """The rules in force. Falls back to the example ONLY when no file was specified or found."""
    candidate = _candidate_path(path)
    if candidate is None:
        return IcpRules()

    # An explicitly requested file that is missing is an error. Falling back here would mean a
    # typo in WG_ICP_FILE silently reinstates somebody else's ICP.
    if not candidate.exists():
        raise IcpConfigError(f"{candidate}: ICP file not found (set {ENV_VAR} or remove it)")
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise IcpConfigError(f"{candidate}: {e}") from e
    if not isinstance(data, dict):
        raise IcpConfigError(f"{candidate}: expected a JSON object")

    where = str(candidate)
    targets = _strings(data.get("target_roles"), "target_roles", where)
    if not targets:
        # Without this the judge has no positive criterion at all and rejects everyone, which
        # reads as "the ICP is too strict" rather than "the file is empty".
        raise IcpConfigError(f"{where}: 'target_roles' cannot be empty — nobody would qualify")
    return IcpRules(
        target_roles=targets,
        not_target_roles=_strings(data.get("not_target_roles"), "not_target_roles", where),
        never_targets=_strings(data.get("never_targets"), "never_targets", where),
        source=where,
    )
