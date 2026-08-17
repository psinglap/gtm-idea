# One image, one URL: the React UI is built and served by the same FastAPI process that serves
# the API. Deploying them separately would mean a second host, a second deploy to keep in step,
# a build-time VITE_API_BASE pointing across origins, and CORS. Same-origin removes all four —
# the UI calls "/outreach/..." relatively and can never be pointed at the wrong backend.

# --- stage 1: build the UI -------------------------------------------------- #
FROM node:20-slim AS ui
WORKDIR /ui
COPY apps/web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web ./
# Empty base URL on purpose: the bundle then uses relative paths and talks to whatever host it
# was served from. Nothing to configure per environment, nothing to get out of sync.
ENV VITE_API_BASE=""
RUN npm run build

# --- stage 2: the API, plus the built UI ------------------------------------ #
FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# Install deps (editable core package needs packages/ present first).
# Both API and MCP deps go in one image; the MCP service overrides the run command.
COPY requirements.txt requirements-mcp.txt ./
COPY packages ./packages
RUN pip install -r requirements.txt -r requirements-mcp.txt

# App code. `scripts/` matters as much as the API: the scheduled outreach pass runs
# `python scripts/outreach_cron.py` from this same image, and without it the cron service
# starts, fails to find the file, and reports a crash loop that looks like a platform problem.
COPY apps/api ./apps/api
COPY scripts ./scripts
COPY --from=ui /ui/dist ./apps/api/static

EXPOSE 8000
# One image, two roles. WG_MODE=mcp runs the remote MCP server; otherwise the REST API.
CMD ["sh", "-c", "if [ \"$WG_MODE\" = mcp ]; then exec python -m warmgraph.mcp_http; else exec uvicorn main:app --app-dir apps/api --host 0.0.0.0 --port ${PORT:-8000}; fi"]
