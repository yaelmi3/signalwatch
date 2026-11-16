from pathlib import Path

from fastapi import FastAPI

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

app = FastAPI(title="SignalWatch")
app.frontend("/", directory=FRONTEND_DIST, check_dir=True, fallback=None)
