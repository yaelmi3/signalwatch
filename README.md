# SignalWatch

Competitor content monitoring with audience-specific insights.

[Draft technical design](docs/technical_design_records/0001_signalwatch.md)

The starter serves a React greeting page. FastAPI serves the compiled Vite frontend and API docs from one origin.

## Local setup

Use uv, Python 3.13, Node.js 24, and pnpm 11. Run from the repository root:

```sh
uv sync
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run uvicorn signalwatch.main:app --reload
```

Open [the greeting page](http://127.0.0.1:8000/) or [the API docs](http://127.0.0.1:8000/docs).
Rebuild the frontend after frontend changes. The frontend build must exist before starting FastAPI or running tests.

## Checks

After building the frontend:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
