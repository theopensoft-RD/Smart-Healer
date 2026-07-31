#!/usr/bin/env python3
"""Unit tests for pat-fleet-healer (modular). Each healer is exercised in
isolation with a stubbed Context (dependency injection -> NO global monkeypatch).
No production impact: every side-effecting service is a stub.

Run:  python3 tests/test_healers.py            (from the package root)
"""
import os
import sys
import time
import tempfile
import shutil

# import the package from the repo (parent of tests/)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# no real sleeps in tests (F17 verify, dependency bounce)
time.sleep = lambda *a, **k: None

from pat_fleet_healer.config import Config
from pat_fleet_healer.context import Context
from pat_fleet_healer.healers.dependency import DependencyHealer
from pat_fleet_healer.healers.service_liveness import ServiceLivenessHealer
from pat_fleet_healer.healers.radar_sensor import RadarSensorHealer
from pat_fleet_healer.healers.stream_camera import StreamCameraHealer
from pat_fleet_healer.healers.stream_republish import StreamRepublishHealer
from pat_fleet_healer.healers.beszel_agent import BeszelAgentHealer
from pat_fleet_healer.healers.connectivity import ConnectivityHealer
from pat_fleet_healer.healers.disk_hygiene import DiskHygieneHealer
from pat_fleet_healer.healers.registry import default_registry
from pat_fleet_healer import runner

R = []
def check(name, cond):
    R.append((name, bool(cond)))

_TMP = []
def mkctx(state=None, env=None, **over):
    """Build a Context with stub services. Returns (ctx, rec). rec records the
    side effects (restart/escalate/log/rate_hit + last saved state)."""
    cfg = Config(env_path=os.devnull, overrides={})
    cfg.device_id = "PAT-TEST-001"
    cfg.dry_run = False
    cfg.grace_s = 120
    cfg.state_dir = tempfile.mkdtemp(); _TMP.append(cfg.state_dir)
    for k, v in (env or {}).items():
        cfg.env[k] = v
    rec = {"restart": [], "escalate": [], "log": [], "rate_hit": [], "codec": [], "repoint": [],
           "events": [], "hb": [], "saved": None}
    _st = dict(state or {})
    def _save(name, d):
        rec["saved"] = dict(d); _st.clear(); _st.update(d)
    base = {
        "sh":            lambda c, timeout=15: (0, "", ""),
        "log":           lambda m: rec["log"].append(m),
        "svc_active":    lambda s: True,
        "unit_exists":   lambda s: True,
        "svc_age":       lambda s: 999,
        "restart":       lambda s: (rec["restart"].append(s) or True),
        "journal":       lambda u, n=20: "",
        "online_recent": lambda u, m=3: True,
        "tcp_up":        lambda *a, **k: True,
        "estab_1935":    lambda: 1,
        "scan_port":     lambda p: [],
        "rate_ok":       lambda n: True,
        "rate_hit":      lambda n: rec["rate_hit"].append(n),
        "escalate":      lambda h, v, ev=None: rec["escalate"].append((h, v)),
        "state_load":    lambda name: dict(_st),
        "state_save":    _save,
        "event":         lambda code, **f: rec["events"].append((code, f)),
        "heartbeat":     lambda **f: rec["hb"].append(f),
    }
    base.update(over)
    return Context(cfg, base), rec

def esc_has(rec, frag):
    return any(frag in v for _, v in rec["escalate"])

# ===========================================================================
# Ported behavioural tests (DI form)
# ===========================================================================

# T9 connectivity: WAN down -> escalate ONLY, never restart/reboot (netbird-safe)
ctx, rec = mkctx(tcp_up=lambda *a, **k: False)
ConnectivityHealer().run(ctx)
check("T9 connectivity wan-down -> escalate", esc_has(rec, "wan-down"))
check("T9 connectivity -> NO restart (netbird-safe)", len(rec["restart"]) == 0)

