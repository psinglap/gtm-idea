# luma-runner

Runs the daily Luma loop on its own: read both feeds, register for what the queue returns, verify
every registration against Luma's own API.

This replaces `luma-icp-scout/worker.js` as the thing that actually executes. That file holds the
same loop but is built on `chrome.tabs` / `chrome.alarms` / `chrome.storage`, so it only runs
inside a Chrome extension — and we are not shipping one.

All page-side logic lives in `luma-icp-scout/lib/luma-page.js` and is injected at runtime. This
runner contains no selectors. Keep it that way: a second copy of that logic is what caused the
worst bug in this system, where the browser's own question-matcher silently disagreed with the
server's and every stored answer missed.

## Setup

```bash
cd tools/luma-runner
npm install
node run.js login          # opens Chrome; sign in to Luma yourself, then press Enter
```

The session lives in `.chrome-profile/` (gitignored). No password is ever handled by this code.

## Running

```bash
export WG_TOKEN=<workspace token from POST /workspace>
export WG_API=http://localhost:8000

node run.js sync        # step 1 — read feeds, post to /outreach/events
node run.js register    # step 2 — register for the queue
node run.js daily       # both, what cron runs
```

## Daily, unattended

```
0 9 * * *  cd /path/to/gtm-idea/tools/luma-runner && WG_TOKEN=... /usr/local/bin/node run.js daily >> run.log 2>&1
```

Only fires while the machine is awake. `WG_HEADLESS=1` runs without a visible window — leave it
off at first, so you can watch what it does.

## What it will and won't do on your behalf

Driven by the answer bank, not by this code:

| Answer-bank key | Effect |
|---|---|
| `accept event terms` = `yes` | ticks "I agree to the event terms" |
| `sign event terms as` = `<name>` | types that name into an "Accept Terms" signature pad |

Both unset by default. Without them those events are reported and skipped, never guessed at.
Questions with one true answer only the founder knows — ARR, funding, headcount, "have you ever…"
— are never answered by a model; they come back in the questions box.

## The one rule

Success is decided by `api.luma.com/home/get-events`, never by page text. A run once reported two
registrations and delivered zero, because a success regex matched incidental copy after a click
that reopened the form instead of submitting it. Nothing is written to the API until Luma itself
confirms the event in the user's list.

## Reusing your existing Chrome login

`WG_PROFILE` can point at your real Chrome profile instead of the dedicated one:

```bash
WG_PROFILE="$HOME/Library/Application Support/Google/Chrome/Default" node run.js daily
```

Two caveats, which is why it is not the default:

- **Chrome must be fully quit** while the runner uses it. Chrome holds an exclusive lock on a
  profile directory, so the runner cannot share one with a running browser.
- It hands the automation your **entire** browser session, every site, not just Luma.

For a daily cron, sign in once to the dedicated profile instead. It costs one sign-in and then
never interferes with normal browsing.
