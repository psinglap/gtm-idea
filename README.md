# Event outreach — meet people at events, then actually follow up

You go to events. You mean to follow up. You don't, because the follow-up is the boring part:
find who else was there, work out which of them matter, find their email, write the note.

This does that loop end to end, on a schedule, from your own accounts.

```
1  sync every event from your Luma account
2  register for the free ones worth attending
3  re-check the ones awaiting host approval
4  read the guest lists of past events you attended
5  guest -> LinkedIn -> Apollo -> an ICP judge decides who is worth writing to
6  write the note from your template
7  send it, capped and paced, never twice to the same person
```

Steps 1–4 need a logged-in browser, so they run in a Chrome extension on your machine.
Steps 5–7 run on a server on a schedule.

> **You bring your own accounts.** No credentials, no identity and no email copy ship with this
> repo. It cannot send anything until you connect your own.

---

## Try it in two minutes, before connecting anything

No database, no keys, no accounts. It falls back to SQLite and a heuristic judge:

```bash
git clone https://github.com/psinglap/gtm-idea.git && cd gtm-idea
make install PYTHON=python3.12        # 3.9–3.12; PYTHON= is optional
make test                             # 334 tests, no network, no credentials
make api                              # http://localhost:8000
curl localhost:8000/health            # {"status":"ok","store":"sqlite"}
```

That gets you a running API and a dashboard. It won't send mail or read a guest list yet —
that needs the accounts below.

---

# Setup

Nine steps. Steps 1–3 are infrastructure, 4–6 are *you*, 7–9 are running it for real.

## 1 · Get the accounts