# T6 stream-camera: up, not pushing, scan finds NO camera -> escalate camera-absent
ctx, rec = mkctx(env={"RTSP_URL": "rtsp://admin:x@192.168.1.99:554/s"},
                 svc_active=lambda s: True, svc_age=lambda s: 999,
                 estab_1935=lambda: 0, tcp_up=lambda *a, **k: False)
h = StreamCameraHealer(); h._scan_554 = lambda ctx: []
h.run(ctx)
check("T6 camera-absent -> escalate", esc_has(rec, "camera-absent"))
check("T6 camera-absent -> NO restart", len(rec["restart"]) == 0)

# T5b stream-camera: scan finds exactly ONE camera -> repoint+codec+restart
ctx, rec = mkctx(env={"RTSP_URL": "rtsp://admin:x@192.168.1.99:554/s"},
                 svc_active=lambda s: True, svc_age=lambda s: 999,
                 estab_1935=lambda: 0, tcp_up=lambda *a, **k: False)
h = StreamCameraHealer()
h._scan_554 = lambda ctx: ["192.168.1.150"]
h._set_codec_h264 = lambda ctx, ip, cred: rec["codec"].append(ip)
h._repoint_cam = lambda ctx, ip: rec["repoint"].append(ip)
h.run(ctx)
check("T5b cam-drift -> repoint to scanned cam", rec["repoint"] == ["192.168.1.150"])
check("T5b cam-drift -> set H.264", rec["codec"] == ["192.168.1.150"])
check("T5b cam-drift -> restart stream", "pat-smart-stream" in rec["restart"])
check("T5b cam-drift -> NO false escalate", len(rec["escalate"]) == 0)

# ---------------------------------------------------------------------------
# T5c-T5h camera BRAND change at the SAME ip (Hikvision <-> Dahua).
# Real incident 2026-07-31 pit004: a Dahua replaced the Hikvision on the same ip.
# :554 stayed open -> the healer saw "camera fine" and only restarted the stream
# forever, because it never validated the RTSP *path*. ffmpeg 404'd for a full day.
# ---------------------------------------------------------------------------
def mkbroken(url, **over):
    """stream down (not pushing) but the camera ip IS reachable -> the brand-swap shape"""
    o = {"svc_active": lambda s: True, "svc_age": lambda s: 999,
         "estab_1935": lambda: 0, "tcp_up": lambda *a, **k: True}
    o.update(over)
    return mkctx(env={"RTSP_URL": "rtsp://admin:x@192.168.1.126:554" + url}, **o)

def mkcam(rec, brand, works=None):
    """works = the ONE path this camera really answers on. Everything else 404s,
    so the healer has to discover it by probing, not by trusting a table."""
    h = StreamCameraHealer()
    h._scan_554 = lambda ctx: ["192.168.1.126"]
    h._rtsp_ok = lambda ctx, url: bool(works) and url.endswith(works)
    h._detect_brand = lambda ctx, ip, cred: brand
    h._set_codec_h264 = lambda ctx, ip, cred: rec["codec"].append(ip)
    h._repoint_cam = lambda ctx, ip: rec["repoint"].append(ip)
    h._repoint_path = lambda ctx, p: rec["path"].append(p)
    return h

# T5c: Dahua now on the ip, .env still holds the old path -> repoint PATH, not ip
ctx, rec = mkbroken("/stream0"); rec["path"] = []
mkcam(rec, "dahua", works="/cam/realmonitor?channel=1&subtype=0").run(ctx)
check("T5c dahua brand-swap -> repoint RTSP path",
      rec["path"] == ["/cam/realmonitor?channel=1&subtype=0"])
check("T5c dahua brand-swap -> restart stream", "pat-smart-stream" in rec["restart"])
check("T5c dahua brand-swap -> ip untouched (only path wrong)", rec["repoint"] == [])
check("T5c dahua brand-swap -> NO camera-absent (camera IS there)",
      not esc_has(rec, "camera-absent"))

# T5d: Hikvision that answers only on its non-first candidate -> probe past the miss
ctx, rec = mkbroken("/cam/realmonitor?channel=1&subtype=0"); rec["path"] = []
mkcam(rec, "hikvision", works="/Streaming/Channels/101").run(ctx)
check("T5d hikvision brand-swap -> repoint to the path that answers",
      rec["path"] == ["/Streaming/Channels/101"])

