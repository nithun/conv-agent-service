---
type: "Reference"
title: "Monitoring & alerting (liveness)"
---

# Monitoring & alerting (liveness)

Why this exists: the front door (`app`) fast-acks 200 even if the back-room
`worker` is dead — and a dead worker **silently drops every AI reply and every
child-distress escalation**. Detection therefore has two halves: one inside the
stack, one out-of-band (off the host). This is a hard go-live gate (ADR-014 BC-C).

## Half 1 — internal detection (BUILT, in this repo)

| Piece | Where | What |
|---|---|---|
| Worker heartbeat | `app/worker/heartbeat.py` | Redis key `tap_agent:worker:heartbeat`, TTL 15s, written once per drain loop |
| Readiness probe | `app/ingress/readiness.py` | `GET /health/ready` → 200 only if Redis pings **and** heartbeat is fresh; 503 says which failed |
| Container healthcheck | `docker-compose.yml` (`worker`) + `app/worker/healthcheck.py` | `docker compose ps` shows `Up … (healthy)` / `unhealthy` — a bare `Up` can't tell dead from alive |

Live-verified via a forced `docker compose stop worker` drill: 200 → 503
(`"heartbeat key missing"` after the 15s TTL) → 200 on restart.

**Limit:** all of this runs on the same host. If the host sleeps or dies, nothing
here can tell anyone — hence half 2.

## Half 2 — external dead-man's-switch (Grafana/Prometheus side LIVE; pusher NOT built)

Design: a canary on the trial host drives a periodic end-to-end check and, on each
PASS, pushes a "last success" timestamp **out** to a hosted monitoring stack. If the
host goes dark, the timestamp freezes and the alert fires — push semantics mean the
monitored box needs no inbound access.

```text
[trial host] canary --PASS--> POST https://push.xoyo.ad (Basic-Auth)
    metric: tap_agent_convai_canary_last_success_timestamp_seconds
    path:   /metrics/job/tap_agent_convai_canary/deployment/local-trial
        --> Pushgateway --> Prometheus (honor_labels) --> Alertmanager --> email devops@xoyo.ad
```

**Already live and verified on the monitoring side** (2026-07-29):
- Endpoint `https://push.xoyo.ad` (Traefik, LE cert, HTTP Basic-Auth; user `tap-canary`,
  password from the operator **out-of-band only** — never in any doc or chat).
- Prometheus alert **pair**: `TapConvaiCanaryStale` (`time() - max(metric) > 900`, for 1m)
  and `TapConvaiCanaryAbsent` (`absent()` for 15m — the no-data page). Test alert
  delivered to `devops@xoyo.ad`.

## ⚠️ What remains — owed by whoever operates this next (tracked as F-089)

1. **Pick the cadence/threshold pairing** (the monitoring side's §5 flag): a 20-min
   push cadence against a 15-min staleness threshold false-alerts every cycle.
   Recommended: a cheap liveness push every ~5–7 min (gated on `/health/ready` 200),
   keeping the 900s threshold; the full-conversation canary stays as a rarer deep check.
2. **Build the pusher** — `prometheus_client.push_to_gateway` + Basic-Auth, config-driven
   cadence, tests. A ready-made push snippet (python + curl) is in the READY handover doc.
3. **First push lands** → `TapConvaiCanaryAbsent` resolves (this doubles as path proof).
4. **Joint forced-worker-down drill** → alert email received = done.

Reference docs (in the handover package under `docs/`):
`HANDOVER-grafana-ops-convai-liveness-heartbeat-2026-07-29.md` (full design + config),
`HANDOVER-grafana-ops-push-endpoint-READY-2026-07-29.md` (live endpoint contract + snippets).

Related: [worker](worker.md) · [ingress](ingress.md) · [design-principles](design-principles.md#6-worker-liveness-is-explicit)
