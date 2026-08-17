# Warmgraph — Architecture & Operations (single source of truth)

_Current scope: the **event outreach** loop (Luma -> LinkedIn -> Apollo -> Gmail), which is
section 7 and is the tested half of this repo. The signal-finder pieces (customer list, social /
hiring / fundraising signals, competitive intelligence) share the same storage and agent
framework but are earlier and less proven — see the README._

_New here? Read [SETUP.md](SETUP.md) first; it lists every account and key you need._

## 1. What the system does (call flow)
```
client (web / MCP / curl / python)
        │
        ▼
apps/api (FastAPI, :8000)  ──or──  mcp/server.py  ──or──  scripts/ci.py
        │  (all thin wrappers — zero logic)
        ▼
packages/warmgraph  →  WarmgraphService.competitive_intelligence(url, depth)
        │
        ├─ scraper.crawl_site(url)         → Firecrawl (renders JS) / httpx
        ├─ profile.derive_profile(text)    → LLM (Cerebras) → CompanyProfile
        ├─ competitive.analyze_competition → LLM + Tavily (grounding/verify) → CompetitiveAnalysis
        └─ store.save_ci_report(report)    → Neon Postgres (ci_reports)
        ▼
CompetitiveIntelligenceReport  (profile + competitive landscape)
```
**Rule:** all logic lives in the core library `packages/warmgraph`; the API, MCP server,
CLI, and web app are interchangeable thin clients of `WarmgraphService`.

## 2. Where everything lives
| Concern | Location | Runs at (local) | Hosted on |
|---|---|---|---|
| **Backend / core engine** | `packages/warmgraph/` (logic) | — (library) | inside the API container |
| **HTTP API** | `apps/api/main.py` (FastAPI) | http://localhost:8000 (`/docs`) | Render / Cloud Run |
| **Frontend** | `apps/web/` (React + Vite) | http://localhost:5173 | Vercel |
| **MCP server** | `mcp/server.py` | stdio (uv venv) | local now; remote HTTP later |
| **CLI** | `scripts/ci.py` | terminal | — |
| **Database** | Neon (serverless Postgres) | — | **Neon** (already hosted) |
| **LLMs** | hosted APIs via `llm/registry.py` | — | Cerebras / Groq / Gemini (free tiers) |
| **Our own models** | none yet | — | **Modal** (serverless GPU) when we train them |

## 3. Database architecture
- **Engine:** Postgres on **Neon** (project `steep-field-...`, branch `production`).
- **View it:** Neon console → **Tables** (browse rows) and **SQL Editor** (run queries). Or
  connect any SQL client (TablePlus/DBeaver) with the `DATABASE_URL`.
- **Schema (CI scope — one table):**
  ```sql
  ci_reports (
    id          TEXT PRIMARY KEY,   -- ci_xxxx…
    url         TEXT,               -- company analyzed
    created_at  TIMESTAMPTZ,
    data        JSONB               -- the full CompetitiveIntelligenceReport
  )
  ```
- Pattern: key columns for lookup + a `JSONB` `data` blob holding the full Pydantic object
  (schema evolves without migrations during dev). **pgvector** will be added as a `vector`
  column when embeddings arrive. Local dev/tests use SQLite (same interface, `Store`).

## 4. Where are the models? (important)
- **Today: we deploy NO models of our own.** The engine calls **hosted LLMs** (Cerebras
  `gpt-oss-120b`, default) over their APIs, routed through `llm/registry.py`. So there is no
  GPU/model to host right now — cost is just the providers' free tiers.
- **When we train our own small models** (later — classifiers, embeddings, fine-tunes): train
  + serve on **Modal** (serverless GPU, pay-per-use), artifacts in object storage (R2/S3),
  and point the relevant registry task at the Modal endpoint — callers don't change. No
  always-on GPU bill until a model is worth serving.

## 5. Deploy it now — cheapest path (≈ $0/month)
| Layer | Host | Cost now |
|---|---|---|
| API | **Render** (Docker, free web service) — or Cloud Run for scale-to-zero | $0 (spins down idle) |
| Frontend | **Vercel** (free) | $0 |
| Database | **Neon** (free tier) | $0 |
| LLMs | Cerebras/Groq/Gemini free tiers | $0 |