| What | Why | Cost |
|---|---|---|
| **Postgres** — [neon.tech](https://neon.tech) | Everything durable: events, guests, verdicts, the send ledger | Free tier is plenty |
| **A host** — [railway.app](https://railway.app), Render, Fly | Runs the API; needs a stable HTTPS URL for Google's OAuth callback | Free/hobby |
| **Google Cloud project** | A Gmail OAuth client so it can send **from your own mailbox** | Free |
| **Apollo** — [apollo.io](https://apollo.io) | Turns a LinkedIn profile into a verified work email. The paid bottleneck | Paid |
| **An LLM key** — Cerebras, Groq, Gemini, OpenRouter | Judges whether a guest matches your ICP | Free tiers exist |
| **Your own Luma + LinkedIn logins** | Used *in your browser*. Nothing is stored anywhere | Free |

**On Luma and LinkedIn:** there is no credential field for them, anywhere in this repo. The Chrome
extension uses the session your browser is already signed into. Sign out and it simply stops.

## 2 · Point it at your database

Neon → create a project → copy the **pooled** connection string.

```bash
cp .env.example .env
```

```
WG_STORE=postgres
DATABASE_URL=postgresql://user:pass@host/db?sslmode=require
```

Leave these unset and it uses a local SQLite file — fine for trying, not for running the schedule,
because the extension, the API and the cron all need the same database.

## 3 · Fill in the rest of `.env`

```
WG_LLM_PROVIDER=cerebras
CEREBRAS_API_KEY=...                 # or GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY

WG_GOOGLE_CLIENT_ID=...              # Google Cloud -> Credentials -> OAuth client (Web application)
WG_GOOGLE_CLIENT_SECRET=...
WG_GOOGLE_REDIRECT_URI=http://localhost:8000/oauth/google/callback
WG_GMAIL_ACCOUNT=you@yourdomain.com  # so you cannot connect the wrong mailbox by accident
```

Add that exact redirect URI to your OAuth client's **Authorised redirect URIs** in Google Cloud, or
the connect button fails. When you deploy, add the deployed one too.

**Leave `WG_SECRET_KEY` blank** — one is generated and stored on first boot. If you do set it, never
change it afterwards: every stored credential becomes undecryptable.

## 4 · Connect Gmail and Apollo — in the app, not in a file

Start it (`make api`, then `make web` for the dashboard) and open the **Connections** panel:

- **Connect Google** → the OAuth consent screen → done. Scope is `gmail.compose`; your mail is
  never read.
- **Apollo** → paste your API key.

Both are encrypted at rest in *your* database. Neither is an environment variable, and neither
leaves your deployment.

## 5 · Set your ICP — do this before the first run

```bash
cp config/icp.example.json config/icp.json
```

Three lists to edit:

```json
{
  "target_roles":     ["who qualifies"],
  "not_target_roles": ["senior, but the wrong function"],
  "never_targets":    ["absolute — no other signal overrides this"]
}
```

Until you do this, the judge applies the **example** ICP, which is one company's and almost
certainly not yours. Check which is live:

```bash
curl localhost:8000/outreach/settings | grep icp_source
```

A malformed file is a loud error, never a silent fall back to the example — the symptom of the
wrong ICP is verdicts that look subtly off weeks later.

## 6 · Write your email and your registration answers

Both start blank on purpose, and the app **refuses to send** until the email is written.

- **Dashboard → Event Outreach → template.** Your subject and body, your calendar link, your
  signature. Four substitutions are available — `{first_name}`, `{name}`, `{event_name}` (short,
  for the subject) and `{event_place}` ("the Wild AI SF event at Frontier Tower", for the body).
  No LLM touches this, so nothing needs proofreading before it goes out at volume.
- **Dashboard → Answers.** What to type into event registration forms — your name, company, role,
  links. Anything you haven't answered is reported back to you rather than guessed at, so the
  first thing a new install does is ask, not invent.

## 7 · Install the Chrome extension

The server can't reach Luma or LinkedIn. This is the half that can.

1. `chrome://extensions` → **Developer mode** → **Load unpacked** → select `luma-icp-scout/`
2. Open the side panel → enter your API URL and your company URL → **Connect**
3. Be signed in to Luma and LinkedIn in that same browser

It pairs itself with a token and needs no login of its own.

## 8 · Deploy — two services from the same repo

**The API**

- Deploy from your fork; the Dockerfile's `CMD` already honours `$PORT`
- Healthcheck path `/health`
- Set every variable from steps 2–3, and update `WG_GOOGLE_REDIRECT_URI` to the deployed domain
  (and add it in Google Cloud)
- Verify: `curl https://your-app/health` → `{"store":"postgres"}`. If it says `sqlite`, your
  `DATABASE_URL` didn't take.

**The scheduled pass** — a second service, same repo:

- Start command: `python scripts/outreach_cron.py`
- Cron schedule (UTC): `0 15,18,21,23 * * *`

Four runs a day against a per-hour cap means mail trickles out instead of arriving as 200 at once.
That example is 8am/11am/2pm/4pm US Pacific in summer; **UTC ignores daylight saving**, so set it
for your own timezone.

No cron service on your host? Set `WG_IN_PROCESS_SCHEDULER=1` and the API runs the schedule
itself, catching up any slot it missed while down. Running both is safe — they take a shared
45-minute lease in the database, so only one does the work.

`WG_SCHEDULER=0` is the master off switch. Then run it by hand from the dashboard button or
`python scripts/outreach_cron.py --force`.

## 9 · Go live

It starts in **draft** mode. Messages land in Gmail Drafts so you can read what it produces
against real people before anything leaves.

When you're happy:

```
WG_OUTREACH_MODE=send
WG_OUTREACH_DAILY_CAP=200
WG_OUTREACH_HOURLY_CAP=30
WG_ENRICH_LIMIT=1000                 # Apollo lookups per run — your spend ceiling
```

**Size Apollo before you start.** Roughly **15% of lookups produce a verified email** — event
guest lists are full of students, indie hackers and people at stealth or non-US companies Apollo
has no record of, and guessed addresses are held back deliberately. So 1,000 lookups a run × 4
runs is up to 4,000 credits a day to yield in the region of 150 sendable addresses.

**Full reference:** [SETUP.md](SETUP.md) — every variable, every account, the cost arithmetic.

---

## What state this is in

The two halves of this repo are not equally finished.

**Event outreach — everything above — is the tested half.** It has run unattended against live
Luma events and a real mailbox, sending real mail at a daily cap. Most of the code comments are
notes about what broke in production and what was done about it.

**There is also a signal-finder half** (`customer_list`, social/hiring/fundraising signals,
competitive intelligence) which is earlier and less proven. It's left in because it's useful
reading and a reasonable base, but don't assume it's battle-tested. If you want that half, expect
to finish it yourself.

## Rough edges — worth knowing before you invest a weekend

- **The Chrome extension isn't on the Web Store.** Load it unpacked.
- **The browser half only runs while your laptop is awake**, with Chrome open and both sites
  signed in. No way around it — those sites need a real session.
- **Apollo is the real cost and the real bottleneck.** See step 9.
- **The signal-finder half is not battle-tested.**

## What it deliberately does not do

- **No LLM writes your emails.** One static template, two substituted fields.
- **Never registers for a paid event.** Free only, always.
- **Never emails anyone twice**, anyone already in a mail thread with you, anyone with a meeting
  already booked, anyone at a `.edu` address, or any address at a catch-all domain.
- **No tracking pixels.**

## Safety defaults

Chosen so a mistake costs nothing:

- `WG_OUTREACH_MODE=draft` — nothing sends until you change it
- The shipped template **cannot be sent** while it still says `YOUR NAME`
- The registration answer bank ships **empty**, so it can never submit somebody else's identity
  into a stranger's form
- Per-hour and per-day caps, on by default
- `tests/test_no_private_data.py` fails the build if an identifying string or a real email address
  gets committed. Put your own terms in `tests/private-denylist.txt` (gitignored) and it enforces
  yours too

## Layout

```
apps/api/              FastAPI. 31 of its 43 endpoints are the outreach loop.
apps/web/              React dashboard: funnels, run history, live activity log
luma-icp-scout/        Chrome extension — the half that needs your browser session
config/icp.example.json  who counts as a target; copy to config/icp.json
packages/warmgraph/
  outreach/            scheduling, Luma ingest, registration, template, bounces, digest
  agents/activities/   the pipeline: judge -> enrich -> send, chained by outreach_daily
  storage/             Postgres + SQLite behind one interface
  connections/         Google, Apollo, encrypted credential storage
scripts/outreach_cron.py   the scheduled pass
tests/                 334 tests
```

## Docs

- **[SETUP.md](SETUP.md)** — full reference for every account, key, variable and schedule
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the pieces fit
- [DEPLOY.md](DEPLOY.md) — deployment specifics

## A note on the code comments

They're long, and they're mostly incident reports: what broke, what the wrong behaviour looked
like, and why the fix is shaped the way it is. A registration flow that reported success for
events it never registered for; a missing word boundary that failed 157 invitations; a verified
email that reached the wrong person because the domain was catch-all. If something looks oddly
written, the comment above it usually says why.

## Licence

MIT. It automates *your* accounts on *your* behalf — how you use it, and whether that respects the
people you write to, is on you.
