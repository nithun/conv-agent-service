"""F-044 / ADR-012 BC-2 config test: the exposure design (which ports are
published on which interface) must be VERSIONED and MECHANICALLY enforced,
not left to an untracked one-off file rebinding ports on the live VM (the
exact gap BC-2 found — see docs/adrs/ADR-012-trial-gcp-vm-deployment-
topology.md in the parent workspace).

Two files together bound the sensitive internal services (Redis 6379,
self-hosted Langfuse 3001->3000, LiteLLM gateway 4001->4000) to
`127.0.0.1`-only:

- `docker-compose.yml` (base) publishes NO host port at all for those three
  services -- app/worker reach them over the compose network by service
  name (REDIS_URL=redis://redis:6379, LANGFUSE_HOST=http://langfuse:3000,
  OPENROUTER_BASE_URL=http://litellm:4000). This is the fail-CLOSED
  default: a deploy that somehow runs without the override publishes
  nothing on those ports rather than exposing them on 0.0.0.0 (what BC-2
  found the tracked file did before this change).
- `docker-compose.override.yml` (tracked; auto-loaded by `docker compose
  up` with no extra `-f` flag per Compose's documented override
  convention) is the SOLE source of any host publish for those services,
  and binds each one to `127.0.0.1` explicitly.

The app/webhook service (host port 8001) is deliberately EXCLUDED from
this lockdown -- it is the public Glific webhook target, fronted by
Caddy/TLS on the VM, and must stay reachable from outside the loopback.

This parses the raw YAML directly (no live Docker call, no `docker compose
config` shell-out) -- matching `test_docker_compose_worker_credentials.py`'s
convention: a real check still needs Docker available, which a plain
`pytest` run should not require. The merge-arithmetic claim below (base
declares no `ports:` for these services, so the override's binding is the
ONLY one in effect) was verified live this session with
`docker compose config` before this test was written -- Compose
concatenates `ports:` lists across files rather than replacing them, so a
naive override that left the base file's `ports:` in place would have
published BOTH 0.0.0.0 and 127.0.0.1 bindings simultaneously. Stripping the
base file's `ports:` for these services is what actually closes the gap.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
OVERRIDE_PATH = REPO_ROOT / "docker-compose.override.yml"

# service -> (host_port, container_port) expected in the loopback-only
# override binding.
SENSITIVE_SERVICES: dict[str, tuple[str, str]] = {
    "redis": ("6379", "6379"),
    "langfuse": ("3001", "3000"),
    "litellm": ("4001", "4000"),
}

PUBLIC_SERVICE = "app"
PUBLIC_HOST_PORT = "8001"


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _load_base_compose() -> dict:
    return _load_yaml(COMPOSE_PATH)


def _load_override_compose() -> dict:
    return _load_yaml(OVERRIDE_PATH)


def test_override_file_exists() -> None:
    """The whole point of BC-2 is that this control must be a TRACKED
    artifact, not an untracked one-off on the live VM. This test only
    checks existence-on-disk of the well-known path -- it cannot itself
    prove git-tracked-ness, because `.dockerignore` excludes `.git` from
    this repo's build context (so a `git ls-files` shell-out would fail
    inside the very `dev` container this test suite runs in, regardless of
    whether the file is actually committed). Git-tracked-ness is confirmed
    at commit time instead (`git status`/`git ls-files`, same commit as
    this file) -- see the commit that introduced this test.
    """
    assert OVERRIDE_PATH.exists(), (
        "docker-compose.override.yml must exist -- it is the sole source of "
        "any host port publish for redis/langfuse/litellm (F-044 / ADR-012 BC-2)"
    )


def test_base_compose_publishes_no_host_port_for_sensitive_services() -> None:
    """Regression guard for the fail-closed default: the base file must
    declare no `ports:` key at all for redis/langfuse/litellm, so a deploy
    that runs `docker compose -f docker-compose.yml up` WITHOUT the
    override publishes nothing on these ports rather than falling back to
    an implicit 0.0.0.0 bind (the exact regression this whole task closes).
    """
    compose = _load_base_compose()
    for service in SENSITIVE_SERVICES:
        svc = compose["services"][service]
        assert "ports" not in svc, (
            f"{service!r} must not publish a host port in the BASE compose "
            "file -- docker-compose.override.yml is the sole (loopback-only) "
            "source of any host publish for this service (F-044 / ADR-012 BC-2)"
        )


def test_base_compose_still_publishes_the_public_webhook_port() -> None:
    """Confirms this task did NOT lock down the app/webhook port -- 8001
    must stay published and reachable (fronted by Caddy/TLS on the VM; the
    public Glific webhook target).
    """
    compose = _load_base_compose()
    app = compose["services"][PUBLIC_SERVICE]
    assert "ports" in app, "app service must still publish its host port"
    ports = app["ports"]
    assert len(ports) == 1
    published = str(ports[0])
    assert published.startswith(f"{PUBLIC_HOST_PORT}:"), (
        f"app service port mapping changed unexpectedly: {published!r}"
    )
    # Must NOT be loopback-bound -- explicitly excluded from this task's
    # lockdown per its own instructions.
    assert "127.0.0.1" not in published


def test_override_binds_every_sensitive_service_to_loopback_only() -> None:
    override = _load_override_compose()
    for service, (host_port, container_port) in SENSITIVE_SERVICES.items():
        assert service in override["services"], (
            f"docker-compose.override.yml must declare a {service!r} service block"
        )
        svc = override["services"][service]
        ports = svc.get("ports")
        assert ports, f"{service!r} override block must publish a port"
        assert len(ports) == 1, (
            f"{service!r} override should bind exactly one port mapping, got {ports!r}"
        )
        mapping = str(ports[0])
        expected = f"127.0.0.1:{host_port}:{container_port}"
        assert mapping == expected, (
            f"{service!r} port mapping must be loopback-bound: expected "
            f"{expected!r}, got {mapping!r}"
        )


def test_override_never_publishes_a_sensitive_port_on_all_interfaces() -> None:
    """Mechanical GL-005 guard (not just a comment): fails if ANY sensitive
    service's port mapping in the override is missing the `127.0.0.1:`
    host-ip prefix -- i.e. would bind 0.0.0.0/all-interfaces. This is the
    check that must never silently regress if someone "simplifies" the
    override later.
    """
    override = _load_override_compose()
    for service in SENSITIVE_SERVICES:
        svc = override["services"][service]
        for mapping in svc.get("ports", []):
            mapping_str = str(mapping)
            assert mapping_str.startswith("127.0.0.1:"), (
                f"{service!r} publishes {mapping_str!r} -- not loopback-bound. "
                "Every sensitive internal service's port mapping in "
                "docker-compose.override.yml MUST start with '127.0.0.1:' "
                "(F-044 / ADR-012 BC-2)."
            )


def test_override_does_not_touch_the_public_webhook_service() -> None:
    """The override must not add an `app` (or `worker`) block that could
    accidentally narrow or otherwise change the public webhook port -- this
    task's scope is the three internal services only.
    """
    override = _load_override_compose()
    assert PUBLIC_SERVICE not in override["services"], (
        "docker-compose.override.yml must not override the app service -- "
        "the public webhook port (8001) must be left untouched"
    )
    assert "worker" not in override["services"]


def test_override_service_names_are_a_subset_of_base_compose_services() -> None:
    """Typo/drift guard: every service the override touches must exist in
    the base file under the exact same name.
    """
    base_services = set(_load_base_compose()["services"])
    override_services = set(_load_override_compose()["services"])
    assert override_services <= base_services, (
        f"override declares services not present in the base compose file: "
        f"{override_services - base_services}"
    )
    assert override_services == set(SENSITIVE_SERVICES)


def test_effective_merged_ports_are_loopback_only_for_sensitive_services() -> None:
    """End-to-end assertion of what actually happens when Compose merges
    both files: since the base file declares NO `ports:` key for these
    services (asserted above) and Compose's merge for `ports:` is list
    CONCATENATION (verified live via `docker compose config` this session
    -- NOT replacement), the effective published-port list for each
    sensitive service is exactly the override's loopback-bound entry, with
    no 0.0.0.0 entry surviving from the base file.
    """
    base = _load_base_compose()
    override = _load_override_compose()
    for service, (host_port, container_port) in SENSITIVE_SERVICES.items():
        base_ports = base["services"][service].get("ports", [])
        override_ports = override["services"][service]["ports"]
        effective = base_ports + override_ports
        assert effective == [f"127.0.0.1:{host_port}:{container_port}"], (
            f"effective merged ports for {service!r} would be {effective!r}, "
            "expected exactly one loopback-bound entry"
        )
