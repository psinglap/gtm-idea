"""Nothing in this repo may identify the person who runs it.

This is a public repo that automates a real person's outreach: it types answers into strangers'
registration forms and sends mail from their mailbox. Every one of those details started life
hardcoded, because that is the fastest way to get it working — a name, an email, a calendar link,
a company domain, and, in two code comments, the addresses of REAL PROSPECTS who had bounced.
Those two were the worst of it: third-party personal data, published, indexed and scraped, from
people who never agreed to any of it.

Removing them once is easy. Keeping them out is the hard part, because the natural way to debug
this system is to paste in the address that actually broke. So the denylist is a test.

Adding a term here is cheap. If you fork this, add your own.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The denylist is NOT in this file, and that is the point.
#
# The obvious way to write this is a tuple of the strings to ban. It is also self-defeating: the
# list would name, in a public repo, the exact identifiers it exists to keep out of one. A reader
# learns the author's name, employer and the two companies whose people leaked into code comments
# — from the file whose job was to prevent that.
#
# So the terms live in `tests/private-denylist.txt`, which is gitignored. Copy the .example file,
# put your own name, company, domains and handles in it, and this test enforces them locally and
# in your own CI without ever publishing them. Absent the file, the structural checks below still
# run — those are the ones that matter for a fork.
DENYLIST_FILE = Path(__file__).with_name("private-denylist.txt")


def _denied():
    if not DENYLIST_FILE.exists():
        return []
    return [ln.strip().lower() for ln in DENYLIST_FILE.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]


# Real addresses are the thing this is really guarding, and the rule is about the DOMAIN, not
# the name in front of it. A domain that cannot resolve to a real organisation cannot deliver to
# a real person, so fixtures are free to invent whatever local part reads best.
#
# The list is deliberately short and boring. Anything else has to be added on purpose, which is
# the moment someone has to ask "is this a person?" — the question that was never asked when
# a real student's university address went into a test docstring and stayed there.
SAFE_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
SAFE_EMAIL_DOMAINS = (
    "example.com", "example.org", "example.net",           # RFC 2606
    "acme.com", "acme.test", "else.com", "other.com", "x.com", "y.com",
    "mail.example.com", "example-domain.com",
    # Placeholder domains used in docs and form placeholders. Nobody reads mail here.
    "yourcompany.com", "yourdomain.com",
    "state-university.edu", "tech-institute.edu", "college.ac.uk",
    "university.ac.jp", "institute.edu.sg", "education.io", "edutech.ai", "ac.com",
    "googlemail.com", "google.com", "luma.com", "linkedin.com",
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    for name in out.split("\n"):
        p = ROOT / name
        if not name or not p.is_file() or "node_modules" in name:
            continue
        if p.suffix in (".png", ".jpg", ".ico", ".gif", ".woff", ".woff2", ".lock"):
            continue
        try:
            yield name, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_identifying_strings_anywhere():
    """Enforces YOUR terms from tests/private-denylist.txt. Skips if you have not made one."""
    terms = _denied()
    if not terms:
        import pytest
        pytest.skip("no tests/private-denylist.txt — copy the .example and add your own terms")
    hits = []
    for name, text in _tracked():
        if name in ("tests/test_no_private_data.py", "tests/private-denylist.example.txt"):
            continue
        low = text.lower()
        for term in terms:
            if term in low:
                line = next((i + 1 for i, l in enumerate(text.splitlines())
                             if term in l.lower()), 0)
                hits.append(f"{name}:{line} contains {term!r}")
    assert not hits, "private data in a public repo:\n  " + "\n  ".join(sorted(hits))


def test_no_real_email_addresses():
    """An address at a reserved documentation domain is inert. Anything else may be a person."""
    hits = []
    for name, text in _tracked():
        if name in ("tests/test_no_private_data.py", "tests/private-denylist.example.txt"):
            continue
        for m in EMAIL.finditer(text):
            domain = m.group(1).lower().rstrip(".")
            if domain in SAFE_EMAIL_DOMAINS or domain.endswith(SAFE_SUFFIXES):
                continue
            if domain.endswith((".png", ".svg", ".js", ".css", ".ts")):
                continue                   # a filename caught by the address shape, not an address
            hits.append(f"{name}: {m.group(0)}")
    assert not hits, ("addresses that are not at a reserved documentation domain — if one of "
                      "these is a real person, remove it:\n  " + "\n  ".join(sorted(set(hits))))


def test_the_shipped_template_cannot_be_sent_as_is():
    """The repo ships an email template. If a fresh install could send it, the first run would
    mail YOUR NAME to a real guest list, and no one gets those back."""
    import sys
    sys.path.insert(0, str(ROOT / "packages" / "warmgraph"))
    from warmgraph.outreach import template

    gaps = template.missing_fields("Dana Lee", "AI Dinner", template.MessageTemplate())
    assert any("YOUR NAME" in g for g in gaps), \
        "the shipped template must be refused until it is edited"


def test_the_answer_bank_ships_empty():
    """These answers get typed into a stranger's registration form. A default here is somebody
    else's identity submitted on the user's behalf, before they have entered anything."""
    import sys
    sys.path.insert(0, str(ROOT / "packages" / "warmgraph"))
    from warmgraph.outreach import registration

    assert registration.DEFAULT_ANSWERS == {}, \
        f"registration answers must ship empty, found: {sorted(registration.DEFAULT_ANSWERS)}"
