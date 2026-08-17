"""Read delivery failures back out of Gmail, and never mail that address again.

An email that bounces is worse than one that is never sent. The recipient gets nothing, the
sender's domain reputation takes the hit, and — without this — the next run happily tries again,
because as far as the pipeline knew the send succeeded. The first live send hit exactly that:
an address from a hand-built sheet, marked verified by hand, and Gmail
answered "Address not found".

The mailbox is the record of what actually happened. Gmail delivers the failure notice as an
ordinary message from `mailer-daemon`, so the same connection that sends can read the outcome —
no webhooks, no bounce service, no extra credential.

Two deliberate choices:

  • **Hard bounces only.** A full mailbox or a greylisting server is temporary, and burning the
    address on a transient failure loses a real contact for good. Only "no such address" style
    failures mark a contact dead.
  • **The address is taken from the notice, not guessed.** A bounce for one recipient must never
    retire a different one, so the address is matched against contacts we actually mailed.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# Gmail's own failure notices. `from:mailer-daemon` covers Google; the subject terms catch the
# other MTAs that relay through.
SEARCH = ('(from:mailer-daemon OR from:postmaster OR '
          'subject:"Delivery Status Notification" OR subject:"Undelivered Mail Returned")')

# The trailing character class deliberately excludes a final dot: a notice writes
# "failed permanently: victim@other.com." and the sentence's full stop was being captured as
# part of the domain, so the address never matched the ones we had actually mailed.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

# "Address not found", "550 5.1.1 ... does not exist", "user unknown". These mean the mailbox
# will never exist, so the address is retired.
_HARD = re.compile(
    r"address not found|couldn't be found|could not be found|does not exist|doesn'?t exist|"
    r"no such user|user unknown|unknown recipient|recipient not found|"
    r"invalid recipient|mailbox unavailable|550[\s-]*5\.1\.[01]|5\.1\.1", re.I)

# Temporary. Worth knowing about, never worth retiring an address for.
_SOFT = re.compile(
    r"mailbox full|over quota|quota exceeded|temporarily|try again later|greylist|"
    r"rate limited|4\.\d\.\d|deferred", re.I)


def classify(text: str) -> str:
    """'hard' | 'soft' | '' — what kind of failure this notice describes.

    Hard is checked first: a message can mention both, and "address not found" is decisive
    however the rest of the boilerplate reads.
    """
    body = text or ""
    if _HARD.search(body):
        return "hard"
    if _SOFT.search(body):
        return "soft"
    return ""


def failed_address(text: str, known: Optional[set] = None) -> str:
    """The address that failed, taken from the notice.

    A bounce notice quotes several addresses — the daemon, the sender, sometimes a postmaster.
    When we know which addresses we actually mailed, the answer must be one of THEM; retiring a
    contact because their name appeared in someone else's bounce would be silent and permanent.
    """
    found = _EMAIL.findall(text or "")
    if known:
        for addr in found:
            if addr.lower() in known:
                return addr.lower()
        return ""
    for addr in found:                       # fall back to the first non-daemon address
        low = addr.lower()
        if not any(x in low for x in ("mailer-daemon", "postmaster", "noreply", "no-reply")):
            return low
    return ""


def scan(messages: List[Dict], known: Optional[set] = None) -> Dict[str, str]:
    """{address: 'hard'|'soft'} from a list of {'text': ...} bounce notices.

    Pure, so the classification is testable without a mailbox. The caller supplies whatever
    Gmail returned and applies the verdicts.
    """
    out: Dict[str, str] = {}
    for m in messages or []:
        text = m.get("text") or ""
        kind = classify(text)
        if not kind:
            continue
        addr = failed_address(text, known)
        if not addr:
            continue
        # A hard verdict wins: an address that bounced hard once is dead, whatever a later
        # transient notice says.
        if out.get(addr) != "hard":
            out[addr] = kind
    return out
