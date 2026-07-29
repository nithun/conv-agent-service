"""Static config-shape check (L-025, `docs/planning/BC-C-liveness-alert-
tasks.md` task 2.2): confirms the `worker` service in docker-compose.yml
carries a `healthcheck:` stanza (mirroring the `litellm` service's own
F-078 precedent) and `restart: unless-stopped`, so `docker compose ps`
surfaces a dead/hung worker as unhealthy instead of requiring a human to
notice it's merely not `Up` (F-083's exact gap).

Parses the raw YAML (no live Docker call) — a real liveness check needs a
running stack (see docs/planning/BC-C-liveness-alert-tasks.md task 4.2's
live drill), which a plain `pytest` run should not require.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).parent.parent.parent / "docker-compose.yml"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_worker_service_has_a_healthcheck() -> None:
    compose = _load_compose()
    worker = compose["services"]["worker"]
    assert "healthcheck" in worker
    assert "test" in worker["healthcheck"]


def test_worker_healthcheck_does_not_use_curl_or_wget() -> None:
    # This image has neither installed (same reason litellm's own
    # healthcheck uses a python3 CMD, not curl — see that service's
    # comment block in docker-compose.yml).
    compose = _load_compose()
    worker = compose["services"]["worker"]
    test_cmd = " ".join(str(part) for part in worker["healthcheck"]["test"])
    assert "curl" not in test_cmd
    assert "wget" not in test_cmd


def test_worker_healthcheck_invokes_the_worker_healthcheck_module() -> None:
    compose = _load_compose()
    worker = compose["services"]["worker"]
    test_cmd = " ".join(str(part) for part in worker["healthcheck"]["test"])
    assert "app.worker.healthcheck" in test_cmd


def test_worker_service_restarts_unless_stopped() -> None:
    compose = _load_compose()
    worker = compose["services"]["worker"]
    assert worker.get("restart") == "unless-stopped"


def test_litellm_service_still_has_its_own_healthcheck_unchanged() -> None:
    # Regression guard: this task must not touch litellm's existing,
    # already-correct healthcheck (F-078) while adding worker's.
    compose = _load_compose()
    litellm = compose["services"]["litellm"]
    assert "healthcheck" in litellm
    assert litellm.get("restart") == "unless-stopped"
