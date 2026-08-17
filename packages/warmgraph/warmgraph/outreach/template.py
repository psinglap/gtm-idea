"""The follow-up email — one template you write once, filled in per recipient.

No LLM anywhere. You provide the subject and body (calendar link and all, inline, wherever you
want it); the system substitutes two fields and sends it. That is the whole point: nothing is
generated, so nothing needs proofreading, so it can go out at volume without a human in the
loop. It also means the copy can never drift between one recipient and the next.

Two variables, nothing else:
    {first_name}   the recipient's given name   (also {name} for their full name)
    {event_name}   the event they attended

Everything else in the email is your words, sent exactly as written.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

# Separators Luma titles hang their subtitle off. The fullwidth colon is not a typo: real event
# names on the account use it ("Beta x Alibaba Cloud x AMD：AI agent builder challenge").
_CUTS = ("：", ":", "|", "@", "(", " - ", " — ", " – ")
# "#1", "# 1", "#01" — Luma's edition marker. Everything after it is the episode's tagline, not
# the event's name: "Wild AI SF # 1 Hidden Infrastructure of Intelligence" is Wild AI SF. Handled
# by regex rather than _CUTS because the separator has a variable shape.
_EDITION = re.compile(r"\s+#\s*\d+\b")
_TRAILING = re.compile(r"\s*(presented|hosted|powered|sponsored|brought to you)\s+by\b.*$", re.I)
_SUBJECT_MAX = 40
_FIELD = re.compile(r"\{(\w+)\}")
# [anchor text](https://url) — one field in the UI, two renderings out: a real hyperlink in the
# HTML part, and "anchor text (url)" in the plain-text part so nothing is lost for clients that
# refuse HTML.
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# Past this, a "wanted to connect" note reads as odd rather than warm, so the send agent skips
# instead of stretching the phrasing.
#
# It is also the single dial on reach, because ingest.SCAN_LOOKBACK_DAYS follows it: guest lists
# are collected exactly as far back as we are willing to write. Measured on the live database,
# unscanned events this client was approved for held 1,046 guests at 14 days, 6,359 at 30 and
# 14,471 at 60 — so the cost of a short window is large and completely invisible, since an empty
# queue looks the same as no work to do.
MAX_EVENT_AGE_DAYS = int(os.getenv("WG_MAX_EVENT_AGE_DAYS", "") or 14)

# `event_name` is the SHORT name, for the subject line. `event_place` is the fuller phrase for
# the body — "the Wild AI SF event at Frontier Tower" — because the two want different things:
# a subject must stay short, while the body sentence is what proves you were really in the room.
FIELDS = ("first_name", "name", "event_name", "event_place")

CALENDAR = "https://example.com/your-booking-link"
LINKEDIN = "https://www.linkedin.com/in/your-handle/"

DEFAULT_SUBJECT = "Wanted to connect at {event_name}"

DEFAULT_BODY = f"""Hi {{first_name}},

I'm YOUR NAME, building YOUR COMPANY, WHAT YOU DO IN ONE LINE.

I was at {{event_place}} and was hoping to chat in person, but didn't manage to catch you.

Would you be open to a quick 15 minutes? Here's my [Calendar Link]({CALENDAR})