# T5e: path already correct -> do NOT rewrite .env (no churn every 60s tick)
ctx, rec = mkbroken("/cam/realmonitor?channel=1&subtype=0"); rec["path"] = []
mkcam(rec, "dahua", works="/cam/realmonitor?channel=1&subtype=0").run(ctx)
check("T5e path already OK -> NO .env rewrite", rec["path"] == [])

# T5f: nothing answers on any candidate -> escalate, never write a guessed path
ctx, rec = mkbroken("/stream0"); rec["path"] = []
mkcam(rec, None, works=None).run(ctx)
check("T5f no working path -> escalate camera-path-unknown", esc_has(rec, "camera-path-unknown"))
check("T5f no working path -> NO blind path rewrite", rec["path"] == [])

# T5i REGRESSION GUARD: this fleet's Hikvisions are wired with /stream0 and it WORKS.
# A "canonical path" table would have rewritten 14 healthy nodes. Probe order must not
# matter - only what actually answers does.
ctx, rec = mkbroken("/broken"); rec["path"] = []
mkcam(rec, "hikvision", works="/stream0").run(ctx)
check("T5i hikvision keeps the fleet's working /stream0", rec["path"] == ["/stream0"])

# T5j: unknown brand but a candidate DOES answer -> repair anyway, don't call a human out
ctx, rec = mkbroken("/broken"); rec["path"] = []
mkcam(rec, None, works="/cam/realmonitor?channel=1&subtype=0").run(ctx)
check("T5j unknown brand + working candidate -> self-heal, no escalate",
      rec["path"] == ["/cam/realmonitor?channel=1&subtype=0"] and not esc_has(rec, "camera-path-unknown"))

# T5g: brand detection reads the real probe endpoints (not hardcoded to ISAPI)
h = StreamCameraHealer()
_calls = []
ctx, rec = mkbroken("/stream0", sh=lambda c, timeout=15: (
    _calls.append(c) or ((0, "type=DH-IPC-HDW1439V-A-IL", "") if "magicBox" in c else (0, "404 Not Found", ""))))
check("T5g detect dahua via magicBox.cgi", h._detect_brand(ctx, "192.168.1.126", "admin:x") == "dahua")
ctx, rec = mkbroken("/stream0", sh=lambda c, timeout=15: (
    (0, "<DeviceInfo><model>DS-2CD</model></DeviceInfo>", "") if "ISAPI" in c else (0, "", "")))
check("T5g detect hikvision via ISAPI", h._detect_brand(ctx, "192.168.1.126", "admin:x") == "hikvision")

# T5h: H.264 enforcement must use the DAHUA api on a dahua cam (was ISAPI-only -> silent no-op)
def dahua_cam(rec, after):
    """after = what getConfig reports AFTER the setConfig attempt"""
    seen = {"set": False}
    def _sh(c, timeout=15):
        rec["sent"].append(c)
        if "magicBox" in c:
            return (0, "type=DH-IPC", "")
        if "setConfig" in c:
            seen["set"] = True
            return (0, "OK", "")
        if "getConfig" in c:
            return (0, after if seen["set"] else
                    "table.Encode[0].MainFormat[0].Video.Compression=H.265", "")
        return (0, "", "")
    return _sh

ctx, rec = mkbroken("/stream0"); rec["sent"] = []
ctx, rec2 = mkbroken("/stream0", sh=dahua_cam(rec, "table.Encode[0].MainFormat[0].Video.Compression=H.264"))
StreamCameraHealer()._set_codec_h264(ctx, "192.168.1.126", "admin:x")
check("T5h dahua codec -> uses configManager.cgi (not ISAPI)",
      any("configManager.cgi" in c and "H.264" in c for c in rec["sent"]))
