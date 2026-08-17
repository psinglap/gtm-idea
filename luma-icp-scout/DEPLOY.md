# Deploying Luma ICP Scout (for others to use)

Two independent pieces:
- **A) The live dashboard backend** — so you (or anyone using the extension) can view their
  scraped list anytime, on any device, via a private link.
- **B) The extension itself** — so others can install it.

---

## A) Deploy the backend → "view the list anytime, any device"

You already run FastAPI on Render + Neon Postgres, so reuse that.

1. Push this repo to GitHub.
2. Render → **New → Web Service** → pick this repo → set **Root Directory** = `luma-icp-scout/server`
   - **Build:** `pip install -r requirements.txt`
   - **Start:** `uvicorn app:app --host 0.0.0.0 --port $PORT`
3. Add env var **`DATABASE_URL`** = your Neon connection string (so data persists across restarts).
4. Deploy. Your base URL is e.g. `https://icp-scout.onrender.com`.

**How each user's data stays private & separate:** every install auto-generates a random
**token** (stored locally). The dashboard is `/(board)?token=<their-token>`, and the backend
only returns rows for that token. Different users = different tokens = different boards.

**Wire the extension to it (once):** before distributing, set the backend URL as the default
so users don't have to configure anything — in `sidepanel.js` change:
```js
const DEFAULTS = { ..., syncUrl: "https://icp-scout.onrender.com", syncToken: "" };
```
(Leave `syncToken` empty — it auto-generates per user.) Now every install syncs automatically,
and the **🔗 Live link** button opens that user's private board on any device.

Without setting a default `syncUrl`, each user can still paste it themselves in Settings.

---

## B) Distribute the extension

### Option 1 — Chrome Web Store (best for "used by others", one-click install)
1. One-time: create a Chrome Web Store **developer account** ($5) at
   https://chrome.google.com/webstore/devconsole
2. Zip the `luma-icp-scout/` folder (must contain `manifest.json` at the top level).
3. Upload, fill the listing (see `store-listing.md`), add screenshots, submit for review (~1–3 days).
4. ⚠ Reviewers scrutinize extensions that automate other sites (LinkedIn/Luma reading). Keep the
   description honest: it reads pages the signed-in user can already see, for their own use.

### Option 2 — Share a packed build (a few people, no review)
`chrome://extensions` → **Pack extension** → select `luma-icp-scout/` → produces a `.crx` + `.pem`
key. Share the `.crx`; they drag it onto `chrome://extensions`. (Chrome may warn about non-store
extensions.)

### Option 3 — Unpacked (just you / devs)
`chrome://extensions` → **Load unpacked** → select `luma-icp-scout/`.

---

## Recommended path for you
1. Deploy the backend (A) so you get the live link today.
2. Set the default `syncUrl` to your backend, then Web Store (B, Option 1) so others install in one click
   and their lists are viewable anytime with zero setup.