Best
YOUR NAME
YOUR TITLE
[yoursite.com](https://example.com) | [LinkedIn]({LINKEDIN}) | [Book a Slot]({CALENDAR})"""

# The shipped template is a SHAPE, not a message. These are the words that prove it has not been
# edited yet, and missing_fields() refuses to send while any of them survive — so the failure mode
# is "nothing went out", never "a hundred people received YOUR NAME".
PLACEHOLDERS = ("YOUR NAME", "YOUR COMPANY", "YOUR TITLE", "WHAT YOU DO IN ONE LINE",
                "example.com/your-booking-link", "linkedin.com/in/your-handle")


@dataclass
class MessageTemplate:
    """The email itself. Edit it in the UI; it is stored per workspace."""
    subject: str = DEFAULT_SUBJECT
    body: str = DEFAULT_BODY

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "MessageTemplate":
        d = d or {}
        return cls(subject=(d.get("subject") or DEFAULT_SUBJECT),
                   body=(d.get("body") or DEFAULT_BODY))

    def to_dict(self) -> dict:
        return {"subject": self.subject, "body": self.body}


# Venue and calendar names carry decoration that reads fine on a page and badly in a sentence:
# "Frontier Tower 🧑‍🚀" produced "I was at the Wild AI SF event at Frontier Tower 🧑‍🚀". Emoji in
# cold outreach reads as automated, which is the one thing this template avoids. Lives here, with
# the other text-shaping helpers, so every caller gets it — it was previously applied when
# deriving the event name but NOT to the venue, so it leaked straight back into the body.
_DECOR = re.compile(
    "[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\uFE0F\u200d\u2B00-\u2BFF]+")


def strip_decoration(name: str) -> str:
    """A venue or event name, fit for a sentence."""
    out = _DECOR.sub(" ", name or "")
    out = re.sub(r"\s*[|·–—-]\s*$", "", out)
    return re.sub(r"\s+", " ", out).strip(" -|·")


def short_event_name(name: str, limit: int = _SUBJECT_MAX) -> str:
    """Luma title -> something short enough to sit in a subject line.

    Rules, in order: cut at the first separator, drop a trailing "presented by …", collapse
    whitespace, then trim to `limit` on a word boundary. The result is stored on the event and
    is editable in the UI, because rules occasionally produce something graceless.
    """
    s = (name or "").strip()
    if not s:
        return ""
    m = _EDITION.search(s)
    if m and m.start() > 3:
        s = s[:m.start()]
    for cut in _CUTS:
        idx = s.find(cut)
        # Only cut if something meaningful survives — "SF: The Event" must not become "SF".
        if idx > 3:
            s = s[:idx]
            break
    s = _TRAILING.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -–—|:,")
    if len(s) <= limit:
        return s
    clipped = s[:limit].rsplit(" ", 1)[0].strip(" -–—|:,")
    return clipped or s[:limit].strip()


def first_name(full_name: str) -> str:
    """Just the given name, and never a mangled one: if the first token doesn't look like a
    name (an emoji, a handle, a company), fall back to the whole string so we never open with
    "Hi 🚀,".

    Case is normalised because guest lists are typed by the guests themselves: a real Luma list
    gave "fay" where every neighbouring row was capitalised, and "Hi fay," in a cold email reads
    as a mail merge nobody checked.

    Two things are deliberately left alone. Mixed case is always someone's own choice — McCarthy,
    O'Brien, deSouza — and flattening it is a worse error than the one being fixed. Short all-caps
    tokens are initials, so JJ and TJ stay themselves rather than becoming Jj.
    """
    token = (full_name or "").strip().split(" ")[0].strip(" ,.")
    if token and re.fullmatch(r"[A-Za-z][A-Za-z'’\-]{1,}", token):
        if token.islower() or (token.isupper() and len(token) > 2):
            return token.capitalize()
        return token
    return (full_name or "").strip()


def _fill(text: str, values: dict) -> str:
    """Substitute only the fields we know about, and leave anything else alone.

    `str.format` would explode on a stray brace in the user's own copy (a URL, an emoji, a
    literal "{"), and their template must never be able to crash a send.
    """
    return _FIELD.sub(lambda m: str(values.get(m.group(1), m.group(0))), text or "")


def to_plain(text: str) -> str:
    """[Book a Slot](url) -> "Book a Slot (url)". Keeps the address visible for anyone reading
    the plain-text part, rather than dropping the link entirely."""
    return _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", text or "")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def to_html(text: str) -> str:
    """[anchor](url) -> a real hyperlink, newlines -> <br>.

    Deliberately bare: no wrapper table, no images, no tracking pixel. A message with links but
    no beacons still reads as a person writing to a person, which is the point.
    """
    out, last = [], 0
    for m in _LINK.finditer(text or ""):
        out.append(_esc(text[last:m.start()]))
        out.append(f'<a href="{_esc(m.group(2))}">{_esc(m.group(1))}</a>')
        last = m.end()
    out.append(_esc((text or "")[last:]))
    return "".join(out).replace("\n", "<br>")


# Words that already tell the reader this was an event. "SF Founder Dinner event" and
# "AI Dev Night event" read as machine-written, and redundancy in a subject line is exactly the
# tell this template is trying to avoid. Of 11 real event names from one feed, 7 already carried
# one of these; only names like "Wild AI SF" or "ACCELR8" need the word added.
_EVENT_KIND = re.compile(
    r"\b(dinner|night|summit|meetup|breakfast|lunch|mixer|happy hour|hackathon|showcase|"
    r"demo|conference|party|workshop|panel|talk|campfire|social|expo|forum|day|fest|"
    r"session|roundtable|brunch|drinks|retreat|hack)\b", re.I)


def event_label(short: str) -> str:
    """The event name for a subject line, with "event" appended only when it is not already
    obvious that this is one. "Wild AI SF" -> "Wild AI SF event"; "AI Dev Night" unchanged."""
    name = strip_decoration(short)
    if not name:
        return "the event"
    return name if _EVENT_KIND.search(name) else f"{name} event"


def event_place(short: str, venue: str = "") -> str:
    """The body phrase: "the Wild AI SF event at Frontier Tower".

    The venue is what actually jogs someone's memory of the room, but it is only worth naming
    when it is a place rather than a postal address, and only when it is not already in the
    event's name — otherwise you get "the Frontier Tower event at Frontier Tower".
    """
    name = strip_decoration(short)
    if not name:
        return "the event"
    phrase = f"the {name} event"
    place = strip_decoration(venue)
    if not place or place.lower() in name.lower() or re.match(r"^\d", place):
        return phrase
    return f"{phrase} at {place}"


def render(*, name: str, event_name: str, tmpl: MessageTemplate,
           event_short: str = "", venue: str = "") -> tuple:
    """(subject, plain_body, html_body). `event_short` is the shortened event name when one has
    been set for the event — subject lines read badly with a 90-character Luma title."""
    short = event_short or short_event_name(event_name) or event_name
    values = {
        "first_name": first_name(name),
        "name": (name or "").strip(),
        "event_name": event_label(short),
        "event_place": event_place(short, venue),
    }
    subject = to_plain(_fill(tmpl.subject, values)).strip()
    filled = _fill(tmpl.body, values)
    return subject, to_plain(filled), to_html(filled)


def unknown_fields(tmpl: MessageTemplate) -> list:
    """Placeholders in the template that we cannot fill — surfaced in the UI while editing, so
    a typo like {firstname} is caught before it ships as literal text in a hundred emails."""
    found = set(_FIELD.findall(tmpl.subject)) | set(_FIELD.findall(tmpl.body))
    return sorted(found - set(FIELDS))


def missing_fields(name: str, event_name: str, tmpl: MessageTemplate) -> list:
    """What would render as a blank or a leftover placeholder. The send agent refuses rather
    than mailing "Hi ," or a message with a visible {curly_field}."""
    gaps = []
    if not first_name(name):
        gaps.append("recipient name")
    if not (event_name or "").strip():
        gaps.append("event name")
    if not (tmpl.subject or "").strip():
        gaps.append("template subject")
    if not (tmpl.body or "").strip():
        gaps.append("template body")
    for field in unknown_fields(tmpl):
        gaps.append(f"unknown field {{{field}}}")
    # The template still has the shipped placeholders in it. Caught here, with the other reasons
    # a message cannot be built, so it blocks delivery the same way a missing name does — the
    # first run of a fresh install must not be able to mail YOUR NAME to a real guest list.
    blob = f"{tmpl.subject}\n{tmpl.body}"
    for placeholder in PLACEHOLDERS:
        if placeholder in blob:
            gaps.append(f"template still says {placeholder!r} — edit it before sending")
    return gaps
