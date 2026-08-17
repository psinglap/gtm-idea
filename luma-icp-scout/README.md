# Luma ICP Scout

A Chrome extension that scrapes a Luma event's guest list, pulls each attendee's
LinkedIn URL **safely from Luma's own profile pages** (no LinkedIn scraping needed),
scores everyone against your editable ICP, and gives you a ranked "target these
people" list you can export.

Ships with an example ICP — founders (past earliest stage), growth roles, marketing
leaders, creator/influencer-marketing folks. Replace it with your own in the side panel;
the scoring is generic and the keywords are yours to change.

## How it works (proven on a live 141-person event)

0. **Set your ICP** — describe your ideal customer in plain English, or paste your website
   URL and hit **Suggest** to auto-draft it. This is what the AI judge matches people against.
1. **Scan** — reads the ~100+ guests rendered on the event page (name + Luma profile).
2. **Luma enrich** *(safe, same-origin, ~100% LinkedIn coverage)* — fetches each Luma
   profile with polite spacing to get their LinkedIn URL. **No LinkedIn account risk.**
3. **Read LinkedIn & judge** *(the core step — throttled)* — opens each attendee's LinkedIn
   profile through YOUR logged-in session, reads headline/About/experience, and judges each
   enriched profile against your ICP. Human-paced (default ~15s apart, adjustable, capped).
   The judge picks the best free engine automatically:
   - **Free local model** — Chrome's built-in on-device AI (Gemini Nano). No key, private.
   - **Claude (Haiku)** — only if you paste your own API key in Settings. Best quality, ~pennies/event.
   - **Keyword heuristic** — always-on fallback over the full profile text.
4. **Export / Copy / Save** — CSV, clipboard, or save the event to review later.

⚠️ Reading 100+ LinkedIn profiles in one session is where account-risk is highest. Do large
events in **batches across sessions**, and raise the delay for safety.

## Install (unpacked, ~30 seconds)

1. Open `chrome://extensions`
2. Toggle **Developer mode** (top-right) ON
3. Click **Load unpacked**
4. Select this folder: `gtm-idea/luma-icp-scout`
5. Pin the extension, open a Luma event, click the icon → the side panel opens.
6. Hit **Scan this event**.

## Settings

- **Judging engine** — Auto (recommended), or force Local / Claude / Keyword.
- **Claude API key** — optional; stored locally in your browser only. Leave blank for free modes.
- **LinkedIn max / run** — cap for the opt-in deep-read.

## Honest limits

- Chrome extensions are **desktop-only**; mobile "view your saved lists" is a planned
  lightweight web page (phase 2).
- Luma bios are often empty, so without the optional LinkedIn deep-read the judge scores
  on limited text. You always still get the **full guest list + LinkedIn links**.
- The local model needs a recent Chrome (138+) with built-in AI available; otherwise it
  falls back to the keyword heuristic automatically.
- LinkedIn's terms discourage automated viewing — the deep-read is opt-in, capped, and
  throttled, but use it deliberately.

## Multi-event dashboard (one place for every event)

Each event you **Save** becomes a card in a single dashboard — event name, date, time,
location, and its relevant people with LinkedIn links + a "connected" checkbox for
post-event follow-up. New event = new card; the list just grows. Two ways to view it:

- **Desktop, zero setup:** click **📋 Dashboard** in the panel → full-tab view of every
  saved event (reads your browser's local storage).
- **Mobile, one link:** deploy `server/` (rides your gtm-idea Render + Neon Postgres),
  set **Sync URL + token** in Settings, and open `https://<your-app>/board?token=SECRET`
  on your phone. Every Save syncs there automatically. See `server/README.md`.

## Files

- `manifest.json` — MV3 config
- `background.js` — orchestration (scan, Luma enrich, LinkedIn deep-read, site fetch)
- `sidepanel.html/.css/.js` — the assistant UI
- `lib/icp.js` — default ICP + heuristic scorer
- `lib/judge.js` — local / Claude / heuristic judge
- `dashboard/index.html` — the multi-event dashboard (desktop + mobile)
- `server/` — optional FastAPI backend for the one mobile link (Postgres-backed)