# T5k: the brackets MUST be percent-encoded. Raw 'Encode[0]' -> camera rejects with an
# EMPTY body and the old code logged success anyway (pit004 streamed black for a day).
check("T5k dahua setConfig percent-encodes the brackets",
      any("Encode%5B0%5D.MainFormat%5B0%5D" in c for c in rec["sent"])
      and not any("Encode[0].MainFormat[0]" in c for c in rec["sent"]))
check("T5k verified success is logged only after read-back",
      any("verified" in m for m in rec2["log"]))

# T5l: camera refuses H.264 -> say so. NEVER log a success we did not observe.
ctx, rec = mkbroken("/stream0"); rec["sent"] = []
ctx, rec3 = mkbroken("/stream0", sh=dahua_cam(rec, "table.Encode[0].MainFormat[0].Video.Compression=H.265"))
StreamCameraHealer()._set_codec_h264(ctx, "192.168.1.126", "admin:x")
check("T5l dahua refuses H.264 -> logged as REFUSED, not success",
      any("REFUSED" in m for m in rec3["log"]) and not any("verified" in m for m in rec3["log"]))

# T4 radar: circuit stuck + recoverable -> restart
ctx, rec = mkctx(journal=lambda u, n=20: "[radar] read error (circuit=open): TimeoutError",
                 online_recent=lambda u, m=3: False, tcp_up=lambda *a, **k: True)
RadarSensorHealer().run(ctx)
check("T4 radar stuck (recoverable) -> restart radar", "pat-smart-radar" in rec["restart"])

# T4 radar: rate exceeded -> escalate, NOT restart (genuine hw)
ctx, rec = mkctx(journal=lambda u, n=20: "circuit=open", online_recent=lambda u, m=3: False,
                 tcp_up=lambda *a, **k: True, rate_ok=lambda n: False)
RadarSensorHealer().run(ctx)
check("T4 radar rate-exceeded -> escalate not restart", len(rec["restart"]) == 0 and len(rec["escalate"]) >= 1)

# T4b radar: ONLINE recently -> no action
ctx, rec = mkctx(journal=lambda u, n=20: "STARTING -> ONLINE", online_recent=lambda u, m=3: True)
RadarSensorHealer().run(ctx)
check("T4b radar healthy -> no restart no escalate", len(rec["restart"]) == 0 and len(rec["escalate"]) == 0)

# T8 startup grace: service young -> skip even if down
ctx, rec = mkctx(svc_active=lambda s: False, svc_age=lambda s: 10)
ServiceLivenessHealer().run(ctx)
check("T8 startup-grace -> skip restart on young service", len(rec["restart"]) == 0)

# T8b liveness: down + old -> restart
ctx, rec = mkctx(svc_active=lambda s: (s != "pat-smart-stream"), svc_age=lambda s: 999)
ServiceLivenessHealer().run(ctx)
check("T8b liveness down+old -> restart stream", "pat-smart-stream" in rec["restart"])

# T-sensormove: configured sensor gone + ONE new :502 -> escalate sensor-moved (NO restart/repoint)
ctx, rec = mkctx(journal=lambda u, n=20: "circuit=open", online_recent=lambda u, m=3: False,
                 tcp_up=lambda *a, **k: False, scan_port=lambda p: ["192.168.1.107"])
ctx.cfg.modbus_host = "192.168.1.106"
RadarSensorHealer().run(ctx)
check("T-sensormove -> escalate sensor-moved", esc_has(rec, "sensor-moved"))
check("T-sensormove -> NO restart (safety-critical)", len(rec["restart"]) == 0)

# T-sensorabsent: configured sensor gone + nothing on :502 -> escalate sensor-absent
ctx, rec = mkctx(journal=lambda u, n=20: "circuit=open", online_recent=lambda u, m=3: False,
                 tcp_up=lambda *a, **k: False, scan_port=lambda p: [])
ctx.cfg.modbus_host = "192.168.1.106"
RadarSensorHealer().run(ctx)
check("T-sensorabsent -> escalate sensor-absent", esc_has(rec, "sensor-absent"))

