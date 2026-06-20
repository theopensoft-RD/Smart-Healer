# fleet-healer

A guardrailed, **node-local self-healing agent** for an edge sensor fleet (RPi-class
nodes running radar/Modbus + camera + MQTT workers). It detects and remediates
recurring **in-node software/config faults**, escalates the rest (hardware/physical),
and emits a **compact structured event stream designed for AI-assisted diagnosis**.

Deployed as a single zipapp artifact (`healer.pyz`) driven by a `systemd` oneshot
timer (~60 s). Python standard library only — no third-party runtime deps.

## Why

Edge fleets hit the same failures over and over — a Modbus radar circuit stuck, a
stream/camera config drift, a stale media-server broadcast after a restart, redis
down, a service crash-loop, disk fill — each historically fixed **by hand over SSH**
(tribal knowledge / key-person dependency). `fleet-healer` automates that under hard
guardrails, and turns every action into a structured event an operator (or an
external AI agent) can analyse.

## Architecture (modular)

```
pat_fleet_healer/
├── config.py            all tunables + .env loading (one place)
├── context.py           Context = config + injected services (DI -> unit-testable)
├── core/                side-effect chokepoints: shell · systemd · net · journal ·
│                        state · events · log · escalate
├── healers/             one module per failure-family; Healer.run(ctx)
│   └── registry.py      ordered registry (dependency-first)
├── runner.py            the engine: build ctx, run registry, per-healer isolation
├── events_schema.py     the event manifest (decoder + cause/fix playbook)
└── tools/collect.py     build an AI diagnostic bundle
build.py                 -> healer.pyz (zipapp single artifact)
tests/                   56 unit tests (each healer isolated via a stub Context)
```

- **Graduated, least-invasive-first (L0→L5):** in-process fix → service restart →
  dependency restart → config repair → link recovery → reboot (an **edge watchdog**,
  *not* this agent) → escalate.
- **Guardrails (hard-won):** self-preservation (never reboots itself; never touches
  the overlay network) · startup-grace · rate-limit→escalate · backup-before-edit ·
  identity-gate · verify-after-act · liveness ≠ workload-metric.
- **Dependency injection:** healers receive a `Context` and call `ctx.<service>(...)`,
  so every healer is unit-tested in isolation with stub services — no global
  monkeypatching, no live side effects.

## Structured events for AI diagnosis

Every action/escalation is one compact JSONL atom — `{t, n, e, d?}` (epoch, node,
**code**, optional fields). Severity / description / **likely-cause / suggested-fix**
live once in the manifest (`events_schema.py`), not on every line. An external AI
agent reads the manifest and can decode + reason about every event with no prior
knowledge of the system.

```json
{"t":1718900837,"n":"NODE-01","e":"stream-republish.republish-no-rtmp-after-restart","d":{"ams":"media-01"}}
```

Compaction: codes not prose · severity from the manifest (not per line) · heartbeat
not every tick · gzip-rotate at rest. ~70-100 B/atom raw, ~10-15× under gzip.
`healer.pyz collect` packages the manifest + recent events + a live state snapshot
into one gzip'd, secret-redacted bundle for the agent.

## Coverage

**Auto-healed (node-local):** radar circuit stuck-open · stream/camera config drift
(re-point IP, force H.264) · **stale media-server broadcast after a restart** (clean
re-publish, fleet-staggered) · service crash · redis down · disk hygiene ·
monitoring-agent stopped.

**Escalate-only (needs a human):** sensor hardware dead · sensor relocated/re-addressed
(a candidate is reported, **never auto-repointed** — wrong device = wrong reading) ·
camera absent · WAN down · physical faults (connector water-ingress, cable strain).

**Honest gaps (not node-local):** monitoring blindness when no agent is installed ·
overlay-network proxy degradation · cross-layer faults that need a **central
reconciler** (server-side) rather than a node agent.

## Build / test / deploy

```bash
python3 build.py --check      # build healer.pyz + smoke-run it (DRY)
python3 tests/test_healers.py # 56 unit tests (no production impact)
```

Deploy: ship `healer.pyz`, point a `systemd` oneshot `ExecStart` at it, enable the
timer. The agent re-reads itself each tick; rollout is sequential + verifies, and is
trivially reverted (swap `ExecStart` back).

## Design lessons (verify-before-concluding)

The coverage map was refined by mid-deployment corrections, not assumptions: an
overlay-SSH proxy banner ≠ "unreachable"; `systemctl is-active`=`inactive` ≠
"stopped" (it also returns for a non-existent unit); a camera's midday flap ≠ thermal
(it was a PoE/LAN connection fault); a media server "up" with live HLS can still show
clients **offline** if its broadcast *status* went stale after a restart. Each became
a guardrail or an escalation rule rather than a confident-but-wrong auto-fix.

## License

MIT.
