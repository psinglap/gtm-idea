.PHONY: install test eval eval-fresh api web demo mcp-install

# Python 3.9 - 3.12. `python3` on a current machine may be 3.13/3.14, which some pinned
# dependencies do not build against yet — so it is overridable:  make install PYTHON=python3.12
PYTHON ?= python3

install:
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install -U pip
	./.venv/bin/pip install -r requirements.txt

test:
	./.venv/bin/pytest -q

# Offline eval: score customer_list against the golden set + your approve/reject feedback.
# `eval` scores from the warm corpus (cheap); `eval-fresh` re-scrapes every signal first.
eval:
	./.venv/bin/python scripts/eval.py

eval-fresh:
	./.venv/bin/python scripts/eval.py --fresh

api:
	./.venv/bin/uvicorn main:app --reload --app-dir apps/api --port 8000

demo:
	./.venv/bin/python scripts/ci.py $(URL)

web:
	cd apps/web && npm install && npm run dev

# MCP server needs Python >= 3.10 in its own venv
mcp-install:
	cd mcp && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