# T-beszel: inactive + unit present -> restart
ctx, rec = mkctx(unit_exists=lambda s: True, svc_active=lambda s: False, svc_age=lambda s: 999)
BeszelAgentHealer().run(ctx)
check("T-beszel inactive -> restart", "beszel-agent" in rec["restart"])

# T-beszel: active -> no action
ctx, rec = mkctx(unit_exists=lambda s: True, svc_active=lambda s: True)
BeszelAgentHealer().run(ctx)
check("T-beszel active -> no restart", len(rec["restart"]) == 0)

# T-beszel: unit absent -> no action
ctx, rec = mkctx(unit_exists=lambda s: False, svc_active=lambda s: False, svc_age=lambda s: 999)
BeszelAgentHealer().run(ctx)
check("T-beszel unit absent -> no action", len(rec["restart"]) == 0 and len(rec["escalate"]) == 0)

# T-hygiene: purge old, keep recent (real temp files)
ctx, rec = mkctx()
_d = tempfile.mkdtemp()
_old = os.path.join(_d, "x_20200101.log"); open(_old, "w").write("x")
_new = os.path.join(_d, "x_today.log"); open(_new, "w").write("x")
os.utime(_old, (time.time() - 999 * 86400, time.time() - 999 * 86400))
ctx.cfg.log_dir = _d; ctx.cfg.workers_dir = _d
DiskHygieneHealer().run(ctx)
check("T-hygiene old log -> purged", not os.path.exists(_old))
check("T-hygiene recent log -> kept", os.path.exists(_new))
shutil.rmtree(_d, ignore_errors=True)

# ===========================================================================
# F17 stream-republish (the new healer) - every trigger + guardrail
# ===========================================================================
ENVF = {"RTMP_URL": "rtmp://ams.test/CCTVApp/CCTV-X"}

# F17-a: AMS down -> count down_ticks, NO restart (node side is fine)
ctx, rec = mkctx(env=ENVF, tcp_up=lambda *a, **k: False)
StreamRepublishHealer().run(ctx)
check("F17-a AMS down -> NO restart", len(rec["restart"]) == 0)
check("F17-a AMS down -> down_ticks counted", rec["saved"] and rec["saved"].get("down_ticks") == 1)

# F17-b: bounce (down_ticks>=confirm, now up) but ams_back just now -> queue, NOT fire (settle/jitter)
ctx, rec = mkctx(env=ENVF, state={"down_ticks": 3})
StreamRepublishHealer().run(ctx)
check("F17-b bounce -> queued pending", rec["saved"] and rec["saved"].get("pending") is True)
check("F17-b bounce -> NOT fired yet (settle)", len(rec["restart"]) == 0)

# F17-c: pending + pushing + settle/jitter elapsed -> FIRE clean re-publish
ctx, rec = mkctx(env=ENVF, state={"pending": True, "ams_back_ts": time.time() - 10000})
ctx.cfg.republish_spread_s = 0
StreamRepublishHealer().run(ctx)
check("F17-c pending+elapsed -> restart stream", "pat-smart-stream" in rec["restart"])
check("F17-c -> pending cleared after fire", rec["saved"] and rec["saved"].get("pending") is False)

# F17-d: deploy sentinel present -> queue one-time re-publish + consume sentinel
ctx, rec = mkctx(env=ENVF)
sent = os.path.join(ctx.cfg.state_dir, "republish-once"); open(sent, "w").write("")
StreamRepublishHealer().run(ctx)
check("F17-d sentinel -> queued pending", rec["saved"] and rec["saved"].get("pending") is True)
check("F17-d sentinel -> consumed (removed)", not os.path.exists(sent))

# F17-e: pending but NOT pushing (estab=0) -> clear pending, NO restart (camera-healer handles)
ctx, rec = mkctx(env=ENVF, estab_1935=lambda: 0,
                 state={"pending": True, "ams_back_ts": time.time() - 10000})
ctx.cfg.republish_spread_s = 0
StreamRepublishHealer().run(ctx)
check("F17-e pending+not-pushing -> clear, NO restart", len(rec["restart"]) == 0 and rec["saved"].get("pending") is False)