**Steps:**
1. Push the repo to GitHub (`git remote add origin <repo> && git push -u origin main`).
2. **API → Render:** New → Blueprint → pick this repo (`render.yaml` auto-configures it) →
   set secrets in the dashboard: `DATABASE_URL`, `CEREBRAS_API_KEY`, `TAVILY_API_KEY`,
   `FIRECRAWL_API_KEY`. Deploys the `Dockerfile`. Health check: `/health`.
3. **Frontend → Vercel:** import repo, root `apps/web`, set `VITE_API_BASE` to the Render URL.
4. DB already on Neon.

Upgrade path when traffic grows: API → **Cloud Run** (scale-to-zero, pay-per-request);
add **Upstash Redis + a worker** so long CI runs are async; **Modal** for our models;
**Langfuse + Sentry** for LLM observability.

## 6. End-to-end visibility (how to see what's happening)
- **API surface:** http://localhost:8000/docs (Swagger — run every endpoint).
- **Data:** Neon console → Tables / SQL Editor (`select id,url,created_at from ci_reports`).
- **Logs:** uvicorn stdout locally; Render logs in prod.
- **This file:** the living map. **Later:** Langfuse for per-LLM-call cost/latency/quality.

## 7. Event outreach (Luma → LinkedIn → Apollo → Gmail)

A second pipeline on the same foundation: find the people at events you attended, judge them
against your ICP, and email them. Split across two runtimes because half of it *cannot* run on
a server.

```
  Chrome (extension worker, resumable)          Server (cron, laptop can be closed)
  ────────────────────────────────────          ──────────────────────────────────
  A. register   luma.com/sf + invited backlog
  B. scan       guest lists of ended events  ─▶  event_contacts (status=queued)
  C. read       LinkedIn, 15-20s apart       ─▶  status=profiled
                                                 D. event_icp_judge   → target/reject
                                                 E. outreach_enrich   → Apollo email
                                                 F. outreach_send     → Gmail draft/send
```

**Why the split:** LinkedIn only tolerates reads from the user's own logged-in session, and
Luma has no third-party OAuth at all, so A–C are browser-bound. D–F need no browser, so they
run hourly on Render and make progress on whatever the browser already produced.

**Hard gate:** nobody is judged, enriched or emailed without a LinkedIn profile actually being
read. A Luma display name plus a one-line bio cannot identify a person.

**Resumability:** `event_contacts.status` *is* the queue. Rows are leased for 10 minutes; if
Chrome quits the lease expires and the row returns to `queued`, so closing the laptop costs at
most the one profile in flight.

### Tables added
| Table | Scope | Holds |
|---|---|---|
| `event_contacts` | per client | **The event lead list + the work queue.** FKs to `raw_events`, `people`, `customers`. Verdict, score, reason, email, status, lease. |
| `event_registrations` | per client | *Your* ticket key, approval status, scanned-at, subject-line name. Split out because `raw_events` is deduped by url and therefore SHARED between clients at the same event. |
| `outreach_messages` | per client | Send ledger + audit trail. The suppression source (the mailbox is never read). |
| `connections` | per client | Gmail refresh token / Apollo key, Fernet-encrypted. Luma + LinkedIn store nothing. |
| `do_not_contact` | per client | Paste-in exclusions, checked at send time. |

### No sign-in
"Connect Gmail" is a *capability* connection, not a login — Google identity is never used to
authenticate into the app. Tenancy is a random per-install **workspace token** that maps to a
`company_id` (the same pattern as `luma-icp-scout/server/app.py`). Scopes are `gmail.send` +
`gmail.compose` only; no `gmail.readonly`, which keeps this off Google's restricted-scope
review path.

## 8. Locked target stack (for reference as we build forward)
Python core + FastAPI · Neon Postgres (+pgvector) · R2/S3 · Redis queue + workers (async
jobs) · Modal + vLLM for our own models (via the model registry) · React/Vercel · MCP ·
Langfuse + Sentry · GitHub Actions + Docker. Build **async-first** (CI jobs are long/bursty).
