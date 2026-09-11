"""FastAPI application: static web UI plus the JSON API."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .routes import create_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_sources()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .jobs import JobQueue

        app.state.queue = JobQueue(settings)
        try:
            yield
        finally:
            app.state.queue.shutdown()

    app = FastAPI(title="ds-yue-webui", version="0.1.0", lifespan=lifespan)
    app.include_router(create_router(settings))
    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.web_dir), html=True), name="web")
    return app
