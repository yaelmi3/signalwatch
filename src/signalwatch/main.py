from pathlib import Path

from fastapi import FastAPI

from signalwatch.configuration import router

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

app = FastAPI(title="SignalWatch")
app.include_router(router)
app.frontend("/", directory=FRONTEND_DIST, check_dir=True, fallback=None)