# F17-f: pending+elapsed but rate exceeded -> escalate, NO restart
ctx, rec = mkctx(env=ENVF, rate_ok=lambda n: False,
                 state={"pending": True, "ams_back_ts": time.time() - 10000})
ctx.cfg.republish_spread_s = 0
StreamRepublishHealer().run(ctx)
check("F17-f rate-exceeded -> escalate not restart",
      esc_has(rec, "republish-rate-exceeded") and len(rec["restart"]) == 0)

# F17-g: not a streaming node (stream inactive) -> no action
ctx, rec = mkctx(env=ENVF, svc_active=lambda s: False)
StreamRepublishHealer().run(ctx)
check("F17-g stream inactive -> no action", len(rec["restart"]) == 0 and len(rec["escalate"]) == 0)

# F17-h: no RTMP_URL -> no action (not a CCTV node)
ctx, rec = mkctx()
StreamRepublishHealer().run(ctx)
check("F17-h no RTMP target -> no action", len(rec["restart"]) == 0)

# F17-i: startup grace -> queued but NOT fired (don't mistake own boot for a bounce)
ctx, rec = mkctx(env=ENVF, svc_age=lambda s: 5,
                 state={"pending": True, "ams_back_ts": time.time() - 10000})
ctx.cfg.republish_spread_s = 0
StreamRepublishHealer().run(ctx)
check("F17-i grace -> NOT fired", len(rec["restart"]) == 0)

# F17-j: per-node jitter is deterministic + differs by device -> staggers the fleet
off1 = StreamRepublishHealer._stable_offset("PAT-AAA", 150)
off2 = StreamRepublishHealer._stable_offset("PAT-BBB", 150)
off1b = StreamRepublishHealer._stable_offset("PAT-AAA", 150)
check("F17-j jitter stable per node", off1 == off1b and 0 <= off1 < 150)
check("F17-j jitter differs by node (staggered)", off1 != off2)

# ===========================================================================
# Structural: runner isolation + registry
# ===========================================================================

# T-runner: a healer that raises must NOT stop the others; tick still completes
class Boom:
    name = "boom"
    def run(self, ctx): raise RuntimeError("kaboom")
class Ok:
    name = "ok"
    def run(self, ctx): ctx.log("ok-ran")
ctx, rec = mkctx()
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[Boom(), Ok()])
check("T-runner isolates raising healer -> agent.exc", any(c == "agent.exc" and f.get("healer") == "boom" for c, f in rec["events"]))
check("T-runner continues after exception", any("ok-ran" in m for m in rec["log"]))
check("T-runner tick completes -> heartbeat", len(rec["hb"]) >= 1)

# T-runner: no DEVICE_ID -> abort event, never act on an unidentified node
ctx, rec = mkctx(); ctx.cfg.device_id = ""
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[Ok()])
check("T-runner no-DEVICE_ID -> agent.abort, no run",
      any(c == "agent.abort" for c, _ in rec["events"]) and not any("ok-ran" in m for m in rec["log"]))

# T-registry: 8 healers, dependency-first order, unique names, F17 after stream-camera
reg = default_registry()
names = [h.name for h in reg]
check("T-registry has 8 healers", len(reg) == 8)
check("T-registry names unique", len(set(names)) == len(names))
check("T-registry dependency first", names[0] == "dependency")
check("T-registry F17 after stream-camera", names.index("stream-republish") == names.index("stream") + 1)

# ===========================================================================
# Event system (real emit / manifest / bundle) - the AI-diagnosis logging layer
# ===========================================================================
import gzip
import json as _json
from pat_fleet_healer.core import events as EV
from pat_fleet_healer import events_schema as SCH
from pat_fleet_healer.tools import collect as COLLECT
from pat_fleet_healer.context import production_context

def real_cfg():
    c = Config(env_path=os.devnull, overrides={})
    c.device_id = "PAT-EVT-001"; c.dry_run = False
    c.state_dir = tempfile.mkdtemp(); _TMP.append(c.state_dir)
    return c

