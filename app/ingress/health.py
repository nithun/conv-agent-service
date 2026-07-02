"""Health-check route.

No side effects, no downstream calls - this is a liveness probe, not a
readiness probe against Redis/Frappe/Glific. Keeping it dependency-free
means it stays fast and reliable during compose bring-up ordering.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Always 200 if the process is up and serving."""
    return {"status": "ok"}
