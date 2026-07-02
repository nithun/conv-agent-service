"""FastAPI application entrypoint.

Scaffold only (WBS 0.1) - registers the health route. Webhook ingress
(WBS 1.1+) mounts additional routers here in later tasks.
"""

from __future__ import annotations

from fastapi import FastAPI

from app.config.settings import get_settings
from app.ingress.health import router as health_router

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.include_router(health_router)
