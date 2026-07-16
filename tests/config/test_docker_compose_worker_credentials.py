"""Static config-shape check (BR-004 Task 1 AC): confirms the `worker`
service in docker-compose.yml mounts the GCP SA key read-only and sets
GOOGLE_APPLICATION_CREDENTIALS, reusing the existing GCP_SA_KEY_PATH .env
variable — no second key-path variable invented. This parses the raw YAML
(no live Docker call, no `docker compose config` shell-out — a real check
still needs Docker available, which a plain `pytest` run should not
require) and asserts structure/interpolation-string shape, not a live
credential.
"""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE_PATH = Path(__file__).parent.parent.parent / "docker-compose.yml"


def _load_compose() -> dict:
    return yaml.safe_load(COMPOSE_PATH.read_text())


def test_worker_service_sets_google_application_credentials_from_gcp_sa_key_path() -> None:
    compose = _load_compose()
    worker = compose["services"]["worker"]
    env = worker["environment"]
    assert "GOOGLE_APPLICATION_CREDENTIALS" in env
    # Reuses GCP_SA_KEY_PATH (litellm's existing variable) — no second
    # key-path variable invented, and must NOT be a `:?` hard-require
    # (that would crash `docker compose up` with no GCP secret provisioned,
    # contradicting Task 1's "worker fully functional with no credentials"
    # requirement).
    assert "GCP_SA_KEY_PATH" in str(env["GOOGLE_APPLICATION_CREDENTIALS"])
    assert ":?" not in str(env["GOOGLE_APPLICATION_CREDENTIALS"])


def test_worker_service_mounts_the_gcp_sa_key_read_only() -> None:
    compose = _load_compose()
    worker = compose["services"]["worker"]
    volumes = worker["volumes"]
    sa_key_mounts = [v for v in volumes if "gcp-sa.json" in v]
    assert len(sa_key_mounts) == 1
    mount = sa_key_mounts[0]
    assert mount.endswith(":ro")
    assert "GCP_SA_KEY_PATH" in mount
    # Must default to something benign (never a `:?` hard-require) so a
    # fresh dev box/CI with no GCP secret provisioned can still boot the
    # worker (Task 1's "leaves the worker fully functional with NO
    # credentials configured" requirement).
    assert ":?" not in mount


def test_litellm_service_still_requires_the_gcp_sa_key_unchanged() -> None:
    # Regression guard: this task must not weaken litellm's own (correctly
    # mandatory) GCP_SA_KEY_PATH requirement while adding the worker's
    # optional one.
    compose = _load_compose()
    litellm = compose["services"]["litellm"]
    sa_key_mounts = [v for v in litellm["volumes"] if "gcp-sa.json" in v]
    assert len(sa_key_mounts) == 1
    assert ":?set in .env" in sa_key_mounts[0]
