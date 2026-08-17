# ICP Scout — dashboard backend

Serves **one private, always-updating link** you open on your phone. The extension
POSTs each saved event here; the dashboard reads them back.

## Run locally (test on your phone over Wi-Fi)

```bash
cd server
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

- Open on your laptop: `http://localhost:8000/board?token=pick-a-long-secret`
- Open on your phone (same Wi-Fi): `http://<your-laptop-ip>:8000/board?token=pick-a-long-secret`
- In the extension → Settings → **Sync URL** = `http://<your-laptop-ip>:8000`, **Sync token** = same secret.

Local mode stores to `server/_events.json` (no database needed).

## Deploy for a real anywhere-link (free, persistent)

You already run FastAPI on Render with Neon Postgres — reuse it:

1. Push this repo. In Render, add a **new Web Service** from `luma-icp-scout/server`
   (or add these routes to your existing service). Build: `pip install -r requirements.txt`.
   Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`.
2. Set env var **`DATABASE_URL`** to your Neon connection string (data then persists —
   Render's free disk is ephemeral, so Postgres is required for a durable link).
3. Your link: `https://<service>.onrender.com/board?token=your-long-secret`
4. Extension → Settings → **Sync URL** = `https://<service>.onrender.com`, **Sync token** = same secret.

Now every time you hit **Save event** in the extension, that event upserts to the
dashboard. Open the one link on any device to see all events, newest first, with each
person's LinkedIn and a "connected" checkbox for post-event follow-up.

## Security

The token is a shared secret in the URL — anyone with the link sees that token's data.
Use a long random string and don't share the link. (Upgrade path: swap for real auth later.)
