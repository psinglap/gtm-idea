"""
ICP Scout dashboard backend — tiny FastAPI app that stores saved events and
serves one private, always-updating dashboard link (mobile-friendly).

Storage: Postgres via DATABASE_URL (persists — use your Neon URL on Render).
         Falls back to a local JSON file when DATABASE_URL is unset (dev only).

Endpoints:
  POST  /api/events   {token, event}                       upsert one event
  GET   /api/events?token=...                              list events (JSON)
  PATCH /api/person   {token, event, linkedin, connected}  toggle "connected"
  GET   /  and  /board?token=...                           serve the dashboard

Run locally:   pip install -r requirements.txt && uvicorn app:app --reload
The token is a shared secret in the URL — treat the link as private.
"""
import json
import os
import pathlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

DB_URL = os.getenv("DATABASE_URL", "")
DASHBOARD = pathlib.Path(__file__).parent.parent / "dashboard" / "index.html"
JSON_STORE = pathlib.Path(__file__).parent / "_events.json"  # dev fallback

app = FastAPI(title="ICP Scout Board")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


# ---------------- storage layer ----------------
def _pg():
    import psycopg  # lazy import so dev mode needs no driver

    return psycopg.connect(DB_URL, autocommit=True)


def init_db() -> None:
    if not DB_URL:
        return
    with _pg() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS icp_events (
                   token TEXT NOT NULL,
                   url   TEXT NOT NULL,
                   data  JSONB NOT NULL,
                   updated_at TIMESTAMPTZ DEFAULT now(),
                   PRIMARY KEY (token, url)
               )"""
        )


def upsert_event(token: str, event: dict[str, Any]) -> None:
    url = event.get("url") or event.get("name") or ""
    if DB_URL:
        with _pg() as c:
            c.execute(
                """INSERT INTO icp_events (token, url, data, updated_at)
                   VALUES (%s, %s, %s, now())
                   ON CONFLICT (token, url)
                   DO UPDATE SET data = EXCLUDED.data, updated_at = now()""",
                (token, url, json.dumps(event)),
            )
    else:
        store = _read_json()
        store.setdefault(token, {})[url] = event
        _write_json(store)


def list_events(token: str) -> list[dict]:
    if DB_URL:
        with _pg() as c:
            rows = c.execute(
                "SELECT data FROM icp_events WHERE token=%s ORDER BY updated_at DESC",
                (token,),
            ).fetchall()
        return [r[0] for r in rows]
    return list(_read_json().get(token, {}).values())


def set_connected(token: str, url: str, linkedin: str, connected: bool) -> None:
    events = list_events(token)
    for ev in events:
        if ev.get("url") == url:
            for p in ev.get("people", []):
                if p.get("linkedinUrl") == linkedin:
                    p["connected"] = connected
            upsert_event(token, ev)
            return


def _read_json() -> dict:
    if JSON_STORE.exists():
        return json.loads(JSON_STORE.read_text())
    return {}


def _write_json(d: dict) -> None:
    JSON_STORE.write_text(json.dumps(d))


# ---------------- API ----------------
class EventIn(BaseModel):
    token: str
    event: dict


class PersonIn(BaseModel):
    token: str
    event: str
    linkedin: str
    connected: bool


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"ok": True, "store": "postgres" if DB_URL else "json-file"}


@app.post("/api/events")
def post_event(body: EventIn):
    if not body.token:
        raise HTTPException(400, "token required")
    upsert_event(body.token, body.event)
    return {"ok": True}


@app.get("/api/events")
def get_events(token: str):
    if not token:
        raise HTTPException(400, "token required")
    return JSONResponse(list_events(token))


@app.patch("/api/person")
def patch_person(body: PersonIn):
    set_connected(body.token, body.event, body.linkedin, body.connected)
    return {"ok": True}


@app.get("/")
@app.get("/board")
def board():
    return FileResponse(DASHBOARD)


@app.get("/dashboard.js")
def dashboard_js():
    # index.html loads this relative to /board — serve it so the hosted page works.
    return FileResponse(DASHBOARD.parent / "dashboard.js", media_type="application/javascript")


@app.get("/privacy")
def privacy():
    # Public privacy-policy URL for the Chrome Web Store listing.
    return FileResponse(pathlib.Path(__file__).parent.parent / "privacy-policy.html", media_type="text/html")
