# Setup — everything you have to connect

**This repo ships with no accounts, no credentials and no identity in it.** Nothing here can
send mail, spend money or register you for anything until you connect your own accounts. That is
deliberate, and there are tests that keep it that way (`tests/test_no_private_data.py`).

This page is the single list. If something needs an account, a key, a schedule or a login, it is
below.

---

## 1 · Accounts you need

| What | Why it is needed | Cost | Where |
|---|---|---|---|
| **Postgres database** | Everything durable lives here: events, guests, verdicts, the send ledger. | Free tier is enough to start | [neon.tech](https://neon.tech) (or any Postgres) |
| **A host for the API** | Long-running Python, holds encrypted credentials, needs a stable HTTPS URL for the Google OAuth callback. | Free/hobby tier | [railway.app](https://railway.app), Render, Fly, your own box |
| **Google Cloud project** | Gmail OAuth client, so the app can send from *your* mailbox. | Free | [console.cloud.google.com](https://console.cloud.google.com) |
| **Apollo** | Turns a LinkedIn profile into a verified work email. This is the paid bottleneck. | Paid; credits run out fast | [apollo.io](https://apollo.io) |
| **An LLM key** | Judges whether a guest matches your ICP. | Free tiers available | Cerebras, Groq, Gemini, OpenRouter, Anthropic |
| **Chrome + your own Luma login** | The extension registers for events and reads guest lists **as you**, in your own browser. | Free | [luma.com](https://luma.com) |
| **Your own LinkedIn login** | Same browser, for reading public profile headlines. | Free | [linkedin.com](https://linkedin.com) |

### About Luma and LinkedIn

There is **no Luma or LinkedIn credential anywhere in this repo, and there is no field to put
one in.** The Chrome extension runs in your browser and uses the session you are already signed
into. Nothing is stored, nothing is transmitted, and if you sign out it simply stops working.

That is also the honest limitation: the browser half only runs while your machine is awake with
Chrome open.

---

## 2 · Environment variables

Copy `.env.example` to `.env` and fill it in. For a deployment, set the same names in your host's
variables panel.

**Required to do anything:**

```
WG_STORE=postgres
DATABASE_URL=postgresql://...        # your Neon connection string (use the POOLED one)
```

**Required to judge people (pick one provider):**

```
WG_LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=...                 # or GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY
```

**Required to send mail:**

```
WG_GOOGLE_CLIENT_ID=...
WG_GOOGLE_CLIENT_SECRET=...
WG_GOOGLE_REDIRECT_URI=https://YOUR-APP-DOMAIN/oauth/google/callback
WG_GMAIL_ACCOUNT=you@yourdomain.com  # which mailbox to connect, so you cannot connect the wrong one
```

**Your ICP** is a file, not an env var: copy `config/icp.example.json` to `config/icp.json` and
edit the three lists (`target_roles`, `not_target_roles`, `never_targets`). Point `WG_ICP_FILE`
elsewhere if you prefer. A malformed file is a loud error, never a silent fall back to the
example — because the symptom of the wrong ICP is verdicts that look subtly off weeks later.

**Apollo** is connected in the app UI, not by env var — it is stored encrypted in your database.

**Sending behaviour — read these before switching to `send`:**

```
WG_OUTREACH_MODE=draft               # draft | send.  STARTS AT draft ON PURPOSE.
WG_OUTREACH_DAILY_CAP=200
WG_OUTREACH_HOURLY_CAP=30            # paced, so mail trickles out instead of arriving as a burst
WG_ENRICH_LIMIT=1000                 # Apollo lookups per run. This is your spend ceiling.
```

**Browser worker budgets:**

```
WG_LINKEDIN_DAILY_CAP=200            # profile reads/day; start low, raise once gated reads stay 0
WG_EVENT_REGISTER_CAP=25             # Luma registrations/day. Free events only, always.
WG_EVENT_HORIZON_DAYS=14             # only register for events starting within this window
WG_LUMA_DISCOVER_PLACE=              # DEFAULTS TO SAN FRANCISCO — set your own city
```

To find your city's id: open `luma.com/<your-city>` and search the page source for
`discplace-`. Leaving it on the default means you discover events in SF.

**Encryption:** leave `WG_SECRET_KEY` blank and one is generated and stored on first boot. If you
set it yourself, the key lives somewhere your database does not — better, but if you lose it every
stored credential becomes undecryptable. **Never change it on a running install.**

---

## 3 · Deploying

Two services from the same repo.

**The API**

- Deploy from your fork. The Dockerfile's `CMD` already honours `$PORT`.
- Healthcheck path: `/health`
- Set the variables from section 2
- Generate a domain, and put that hostname in `WG_GOOGLE_REDIRECT_URI` **and** in Google Cloud's
  authorised redirect URIs
- Check: `curl https://YOUR-APP/health` → `{"status":"ok","store":"postgres"}`. If it says
  `sqlite`, your `DATABASE_URL` did not take.

**The scheduled pass**

A second service, same repo, different start command:

- Start command: `python scripts/outreach_cron.py`
- Cron schedule: see below

`railway.json` deliberately carries **build config only**. Both services read it, so a
`startCommand` there would make the cron service run the API instead.

---

## 4 · Cron schedule — when it runs

The schedule is a cron expression on the scheduled-pass service, in **UTC**.

```
0 15,18,21,23 * * *     # 4 runs a day
```

That example is 8am, 11am, 2pm and 4pm US Pacific during daylight time. **UTC does not follow
daylight saving, so it drifts an hour in winter** — set it for your own timezone.

Four runs against a 50/hour cap is 200 a day in small batches rather than one burst, which is
better for deliverability.

There are two ways to fire the schedule and you can run either or both:

| | How to enable | Behaviour |
|---|---|---|
| **Cron service** (recommended) | Set the cron expression above | Host starts it at the scheduled time, it works, it exits. No polling. |
| **In-process scheduler** | `WG_IN_PROCESS_SCHEDULER=1` on the API | The API sleeps until the next slot and runs it itself. Catches up a slot it missed while down. |

Running both is safe: every automatic trigger takes a shared 45-minute lease in the database
first, so one does the work and the other returns quietly.

**The master switch** is `WG_SCHEDULER`. Set it to `0` and nothing automatic runs at all — use the
UI button, or `python scripts/outreach_cron.py --force`.

---

## 5 · The Chrome extension

The server half cannot reach Luma or LinkedIn. The extension does that, from your browser.

1. `chrome://extensions` → Developer mode → **Load unpacked** → select `luma-icp-scout/`
2. Open the side panel, enter your API URL and your company URL, and press **Connect**
3. Make sure you are signed in to Luma and LinkedIn in that same browser

Before publishing your own build to the Chrome Web Store, edit
`luma-icp-scout/privacy-policy.html` — it has a placeholder contact address you must replace.

---

## 6 · What you MUST fill in before the first send

The repo ships these blank on purpose. Until you set them, the app refuses to send rather than
sending something generic.

| What | Where | What happens if you skip it |
|---|---|---|
| **Your email template** | App → Event Outreach → template | Sending is **blocked**. The shipped template still says `YOUR NAME`, and `missing_fields()` refuses it. |
| **Your registration answers** | App → Answers | Every registration form is reported as an open question instead of being filled. Nothing is guessed. |
| **Your ICP** | `cp config/icp.example.json config/icp.json`, then edit | The judge applies the **example** ICP, which is one company's and almost certainly not yours. Check `GET /outreach/settings` → `icp_source`. |
| **Apollo key** | App → Connections | Nobody gets an email address, so nothing is ever sendable. |
| **Gmail** | App → Connections → Connect Google | Nothing sends. |

**Keep `WG_OUTREACH_MODE=draft` until you have read what it produces.** Drafts land in Gmail for
review. Switch to `send` only once the copy is yours and the queue looks right.

---

## 7 · What this costs

Apollo is the only part that reliably costs money, and it is worth sizing before you start.

Measured on a real run: **roughly 15% of Apollo lookups produce a verified email.** Luma guest
lists are full of students, indie hackers and people at stealth or non-US companies, and Apollo
simply has no record of them. Of those it does match, many come back with a guessed address
rather than a verified one, and those are held back deliberately — a wrong-recipient is worse
than a bounce, because nothing detects it.

So at `WG_ENRICH_LIMIT=1000` and four runs a day you are spending up to 4,000 lookups to produce
in the region of 150 sendable addresses. Plan credits accordingly, and expect enrichment to stop
mid-pass with a 422 when they run out.