# E1: emit writes a compact JSONL atom {t,n,e,d}
c = real_cfg()
EV.emit(c, "f17.republish.ok", {"ams": "am01", "estab": 1})
_lines = open(os.path.join(c.state_dir, "events.jsonl")).read().splitlines()
_atom = _json.loads(_lines[-1])
check("E1 atom has t,n,e + code", {"t", "n", "e"}.issubset(_atom) and _atom["e"] == "f17.republish.ok")
check("E1 atom carries fields in d", _atom["d"]["ams"] == "am01")
check("E1 atom compact (<120B)", len(_lines[-1]) < 120)

# E2: manifest covers ALL escalation codes the healers can emit (catches a missing decoder entry)
_expected = ["dependency.redis-down-rate-exceeded", "dependency.redis-restart-failed",
    "liveness.svc-crash-loop", "liveness.svc-restart-failed",
    "radar.vegamet-fault-or-stuck", "radar.sensor-moved", "radar.sensor-absent", "radar.sensor-ambiguous",
    "stream.stream-repair-rate-exceeded", "stream.camera-absent", "stream.camera-ambiguous",
    "stream.camera-path-unknown",
    "stream-republish.republish-rate-exceeded", "stream-republish.republish-restart-failed",
    "stream-republish.republish-no-rtmp-after-restart",
    "beszel.beszel-agent-restart-rate-exceeded", "beszel.beszel-agent-restart-failed",
    "connectivity.wan-down-detect-only"]
_missing = [x for x in _expected if x not in SCH.CODES]
check("E2 manifest covers all escalation codes", not _missing)
check("E2 every code has sev+desc+cause+fix (AI playbook)",
      all(all(k in SCH.CODES[x] for k in ("sev", "desc", "cause", "fix")) for x in SCH.CODES))

# E3: severity comes from the manifest (not duplicated on the line)
check("E3 escalation sev resolvable", SCH.CODES["radar.sensor-absent"]["sev"] in ("error", "warn"))

# E4: heartbeat is rate-limited (2nd call within window -> no 2nd atom)
c = real_cfg(); c.heartbeat_s = 9999
EV.heartbeat(c); EV.heartbeat(c)
_hb = [l for l in open(os.path.join(c.state_dir, "events.jsonl")).read().splitlines() if "agent.alive" in l]
check("E4 heartbeat rate-limited (1 not 2)", len(_hb) == 1)

# E5: escalate wiring -> coded event with the ':svc' rate-suffix stripped
c = real_cfg(); ctxp = production_context(c)
ctxp.escalate("liveness:pat-smart-stream", "svc-crash-loop", {"svc": "pat-smart-stream"})
_last = _json.loads(open(os.path.join(c.state_dir, "events.jsonl")).read().splitlines()[-1])
check("E5 escalate -> coded event (':svc' stripped)", _last["e"] == "liveness.svc-crash-loop")

# E6: collect bundle = gzip, self-contained (manifest+state+events), secrets redacted
c = real_cfg()
EV.emit(c, "agent.log", {"msg": "ok"})
EV.emit(c, "agent.log", {"password": "hunter2"})        # a stray secret must NOT leave in the bundle
_out, _raw, _comp = COLLECT.build_bundle(c)
_b = _json.loads(gzip.open(_out).read().decode())
check("E6 bundle self-contained (manifest+state+events)",
      "manifest" in _b and "state" in _b and "events_jsonl" in _b)
check("E6 bundle manifest decodes codes", "radar.sensor-moved" in _b["manifest"]["codes"])
check("E6 bundle redacts secrets", "hunter2" not in gzip.open(_out).read().decode())
check("E6 bundle compresses", _comp <= _raw)

# ---------------------------------------------------------------------------
for d in _TMP:
    shutil.rmtree(d, ignore_errors=True)
print("=== UNIT TEST RESULTS (modular) ===")
for n, ok in R:
    print("%-58s %s" % (n, "PASS" if ok else "FAIL"))
print("TOTAL: %d/%d PASS" % (sum(1 for _, ok in R if ok), len(R)))
sys.exit(0 if all(ok for _, ok in R) else 1)
