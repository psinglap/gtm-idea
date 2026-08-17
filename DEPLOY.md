# Deploying

Three pieces, three places:

| Piece | Where | Why there |
|---|---|---|
| API (`apps/api`) | Railway | Long-running Python, holds encrypted credentials, needs a stable HTTPS URL for the OAuth callback |
| Scheduled pass (`scripts/outreach_cron.py`) | Railway, second service | Same image, different start command and a cron schedule |
| UI (`apps/web`) | Vercel | Static React build |
| Database | Neon | Already live; everything else is stateless |

The browser half — Luma sync, registration, guest lists, LinkedIn — does NOT deploy. It needs a
logged-in Chrome, so it runs from the extension on a real machine and posts results to the API.

---

## 1 · API on Railway

New Project -> Deploy from GitHub -> your fork of this repo.

Railway reads `railway.json`, builds the Dockerfile, and starts the API via the Dockerfile's
own CMD (which already honours `$PORT`).

`railway.json` deliberately carries BUILD CONFIG ONLY. Both services in this project are built
from the same repo, so both read the same file, and Railway's config-as-code wins over dashboard
settings. A `startCommand` there would make the cron service run uvicorn instead of the cron
script, and a `healthcheckPath` would make it fail a check it can never answer and restart in a
loop that looks like a crash. Per-service commands belong in the dashboard.

Set the API's healthcheck in the dashboard: Settings -> Healthcheck Path -> `/health`.

Variables (Settings -> Variables):

    WG_STORE=postgres
    DATABASE_URL=<Neon pooled connection string>
    CEREBRAS_API_KEY=<...>
    WG_LLM_PROVIDER=cerebras
    WG_GOOGLE_CLIENT_ID=<...>
    WG_GOOGLE_CLIENT_SECRET=<...>
    WG_GOOGLE_REDIRECT_URI=https://<your-app>.up.railway.app/oauth/google/callback
    WG_CORS_ORIGINS=https://<your-vercel-app>.vercel.app
    WG_OUTREACH_MODE=draft
    WG_OUTREACH_DAILY_CAP=200
    WG_OUTREACH_HOURLY_CAP=30

**Do NOT set `WG_SECRET_KEY`.** The Fernet key that encrypts stored Gmail and Apollo credentials
was auto-generated and lives in Neon. Setting a different one here makes every stored credential
undecryptable — the connections will look present and fail to use.

Settings -> Networking -> Generate Domain. That hostname goes in `WG_GOOGLE_REDIRECT_URI` above
and in Google Cloud's authorised redirect URIs.

Check: `curl https://<your-app>.up.railway.app/health` -> `{"status":"ok","store":"postgres"}`.
If `store` says `sqlite`, `DATABASE_URL` did not take.

## 2 · The scheduled pass

Same project -> New -> GitHub Repo -> the same repo and branch. Then on that service:

- Settings -> Deploy -> Custom Start Command: `python scripts/outreach_cron.py`
- Settings -> Cron Schedule: `0 15,18,21,23 * * *`   (UTC — see SETUP.md section 4)

Four times a day, not once, on purpose: the send cap is per hour, so mail goes out in small
batches rather than as 200 at 9am. It is a no-op when the queue is empty. Those four slots are
UTC and do not follow daylight saving — set them for your own timezone.

Give it the same variables as the API — it talks to the same database and the same Google app.
It does not need `WG_CORS_ORIGINS` or a domain.

## 3 · The UI

There is no separate UI deploy. The Dockerfile builds `apps/web` in a first stage and the API
serves it, so one Railway service gives you both at the same URL.

That is deliberate. A separate host means a second deploy to keep in step, a build-time
`VITE_API_BASE` pointing across origins, and CORS. Same-origin removes all three: the bundle is
built with an empty API base, so it calls back relatively and can never point at the wrong
backend.

Open the Railway domain and the app is there. `/health` and `/outreach/...` still answer as
themselves — the static mount is registered last, after every API route.

## (alternative) UI on Vercel

Import the repo, root directory `apps/web`.

    VITE_API_BASE=https://<your-app>.up.railway.app

Build-time, not runtime: change it and you must redeploy. Without it the UI calls
`http://localhost:8000` and works only on the machine running the API.

## 4 · Google Cloud

Google Auth Platform -> Clients -> your OAuth client -> Authorised redirect URIs, add:

    https://<your-app>.up.railway.app/oauth/google/callback

Keep the localhost one for local work.

## 5 · Reconnect Gmail

The existing token was issued for `localhost:8000` and will not work from Railway. Open the
deployed UI and reconnect. Apollo needs no reconnect — an API key is not origin-bound.

## 6 · Point the extension at production

`luma-icp-scout` side panel -> server URL -> the Railway hostname. Until this is done the
extension posts to localhost and the deployed queue stays empty.

## Cold starts

The web service runs on a free plan, so it sleeps after ~15 minutes idle and takes 30-60s to
wake. That is expected, and every caller is built for it: the extension, the web UI and the
Playwright runner all retry with growing waits over about 67 seconds, so a wake-up shows as a
pause rather than an error.

What retries: a network error (a sleeping instance refuses the connection) and 408/429/502/503/
504 from the host's proxy while it boots. What does not: 401, 404, 422 and anything else — those
are real answers from a running server, and retrying them would only delay the truth.

The one thing outside our control is Google's OAuth redirect, which lands on the API directly.
If connecting Gmail fails once, click connect again — the second attempt hits a warm instance.
Upgrading the web service to the starter plan removes cold starts entirely if it becomes
annoying.

## 7 · Watch one cycle, then send

`WG_OUTREACH_MODE` starts at `draft`. Let the cron run, read the drafts it produces, then set it
to `send` on both services.
