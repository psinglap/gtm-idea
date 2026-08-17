# Chrome Web Store listing (draft)

**Name:** Luma ICP Scout

**Summary (132 chars max):**
Find your best-fit prospects at any Luma event — scan the guest list, match your ICP from LinkedIn, and get a ready target list.

**Description:**
Luma ICP Scout turns a Luma event's guest list into a ranked list of the people worth your time.

- Scans the full guest list of any Luma event you're registered for (names, photos, LinkedIn).
- Reads each attendee's LinkedIn profile (slowly, in your own session) and matches them against
  your ideal-customer profile — which it builds automatically from your website.
- Learns from your feedback: accept/reject anyone and it adapts to your judgment over time.
- Gives you a clean dashboard, a printable target sheet with talking points, and a private link
  to view your list on any device.

You stay in control: it only reads pages you can already see when signed in, everything runs
locally by default (a free on-device model, no account required), and nothing is shared unless
you choose to.

**Category:** Productivity
**Permissions justification:**
- `luma.com`, `linkedin.com` host access — to read the guest list and profiles the signed-in user
  can already view, on their behalf.
- `tabs`, `scripting` — to open and read those pages.
- `storage` — to save the user's ICP, results, and settings locally.
- `sidePanel` — the app's UI.

**Privacy:** Data (ICP, results) is stored locally in the user's browser. If the user enables the
optional dashboard sync, their results are sent only to the backend URL they configure, keyed to a
private token. No data is sold or shared with third parties. An optional Claude API key, if provided,
is used only to call Anthropic's API from the user's browser and is stored locally.
