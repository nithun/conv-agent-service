"""Shared fixtures for ingress-layer tests: an httpx AsyncClient against the
real FastAPI `app` object, with `app.state.redis` swapped for fakeredis so
tests never need a live Redis, per tests/ingress' async-stack convention
(tap-agent-service/CLAUDE.md's Test layout section).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import fakeredis.aioredis
import httpx
import pytest

from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.distress_handler = None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
