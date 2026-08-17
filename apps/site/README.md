# signal — marketing site

The public landing page. Plain static site (HTML + CSS + JS), no build step — deploys anywhere.

- `index.html` — the page (all copy lives here, so it's crawlable + LLM-quotable)
- `styles.css` — theme + layout (Midnight Violet `#220925` + Lemon Lime `#CFD11A`, light default, dark supported)
- `main.js` — light/dark toggle + URL capture (hands the entered domain to the app)

## Run locally

Any static server works. Pick one:

```bash
# Python (built in on macOS)
cd apps/site && python3 -m http.server 8080
# → open http://localhost:8080

# or Node
npx serve apps/site
```

## Deploy to prod

It's a static site — no build. On Vercel:

- **Root Directory:** `apps/site`
- **Framework preset:** Other (no build command, output = the folder itself)

Or drag the `apps/site` folder into Netlify / Cloudflare Pages / any static host.

## Before going live (TODOs)

1. **Brand name** — replace the placeholder `signal` wordmark + `S` glyph, and the `name`/title/OG/schema in `index.html`.
2. **App handoff** — set `APP_URL` in `main.js` to the real signup/app URL (currently the existing web app).
3. **Canonical + OG URLs** — swap `https://example.com/` in `index.html` for the real domain.
4. **OG image** — add a real share image (`og:image`) once the brand is set.
