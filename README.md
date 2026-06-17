# fleet-healer — Node-Local Self-Healing Agent

> A guardrailed, **node-local self-healing agent** for an edge sensor fleet (RPi-class nodes running radar/Modbus + camera + MQTT workers). It detects and remediates recurring **in-node software/config faults**, and **escalates** the rest (hardware/physical) to the operator's existing monitoring/alerting.

---

## 1. Background

An edge fleet of sensor nodes hits the same failures over and over — Modbus radar circuit stuck, stream/camera config drift, redis crash, service crash-loops, disk fill — each historically fixed **by hand over SSH** (tribal knowledge / key-person dependency). `fleet-healer` automates that: nodes self-recover the recurring software/config faults under hard guardrails, and escalate the hardware/physical ones so a human only gets involved when a human is actually required.

## 2. Architecture

- **Graduated · least-invasive-first (L0→L5):** L0 in-process fix → L1 service restart → L2 dependency restart → L3 config repair → L4 link recovery → L5 reboot (**performed by an edge hardware watchdog, NOT this agent**) → escalate.
- **Runtime:** a `systemd` oneshot timer (~60 s) that re-reads the agent each tick.
- **10 guardrails (hard-won):** self-preservation (never reboots itself; never touches the overlay network) · startup-grace · rate-limit→escalate · backup-before-edit · identity-gate · no-fake · idempotent · liveness ≠ workload-metric · verify-after-act · network-safe.

## 3. Version history

| version | milestone |
|---|---|
| 0.1 | Design — FMEA failure-mode catalog + framework spec; graduated ladder + guardrails |
| 0.2 | **P0 root fix** — Modbus radar circuit-breaker half-open (kills the #1 recurring fault in-process); first single-node trial |
| 0.3 | Node-local agent (5 healers: dependency, service-liveness, radar-sensor, stream-camera, connectivity) + systemd timer + narrow NOPASSWD sudoers |
| 0.4 | MQTT escalation; full test suite (unit + live induced-fault); second node |
| 0.5 | +`disk-hygiene` healer (log/backup rotation) |
| 0.6 | +monitoring-agent healer · +sensor-relocation escalation · env-var bugfix · **7 healers · 22 unit tests** · env-path test hook |
| 0.6 (rollout) | Fleet-wide deploy — agent-only, idempotent, no stream restart |

## 4. Coverage

### ✅ Auto-healed (node-local)
| Failure mode | Detection (read-only) | Action |
|---|---|---|
| radar circuit stuck-open | radar FAULT · `circuit=open` · no recent ONLINE | L0 in-proc half-open + L1 restart |
| stream / camera config | stream not pushing · LAN `:554` scan · journal | L3 — re-point camera IP · force H.264 · quote config value · restart |
| service crash / inactive | `systemctl is-active` (+ startup-grace) | L1 reset-failed + restart |
| redis dependency down | redis ping | L2 restart redis + dependents |
| disk / log growth | strict pattern + mtime | purge rotated logs/backups past retention |
| monitoring-agent stopped | service inactive (unit present) | L1 restart |

### 🟡 Escalate-only (cannot auto-fix → operator alerting → technician)
sensor hardware dead · camera absent · **sensor relocated / re-addressed** (scan, escalate a *candidate* — deliberately **not** auto-repointed: pointing at the wrong Modbus device = wrong reading = safety-critical) · backend ID placeholder · ingest/census mismatch · WAN down (+ router self-reboot) · node hardware-dead (no out-of-band on cellular CGNAT → on-site).

### 🔵 Handled elsewhere (not this agent)
- **thermal / transcode** → deploy-time encoder config (the agent forces the camera to H.264 to reduce transcode, but does not set the encoder mode itself)
- **node software-hang** → `systemd` RuntimeWatchdog (edge)
- **human alerting** → the operator's existing dashboard + chat alerting (the agent's `escalate()` is a supplementary MQTT/log channel)

## 5. What it does NOT cover (honest gaps)

| Gap | Why |
|---|---|
| monitoring-agent **blindness** beyond "stopped service" | the healer only restarts a *stopped* agent service; nodes with **no agent installed** (need install) or an agent that **cannot reach the hub** (network-degraded) are not auto-fixed |
| **overlay-network version / proxy degradation** | nodes on an older overlay-SSH proxy intermittently lose admin SSH + monitoring reachability; the connectivity healer only checks WAN ping, not overlay/hub reach |
| **camera connection flap / down** | root cause is **physical** — water ingress in the camera PoE connector, or mechanical strain on the LAN cable → connection flap/loss; the agent escalates but cannot repair (needs on-site re-seat/waterproof + strain-relief) |

## 6. Deploy & test

- **Deploy** (agent-only, no stream restart): backup → `py_compile`-gated → idempotent sudoers → verify. Fleet rollout = sequential, raw-SSH-first, per-node-type key.
- **Test:** `test_healer.py` (import + monkeypatch — **no production impact**) + `live-test.sh` (induce real faults → verify → restore).

## 7. Design lessons (verify-before-concluding)

The coverage map was refined by **mid-deployment corrections**, not assumptions:

- an overlay-SSH proxy banner ≠ "unreachable" (it auths silently via a cached token);
- "older proxy version" ≠ "admin-broken" (only *some* nodes' backend hop fails);
- `systemctl is-active`=`inactive` ≠ "stopped" (it also returns `inactive` for a **non-existent** unit);
- a camera's midday flap ≠ thermal (it was a **PoE/LAN connection** fault);
- "silent escalation" ≠ "no alerting" (an external dashboard/alerting already existed).

Each became a **guardrail or an escalation rule** rather than a confident-but-wrong auto-fix.
