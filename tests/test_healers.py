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
from pat_fleet_healer.healers.net_probe import NetProbeHealer
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
        "state_writable": lambda: True,
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
# F10e ec25 uplink recovery (IRIV internal Quectel EC25) - the modem-reset path
# The ONLY healer that actively repairs the uplink. It has no external watchdog
# to fall back on, and it runs on nodes that are painful to reach physically, so
# every guard (confirm / settle / rate / dry-run / rc) is pinned here.
# ===========================================================================

def sh_stub(log, rules=None, rc=0, out="", err=""):
    """Recording shell stub. rules = [(fragment, (rc, out, err)), ...] matched in order."""
    def _sh(c, timeout=15):
        log.append(c)
        for frag, ret in (rules or []):
            if frag in c:
                return ret
        return (rc, out, err)
    return _sh

def issued_reset(log):
    return [c for c in log if "--reset" in c]

DOWN = {"tcp_up": lambda *a, **k: False}       # WAN down on both probes

# F10e-a: first down-tick -> count only, NO reset (a flap is not an outage)
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), **DOWN); ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-a ec25 tick1 -> NO modem reset (confirm guard)", issued_reset(_L) == [])
check("F10e-a ec25 tick1 -> down counted =1", rec["saved"] and rec["saved"].get("down") == 1)
check("F10e-a ec25 tick1 -> NO premature escalate", len(rec["escalate"]) == 0)

# F10e-b: down reaches WAN_DOWN_CONFIRM -> reset the MODEM (exact command), arm the settle window
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 2}, **DOWN); ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-b confirmed outage -> resets modem via mmcli",
      issued_reset(_L) == ["sudo -n /usr/bin/mmcli -m any --reset"])
check("F10e-b reset -> rate accounted (cannot reset-loop)", rec["rate_hit"] == ["connectivity"])
check("F10e-b reset -> settle window armed (reset_ts set)", rec["saved"] and rec["saved"].get("reset_ts"))
# safety invariant: repairing the uplink must never reboot the node or touch netbird
check("F10e-b SAFETY no service restart", len(rec["restart"]) == 0)
check("F10e-b SAFETY no reboot / no netbird in any command",
      not any(("reboot" in c or "netbird" in c) for c in _L))

# F10e-c: inside the settle window -> wait, do NOT reset again
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 5, "reset_ts": time.time()}, **DOWN)
ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-c within settle -> NO second reset", issued_reset(_L) == [])
check("F10e-c within settle -> no escalate yet", len(rec["escalate"]) == 0)

# F10e-d: reset + settle elapsed + STILL down -> not a soft wedge; escalate to a human
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 5, "reset_ts": time.time() - 9999}, **DOWN)
ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-d reset didn't help -> escalate ec25-reset-no-recovery",
      esc_has(rec, "ec25-reset-no-recovery"))
check("F10e-d reset didn't help -> NO further reset", issued_reset(_L) == [])
check("F10e-d -> settle window disarmed", rec["saved"] and not rec["saved"].get("reset_ts"))

# F10e-e: rate exceeded (flapping 4G) -> escalate, never keep resetting
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 2}, rate_ok=lambda n: False, **DOWN)
ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-e rate exceeded -> escalate ec25-reset-rate-exceeded",
      esc_has(rec, "ec25-reset-rate-exceeded"))
check("F10e-e rate exceeded -> NO reset", issued_reset(_L) == [])

# F10e-f: the reset command itself fails (missing sudoers / ModemManager down) -> escalate, do NOT arm settle
_L = []
ctx, rec = mkctx(sh=sh_stub(_L, rc=1, err="sudo: a password is required"), state={"down": 2}, **DOWN)
ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-f reset failed -> escalate ec25-reset-failed", esc_has(rec, "ec25-reset-failed"))
check("F10e-f reset failed -> settle NOT armed (retry next tick)",
      rec["saved"] and not rec["saved"].get("reset_ts"))

# F10e-g: dry-run -> decide and log, touch nothing (the safe rehearsal we deploy with)
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 2}, **DOWN)
ctx.cfg.uplink = "ec25"; ctx.cfg.dry_run = True
ConnectivityHealer().run(ctx)
check("F10e-g dry-run -> NO reset executed", issued_reset(_L) == [])
check("F10e-g dry-run -> says what it WOULD do", any("would reset modem" in m for m in rec["log"]))

# F10e-h: WAN back after a reset -> clear the counters so the next outage starts clean
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), state={"down": 4, "reset_ts": time.time()})   # tcp_up default True
ctx.cfg.uplink = "ec25"
ConnectivityHealer().run(ctx)
check("F10e-h recovered -> counters cleared",
      rec["saved"] and rec["saved"].get("down") == 0 and rec["saved"].get("reset_ts") == 0)
check("F10e-h recovered -> logged as post-reset recovery",
      any("recovered after modem reset" in m for m in rec["log"]))

# --- uplink classification: the wrong class would either reset a Robustel node or strand an IRIV ---

# F10u-a: explicit robustel -> detect+escalate only, and don't even probe for a modem
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), **DOWN); ctx.cfg.uplink = "robustel"
ConnectivityHealer().run(ctx)
check("F10u-a robustel -> escalate detect-only", esc_has(rec, "wan-down-detect-only"))
check("F10u-a robustel -> NO reset (Robustel self-reboots off-node)", issued_reset(_L) == [])
check("F10u-a explicit uplink -> skips modem probing", _L == [])

# F10u-b: auto-detect finds a Quectel EC25 -> ec25 path (counts down, does not escalate detect-only)
_L = []
ctx, rec = mkctx(sh=sh_stub(_L, rules=[("mmcli -L", (0, "/org/.../Modem/0 [Quectel] EC25", ""))]), **DOWN)
ctx.cfg.uplink = "auto"
ConnectivityHealer().run(ctx)
check("F10u-b auto-detect EC25 -> classified ec25", rec["saved"] and rec["saved"].get("uplink") == "ec25")
check("F10u-b auto-detect EC25 -> NOT the detect-only path", not esc_has(rec, "wan-down-detect-only"))

# F10u-c: auto-detect finds no modem -> robustel (never invent an ec25 node)
_L = []
ctx, rec = mkctx(sh=sh_stub(_L), **DOWN); ctx.cfg.uplink = "auto"
ConnectivityHealer().run(ctx)
check("F10u-c auto-detect no modem -> robustel", rec["saved"] and rec["saved"].get("uplink") == "robustel")
check("F10u-c auto-detect no modem -> escalate detect-only", esc_has(rec, "wan-down-detect-only"))
check("F10u-c auto-detect no modem -> NO reset", issued_reset(_L) == [])

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

# T-runner: no DEVICE_ID -> infra-only mode (rev 521). A signage/IRIV node has no
# sensor identity but still needs its 4G/disk/beszel healers; the sensor+stream
# healers stay gated, so an unidentified node can never act on someone's sensor.
class InfraOnly:
    name = "infra-ok"; requires_identity = False
    def run(self, ctx): ctx.log("infra-ran")
ctx, rec = mkctx(); ctx.cfg.device_id = ""
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[InfraOnly(), Ok()])
check("T-runner no-DEVICE_ID -> agent.infra-only event",
      any(c == "agent.infra-only" for c, _ in rec["events"]))
check("T-runner no-DEVICE_ID -> infra healer RUNS", any("infra-ran" in m for m in rec["log"]))
check("T-runner no-DEVICE_ID -> identity healer GATED", not any("ok-ran" in m for m in rec["log"]))
check("T-runner no-DEVICE_ID -> heartbeat counts only what ran",
      rec["hb"] and rec["hb"][-1].get("healers") == 1)

# T-runner: WITH a DEVICE_ID -> everything runs, no infra-only marker
ctx, rec = mkctx()
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[InfraOnly(), Ok()])
check("T-runner with DEVICE_ID -> all healers run",
      any("infra-ran" in m for m in rec["log"]) and any("ok-ran" in m for m in rec["log"]))
check("T-runner with DEVICE_ID -> no infra-only marker",
      not any(c == "agent.infra-only" for c, _ in rec["events"]))

# T-registry: exactly these healers survive on an identity-less node (a sign): the four infra ones
# plus, since v536, the boot marker and the two stream/MQTT channel healers that pick the sign's units
_infra = sorted(h.name for h in default_registry() if not getattr(h, "requires_identity", True))
check("T-registry infra set = beszel/boot/connectivity/disk-hygiene/mqtt-channel/net-probe/stream-socket",
      _infra == ["beszel", "boot", "connectivity", "disk-hygiene", "mqtt-channel", "net-probe", "stream-socket"])

# T-registry: 14 healers, boot marker first then dependency, unique names, F17 after stream-camera
reg = default_registry()
names = [h.name for h in reg]
check("T-registry has 14 healers", len(reg) == 14)
check("T-registry names unique", len(set(names)) == len(names))
check("T-registry boot marker first, dependency next", names[:2] == ["boot", "dependency"])
check("T-registry loop verdict after the radar repair, dropler after loop",
      names.index("loop") == names.index("radar") + 1 and names.index("dropler") == names.index("loop") + 1)
check("T-registry socket healer after camera/republish repairs", names.index("stream-socket") > names.index("stream-republish"))
check("T-registry net-probe last", names[-1] == "net-probe")
check("T-registry F17 after stream-camera", names.index("stream-republish") == names.index("stream") + 1)

# ===========================================================================
# S* unwritable state dir - the failure that looked like perfect health
#
# Real incident (KB rev 1138): the state dir was root-owned on 4 nodes, so
# rate_hit() raised PermissionError on the line BEFORE every repair. runner
# swallowed it as agent.exc and the tick "completed"; systemd said active and
# journald showed heartbeats. pit002 detected 220 faults in 7 days and performed
# ZERO repairs. These tests use a REAL unwritable directory, not a stub.
# ===========================================================================
from pat_fleet_healer.core import state as ST

def unwritable_cfg():
    """A real directory the process genuinely cannot write into."""
    c = Config(env_path=os.devnull, overrides={})
    c.device_id = "PAT-RO-001"; c.dry_run = False; c.grace_s = 120
    d = tempfile.mkdtemp(); _TMP.append(d)
    c.state_dir = os.path.join(d, "pat-smart")
    os.makedirs(c.state_dir)
    os.chmod(c.state_dir, 0o500)                 # r-x: listable, NOT writable
    return c

_ro = unwritable_cfg()
# precondition: if this fails the whole section is vacuous (e.g. running as root)
check("S0 harness really produced an unwritable dir", not os.access(_ro.state_dir, os.W_OK))

# S1: rate_hit must never raise - it sits one line above every repair
_raised = None
try:
    _wrote = ST.rate_hit(_ro, "svc")
except Exception as e:
    _raised = e; _wrote = None
check("S1 rate_hit does NOT raise when state is unwritable", _raised is None)
check("S1 rate_hit reports it did not persist", _wrote is False)

# S2: end-to-end - a real healer with a real unwritable dir must STILL repair.
# Only rate_hit/rate_ok are wired to the real implementation; restart stays a stub.
ctx, rec = mkctx(svc_active=lambda s: (s != "pat-smart-stream"), svc_age=lambda s: 999)
ctx.cfg.state_dir = _ro.state_dir
ctx._svc["rate_ok"] = lambda n: ST.rate_ok(ctx.cfg, n)
ctx._svc["rate_hit"] = lambda n: ST.rate_hit(ctx.cfg, n)
ServiceLivenessHealer().run(ctx)
check("S2 unwritable state -> the repair STILL happens", "pat-smart-stream" in rec["restart"])

# S3: and the lost cap is announced, not silent
ctx, rec = mkctx(state_writable=lambda: False)
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[Ok()])
check("S3 unwritable state -> agent.state-unwritable emitted",
      any(c == "agent.state-unwritable" for c, _ in rec["events"]))
check("S3 the event names the offending dir",
      any(c == "agent.state-unwritable" and "dir" in f for c, f in rec["events"]))
check("S3 unwritable state -> healers still run", any("ok-ran" in m for m in rec["log"]))
check("S3 unwritable state -> tick still completes", len(rec["hb"]) >= 1)

# S4: writable state must stay quiet (no false alarm on 57 healthy nodes)
ctx, rec = mkctx()
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[Ok()])
check("S4 writable state -> NO state-unwritable alarm",
      not any(c == "agent.state-unwritable" for c, _ in rec["events"]))

# S5: the guard must not break real rate limiting on healthy nodes
_ok = real_cfg_ro = Config(env_path=os.devnull, overrides={})
_ok.device_id = "PAT-RW-001"; _ok.state_dir = tempfile.mkdtemp(); _TMP.append(_ok.state_dir)
check("S5 writable rate_hit persists (returns True)", ST.rate_hit(_ok, "svc") is True)
check("S5 rate file actually written", os.path.exists(os.path.join(_ok.state_dir, "healer-rate.json")))
for _ in range(_ok.rate_max + 2):
    ST.rate_hit(_ok, "svc")
check("S5 quota still closes the gate after rate_max", ST.rate_ok(_ok, "svc") is False)
check("S5 a different healer keeps its own quota", ST.rate_ok(_ok, "other") is True)

# S6: state.writable() is a round-trip probe, not a guess
check("S6 writable() True on a writable dir", ST.writable(_ok) is True)
check("S6 writable() False on the unwritable dir", ST.writable(_ro) is False)
check("S6 probe leaves no litter", ".wtest" not in os.listdir(_ok.state_dir))

# S7: the other writers in the same class must not raise either
#     (makedirs used to sit OUTSIDE their try -> an unwritable PARENT raised)
_deep = Config(env_path=os.devnull, overrides={})
_deep.device_id = "PAT-RO-002"
_deep.state_dir = os.path.join(_ro.state_dir, "cannot", "create")   # parent is r-x
_raised = None
try:
    ST.save(_deep, "conn", {"down": 1})
    from pat_fleet_healer.core import events as _EV
    _EV.emit(_deep, "agent.log", {"msg": "x"})
except Exception as e:
    _raised = e
check("S7 save() + emit() do NOT raise when the dir cannot even be created", _raised is None)

os.chmod(_ro.state_dir, 0o700)                   # let the temp cleanup remove it

# ===========================================================================
# P* phase-1 network probe - measures, never remediates
#
# Its whole purpose is to answer "carrier or cabinet?" when the WAN drops, and to
# record how long each outage lasted. The null-vs-zero tests below are not
# pedantry: conflating "could not measure" with "measured zero" is precisely what
# invalidated the 2026-08-03 fleet report.
# ===========================================================================
import json as _pj
import socket

def nb_json(mgmt=True, sig=True, r_up=2, r_tot=2, p_up=65, p_tot=75):
    import json as _j
    return _j.dumps({"management": {"connected": mgmt}, "signal": {"connected": sig},
                     "relays": {"available": r_up, "total": r_tot},
                     "peers": {"connected": p_up, "total": p_tot}})
NB_OK = nb_json()

def sh_net(route="default via 192.168.1.1 dev eth0 proto dhcp", ping="up", rtt="2.1",
           nb=NB_OK, ntp="yes", disk="/dev/root 30G 7G 22G 24% /", mdev="0.5"):
    """Stub the shell surface the probe touches. ping: 'up'|'down'|'missing'."""
    def _sh(c, timeout=15):
        if "ip route" in c:
            return (0, route, "")
        if c.startswith("ping"):
            if ping == "missing":
                return (127, "", "ping: command not found")
            n = 5 if "-c5" in c else 2
            if ping == "up":
                return (0, "%d packets transmitted, %d received, 0%% packet loss\n"
                           "rtt min/avg/max/mdev = 1.0/%s/3.0/%s ms" % (n, n, rtt, mdev), "")
            return (1, "%d packets transmitted, 0 received, 100%% packet loss" % n, "")
        if "netbird status" in c:
            return ((0, nb, "") if nb else (127, "", "not found"))
        if "timedatectl" in c:
            return (0, ntp, "")
        if c.startswith("df "):
            return (0, disk, "")
        return (0, "", "")
    return _sh

def probe_rows(ctx):
    f = os.path.join(ctx.cfg.state_dir, "netprobe.jsonl")
    try:
        return [_pj.loads(l) for l in open(f).read().splitlines() if l.strip()]
    except Exception:
        return []

def mkprobe(centre="", **over):
    o = {"sh": sh_net(), "tcp_up": lambda *a, **k: True}
    o.update(over)
    ctx, rec = mkctx(**o)
    ctx.cfg.probe_sample_s = 300
    # default OFF: a unit test must never reach the real internet, and an
    # unconfigured centre is itself the "not measured" case we want to assert
    ctx.cfg.probe_centre = centre
    return ctx, rec

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

DOWN_WAN = {"tcp_up": lambda *a, **k: False}

# P1 healthy tick -> a telemetry sample, no outage records
ctx, rec = mkprobe()
NetProbeHealer().run(ctx)
rows = probe_rows(ctx)
check("P1 healthy -> one sample row", [r for r in rows if r["k"] == "s"])
check("P1 healthy -> no outage rows", not [r for r in rows if r["k"] in ("down", "up")])
check("P1 sample carries node id + timestamp",
      rows and rows[0].get("n") and rows[0].get("t"))

# P2 sample cadence is respected - a 60s tick must NOT write a sample every time
ctx, rec = mkprobe()
p = NetProbeHealer()
p.run(ctx); p.run(ctx); p.run(ctx)
check("P2 three ticks inside the window -> still ONE sample",
      len([r for r in probe_rows(ctx) if r["k"] == "s"]) == 1)

# P3 WAN drops -> a 'down' row, and the gateway state at that moment is captured
ctx, rec = mkprobe(**DOWN_WAN)
NetProbeHealer().run(ctx)
d = [r for r in probe_rows(ctx) if r["k"] == "down"]
check("P3 wan down -> 'down' row written", len(d) == 1)
check("P3 down row records the gateway was reachable", d and d[0]["gw"] == 1)
check("P3 down -> outage clock started", rec["saved"] and rec["saved"].get("down_since"))

# ---- the discriminator: who was missing during the outage? ----------------
ETH_ROUTE = "default via 192.168.1.1 dev eth0 proto dhcp"
USB_ROUTE = "default via 192.168.225.1 dev usb0 proto dhcp"   # the real PISN route

def outage(gw, ticks=3, elapsed=180, route=ETH_ROUTE, route2=None):
    """Run `ticks` down-ticks with a given gateway state, then recover.
    route2 lets a test change the WAN interface mid-outage (failover)."""
    sh = sh_net(ping=gw, route=route)
    ctx, rec = mkprobe(sh=sh, **DOWN_WAN)
    p = NetProbeHealer()
    for _ in range(ticks):
        p.run(ctx)
    st = dict(rec["saved"] or {})
    st["down_since"] = int(time.time()) - elapsed          # age the outage
    ctx2, rec2 = mkprobe(sh=sh_net(ping=gw, route=route2 or route), state=st)
    ctx2.cfg.state_dir = ctx.cfg.state_dir                 # same probe file
    p.run(ctx2)
    return ctx2, rec2

# P4 gateway answered throughout -> the carrier was missing, not us
ctx, rec = outage("up")
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("P4 gateway up throughout -> verdict 'carrier'", u and u[0]["v"] == "carrier")
check("P4 outage duration recorded (~180s)", u and 170 <= u[0]["dur"] <= 190)
check("P4 emits probe.wan-outage with the verdict",
      any(c == "probe.wan-outage" and f.get("verdict") == "carrier" for c, f in rec["events"]))

# P5 gateway gone too -> the fault was in our cabinet
ctx, rec = outage("down")
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("P5 gateway down too -> verdict 'onsite'", u and u[0]["v"] == "onsite")

# P6 gateway unmeasurable throughout -> 'unknown'; NEVER guessed onto a side
ctx, rec = outage("missing")
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("P6 gateway unmeasurable -> verdict 'unknown' (not carrier, not onsite)",
      u and u[0]["v"] == "unknown")
check("P6 unmeasurable ticks counted separately from up/down",
      u and u[0]["gw_unk"] >= 1 and u[0]["gw_up"] == 0 and u[0]["gw_down"] == 0)

# ---- null vs zero: the discipline the 2026-08-03 report died for ----------

# P7 an unreachable gateway is 0; an unMEASURABLE one is null. Never the same.
ctx, _ = mkprobe(sh=sh_net(ping="down"))
NetProbeHealer().run(ctx)
r_down = [r for r in probe_rows(ctx) if r["k"] == "s"][0]
ctx, _ = mkprobe(sh=sh_net(ping="missing"))
NetProbeHealer().run(ctx)
r_miss = [r for r in probe_rows(ctx) if r["k"] == "s"][0]
check("P7 gateway unreachable -> gw == 0", r_down["gw"] == 0)
check("P7 gateway unmeasurable -> gw is null (NOT 0)", r_miss["gw"] is None)

# P8 no default route at all -> dev/gw null, and the row is still written
ctx, _ = mkprobe(sh=sh_net(route=""))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P8 no route -> dev is null", r["dev"] is None)
check("P8 no route -> still records the sample (silence is data too)", r["k"] == "s")

# P9 an unreadable sensor is null, never 0
h = NetProbeHealer()
check("P9 missing thermal sensor -> null", h._temp() is None or isinstance(h._temp(), float))
check("P9 counters for a nonexistent device -> null", h._counters("nosuchdev0") is None)
check("P9 counters with no device name -> null", h._counters(None) is None)

# P10 THE probe must never act. Not once, under any input.
for opts in ({}, DOWN_WAN, {"sh": sh_net(ping="down")}, {"sh": sh_net(ping="missing")}):
    o = dict(opts)
    ctx, rec = mkprobe(**o)
    NetProbeHealer().run(ctx)
    if rec["restart"] or rec["escalate"] or rec["rate_hit"]:
        check("P10 probe never remediates (%s)" % (list(opts) or "healthy"), False)
        break
else:
    check("P10 probe never restarts / escalates / consumes quota", True)

# P11 the probe writes to its OWN file, not the healer event stream
ctx, rec = mkprobe()
NetProbeHealer().run(ctx)
check("P11 writes netprobe.jsonl", os.path.exists(os.path.join(ctx.cfg.state_dir, "netprobe.jsonl")))
check("P11 does NOT write events.jsonl", not os.path.exists(os.path.join(ctx.cfg.state_dir, "events.jsonl")))

# ---- v525 additions: relay health, the centre, reboot vs outage, jitter -----

# P13 netbird parsed: relays and peers as measured fractions
ctx, _ = mkprobe()
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P13 netbird management/signal parsed", r["nb_mgmt"] == 1 and r["nb_sig"] == 1)
check("P13 relays parsed as 2/2", (r["nb_relay_up"], r["nb_relay_tot"]) == (2, 2))
check("P13 peers parsed as 65/75", (r["nb_peer_up"], r["nb_peer_tot"]) == (65, 75))

# P14 no netbird CLI -> every nb_* is null. A node WITHOUT the overlay must never
# look like a node whose relays are DOWN.
ctx, _ = mkprobe(sh=sh_net(nb=""))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P14 no netbird -> nb_* all null (NOT 0)",
      all(r[k] is None for k in ("nb_mgmt", "nb_sig", "nb_relay_up", "nb_peer_up")))

# P15 relays genuinely down -> 0, which must be distinct from P14's null
ctx, _ = mkprobe(sh=sh_net(nb=nb_json(r_up=0, r_tot=2, p_up=0)))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P15 relays down -> 0 (measured), not null", r["nb_relay_up"] == 0 and r["nb_relay_tot"] == 2)

# P15b netbird IS there but a line is malformed -> that FIELD is null, while the
# fields that DID parse stay real. Partial data must not become fake zeros.
ctx, _ = mkprobe(sh=sh_net(nb='{"management":{"connected":true},"signal":{"connected":true},'
                                     '"relays":{"note":"unavailable"},"peers":{"connected":65,"total":75}}'))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P15b unparseable relays line -> nb_relay_* null (NOT 0)",
      r["nb_relay_up"] is None and r["nb_relay_tot"] is None)
check("P15b the fields that parsed are still real", r["nb_mgmt"] == 1 and r["nb_peer_up"] == 65)

# P16 the centre: reachable / refused / not configured
_p = free_port()
_srv = socket.socket(); _srv.bind(("127.0.0.1", _p)); _srv.listen(1)
ctx, _ = mkprobe(centre="127.0.0.1:%d" % _p)
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P16 centre reachable -> ctr=1 with a latency", r["ctr"] == 1 and r["rtt_ctr"] is not None)
_srv.close()
ctx, _ = mkprobe(centre="127.0.0.1:%d" % free_port())
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P16 centre refused -> ctr=0", r["ctr"] == 0)
ctx, _ = mkprobe(centre="")
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P16 centre not configured -> ctr null (NOT 0)", r["ctr"] is None)

# P17 internet fine but centre gone = its own outage class, with its own duration
_dead = "127.0.0.1:%d" % free_port()
ctx, rec = mkprobe(centre=_dead)
NetProbeHealer().run(ctx)
check("P17 centre down while wan up -> 'ctr_down' row",
      [x for x in probe_rows(ctx) if x["k"] == "ctr_down"])
st = dict(rec["saved"] or {}); st["ctr_since"] = int(time.time()) - 90
_p2 = free_port()
_srv2 = socket.socket(); _srv2.bind(("127.0.0.1", _p2)); _srv2.listen(1)
ctx2, rec2 = mkprobe(centre="127.0.0.1:%d" % _p2)     # centre answering again
ctx2.cfg.state_dir = ctx.cfg.state_dir
ctx2._svc["state_load"] = lambda n: dict(st)
NetProbeHealer().run(ctx2)
_srv2.close()
u = [x for x in probe_rows(ctx2) if x["k"] == "ctr_up"]
check("P17 centre back -> 'ctr_up' row with duration", u and 80 <= u[0]["dur"] <= 100)
check("P17 the end was confirmed, not assumed", u and u[0]["end"] == 1)
check("P17 emits probe.centre-unreachable",
      any(c == "probe.centre-unreachable" for c, _ in rec2["events"]))

# P17b centre becomes UNMEASURABLE mid-outage -> close it, and say the end is unknown.
# Leaving it open would park the clock and later report one absurd duration.
st2 = {"ctr_since": int(time.time()) - 45}
ctx3, rec3 = mkprobe(centre="")
ctx3._svc["state_load"] = lambda n: dict(st2)
NetProbeHealer().run(ctx3)
u = [x for x in probe_rows(ctx3) if x["k"] == "ctr_up"]
check("P17b unmeasurable centre -> outage closed, end recorded as null",
      u and u[0]["end"] is None and 35 <= u[0]["dur"] <= 55)

# P18 if the WAN is down too, that is NOT a centre outage - it is the WAN outage.
# Counting it twice would inflate the very argument this data is meant to test.
ctx, rec = mkprobe(centre=_dead, **DOWN_WAN)
NetProbeHealer().run(ctx)
check("P18 wan down -> no 'ctr_down' (not double-counted)",
      not [x for x in probe_rows(ctx) if x["k"] == "ctr_down"])

# P19 uptime -> separates a reboot from a network outage
ctx, _ = mkprobe()
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P19 uptime recorded", r["up"] is None or (isinstance(r["up"], int) and r["up"] > 0))
import pat_fleet_healer.healers.net_probe as _np
_uf = tempfile.mkstemp()[1]; open(_uf, "w").write("7289753.12 1234.5\n")
_orig_uf = _np.UPTIME_FILE; _np.UPTIME_FILE = _uf
check("P19 uptime parsed from the real file format", NetProbeHealer()._uptime() == 7289753)
_np.UPTIME_FILE = os.path.join(_uf, "nope")
check("P19 unreadable uptime -> null (NOT 0)", NetProbeHealer()._uptime() is None)
_np.UPTIME_FILE = _orig_uf; os.unlink(_uf)

# P20 clock sync is tri-state, never assumed
for val, want, label in (("yes", 1, "synced"), ("no", 0, "not synced"), ("", None, "unknown")):
    ctx, _ = mkprobe(sh=sh_net(ntp=val))
    NetProbeHealer().run(ctx)
    r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
    check("P20 ntp %s -> %s" % (label, want), r["ntp"] == want)

# P21 jitter + loss come from the 5-packet sample, not the cheap per-tick check
ctx, _ = mkprobe(sh=sh_net(rtt="27.5", mdev="6.1"))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P21 jitter (mdev) recorded", r["jit_net"] == 6.1)
check("P21 loss percent recorded as 0 (measured)", r["loss_net"] == 0)
ctx, _ = mkprobe(sh=sh_net(ping="down"))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P21 total loss -> 100 (measured), rtt null (nothing came back)",
      r["loss_net"] == 100 and r["rtt_net"] is None)

# P22 disk + memory
ctx, _ = mkprobe()
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P22 disk percent parsed", r["disk"] == 24)
check("P22 memory percent is a sane 0-100 or null",
      r["mem"] is None or 0 <= r["mem"] <= 100)
# P22b df says something we cannot parse -> null. A 0 here would read as
# "disk completely empty", which is the most dangerous possible wrong answer.
ctx, _ = mkprobe(sh=sh_net(disk="df: /: Operation not permitted"))
NetProbeHealer().run(ctx)
r = [x for x in probe_rows(ctx) if x["k"] == "s"][0]
check("P22b unparseable df -> disk null (NOT 0)", r["disk"] is None)

# P23 the per-tick gateway check stays cheap; only the sample pays for 5 packets
_seen = []
def sh_count(c, timeout=15):
    if c.startswith("ping"):
        _seen.append("-c5" if "-c5" in c else "-c2")
    return sh_net()(c, timeout)
ctx, _ = mkprobe(sh=sh_count)
p = NetProbeHealer()
p.run(ctx); _seen[:] = []; p.run(ctx)      # 2nd tick is inside the sample window
check("P23 a non-sampling tick uses only the cheap 2-packet check",
      _seen and all(x == "-c2" for x in _seen))

# P12 an unwritable state dir must not break the tick (a probe is never load-bearing)
ctx, rec = mkprobe()
_ro = tempfile.mkdtemp(); _TMP.append(_ro)
ctx.cfg.state_dir = os.path.join(_ro, "ro"); os.makedirs(ctx.cfg.state_dir); os.chmod(ctx.cfg.state_dir, 0o500)
_raised = None
try:
    NetProbeHealer().run(ctx)
except Exception as e:
    _raised = e
os.chmod(ctx.cfg.state_dir, 0o700)
check("P12 unwritable state dir -> probe does NOT raise", _raised is None)

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
    "connectivity.wan-down-detect-only",
    "connectivity.ec25-reset-failed", "connectivity.ec25-reset-no-recovery",
    "connectivity.ec25-reset-rate-exceeded"]
_missing = [x for x in _expected if x not in SCH.CODES]
check("E2 manifest covers all escalation codes", not _missing)
check("E2 manifest decodes agent.infra-only", "agent.infra-only" in SCH.CODES)
check("E2 manifest decodes agent.state-unwritable", "agent.state-unwritable" in SCH.CODES)
check("E2 state-unwritable is sev=error (so it pushes to central)",
      SCH.CODES.get("agent.state-unwritable", {}).get("sev") == "error")
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
# G1-G12  what the gateway ACTUALLY is decides what "carrier" is worth.
# Found 2026-08-05 from the first real probe data: all 7 recorded outages were
# verdict "carrier", and all 7 came from pisn002 - the one node class where that
# verdict means the least. On pit/pir the default route is eth0 and the gateway
# 192.168.1.1 is the Robustel, a separately powered box in the cabinet. On the
# PISN IRIVs the route is usb0 and the gateway 192.168.225.1 is the EC25 modem's
# OWN RNDIS interface: it answers while the module is merely enumerated on USB,
# with no cellular registration at all. Same word, different evidence.
# ---------------------------------------------------------------------------
check("G1 eth0 gateway is a separate router", NetProbeHealer._gw_kind("eth0") == "router")
check("G1 eno1 gateway is a separate router", NetProbeHealer._gw_kind("eno1") == "router")
check("G1 usb0 gateway is the node's own modem", NetProbeHealer._gw_kind("usb0") == "modem")
check("G1 wwan0 is a modem", NetProbeHealer._gw_kind("wwan0") == "modem")
check("G1 ppp0 is a modem", NetProbeHealer._gw_kind("ppp0") == "modem")
check("G1 unknown interface -> None, never 'router'", NetProbeHealer._gw_kind(None) is None)
check("G1 empty interface -> None", NetProbeHealer._gw_kind("") is None)

check("G2 modem gateway up throughout is NOT evidence of 'carrier'",
      NetProbeHealer._verdict(5, 0, 0, "modem") == "unknown")
check("G3 router gateway up throughout IS 'carrier'",
      NetProbeHealer._verdict(5, 0, 0, "router") == "carrier")
check("G4 unidentified gateway must not claim 'carrier'",
      NetProbeHealer._verdict(5, 0, 0, None) == "unknown")
check("G5 'onsite' needs no gate - a dead gateway is local either way",
      NetProbeHealer._verdict(0, 5, 0, "modem") == "onsite"
      and NetProbeHealer._verdict(0, 5, 0, "router") == "onsite")

# end to end through the state machine, on the REAL pisn route string
ctx, rec = outage("up", route=USB_ROUTE)
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("G6 PISN outage: gateway up throughout -> 'unknown', NOT 'carrier'",
      u and u[0]["v"] == "unknown")
check("G6 PISN outage still records the raw tallies (nothing lost)",
      u and u[0]["gw_up"] >= 1 and u[0]["gw_down"] == 0)
check("G7 up row carries the interface", u and u[0].get("dev") == "usb0")
check("G7 up row carries the gateway kind", u and u[0].get("gwk") == "modem")
d = [r for r in probe_rows(ctx) if r["k"] == "down"]
check("G8 down row carries interface + kind",
      d and d[0].get("dev") == "usb0" and d[0].get("gwk") == "modem")
check("G9 the event carries the gateway kind too",
      any(c == "probe.wan-outage" and f.get("gwk") == "modem" for c, f in rec["events"]))

# the 56 pit/pir nodes must be UNAFFECTED
ctx, rec = outage("up", route=ETH_ROUTE)
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("G10 pit/pir keep their discriminator: verdict stays 'carrier'",
      u and u[0]["v"] == "carrier")
check("G10 pit/pir rows labelled router/eth0",
      u and u[0].get("gwk") == "router" and u[0].get("dev") == "eth0")

# a cabinet fault on a PISN node must still read as onsite
ctx, rec = outage("down", route=USB_ROUTE)
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("G11 PISN cabinet fault still reads 'onsite'", u and u[0]["v"] == "onsite")

# interface changed mid-outage -> we no longer know what was pinged
ctx, rec = outage("up", route=ETH_ROUTE, route2=USB_ROUTE)
u = [r for r in probe_rows(ctx) if r["k"] == "up"]
check("G12 interface changed mid-outage -> no side claimed",
      u and u[0]["v"] == "unknown" and u[0].get("gwk") is None)


# ---------------------------------------------------------------------------
# K1-K14  the stream session vs the internet, recorded on ONE row.
# The question this closes: when a stream to the centre drops, was the mobile
# network actually gone, or was the internet fine and the session broke anyway?
# Measured 5 Aug over 11.5h: 7 internet outages (all one node) vs 9 stream breaks
# (three OTHER nodes) with ZERO overlap - which, if it holds, means the drops are
# not the carrier's. That claim is too load-bearing to rest on lining up two logs
# by hand at 60s resolution, so the probe now records both facts together.
# ---------------------------------------------------------------------------
check("K1 internet up throughout -> 'net-ok' (NOT the mobile network)",
      NetProbeHealer._push_verdict(5, 0) == "net-ok")
check("K1 internet gone throughout -> 'wan'", NetProbeHealer._push_verdict(0, 5) == "wan")
check("K1 both -> 'mixed'", NetProbeHealer._push_verdict(3, 2) == "mixed")
check("K1 nothing observed -> 'unknown', never a guess",
      NetProbeHealer._push_verdict(0, 0) == "unknown")

def pushctx(estab, active=True, wan=True, state=None):
    o = {"estab_1935": (lambda: estab), "svc_active": (lambda s: active),
         "tcp_up": (lambda *a, **k: wan)}
    ctx, rec = mkprobe(state=state, **o)
    return ctx, rec

# service running but nothing established = a real stream outage
ctx, rec = pushctx(0)
NetProbeHealer().run(ctx)
d = [r for r in probe_rows(ctx) if r["k"] == "push_down"]
check("K2 stream service up but no session -> push_down row", len(d) == 1)
check("K2 push_down records what the internet was doing", d and d[0].get("wan") == 1)

# a session that is established writes nothing
ctx, rec = pushctx(2)
NetProbeHealer().run(ctx)
check("K3 an established session writes no outage row",
      not [r for r in probe_rows(ctx) if r["k"] in ("push_down", "push_up")])

# the decisive case: internet reachable for the WHOLE stream outage
def push_outage(wan_seq, elapsed=120):
    """Run down-ticks with a given internet state, then let the session return."""
    ctx, rec = pushctx(0, wan=wan_seq[0])
    p = NetProbeHealer()
    p.run(ctx)
    st = dict(rec["saved"] or {})
    for w in wan_seq[1:]:
        ctx2, rec2 = pushctx(0, wan=w, state=st)
        ctx2.cfg.state_dir = ctx.cfg.state_dir
        p.run(ctx2); st = dict(rec2["saved"] or {})
    st["push_since"] = int(time.time()) - elapsed
    ctx3, rec3 = pushctx(1, wan=True, state=st)      # session back
    ctx3.cfg.state_dir = ctx.cfg.state_dir
    p.run(ctx3)
    return ctx3, rec3

ctx, rec = push_outage([True, True, True])
u = [r for r in probe_rows(ctx) if r["k"] == "push_up"]
check("K4 internet fine throughout the stream outage -> verdict 'net-ok'",
      u and u[-1]["v"] == "net-ok")
check("K4 duration recorded (~120s)", u and 110 <= u[-1]["dur"] <= 130)
check("K4 tallies kept: wan_up>0 and wan_down==0",
      u and u[-1]["wan_up"] >= 1 and u[-1]["wan_down"] == 0)
check("K5 emits probe.stream-session-lost carrying the verdict",
      any(c == "probe.stream-session-lost" and f.get("verdict") == "net-ok"
          for c, f in rec["events"]))

ctx, rec = push_outage([False, False])
u = [r for r in probe_rows(ctx) if r["k"] == "push_up"]
check("K6 internet gone too -> verdict 'wan'", u and u[-1]["v"] == "wan")

ctx, rec = push_outage([True, False, True])
u = [r for r in probe_rows(ctx) if r["k"] == "push_up"]
check("K7 internet flapped during the outage -> 'mixed', no side claimed",
      u and u[-1]["v"] == "mixed")

# a STOPPED service is not an outage - the PISN signage nodes would otherwise
# report a permanent stream failure
ctx, rec = pushctx(0, active=False)
NetProbeHealer().run(ctx)
check("K8 stopped stream service writes NO outage row (not pushing on purpose)",
      not [r for r in probe_rows(ctx) if r["k"] in ("push_down", "push_up")])

# null vs zero, again: the field that says how many sessions are up
ctx, rec = pushctx(3)
NetProbeHealer().run(ctx)
srow = [r for r in probe_rows(ctx) if r["k"] == "s"]
check("K9 sample carries the session count", srow and srow[0].get("push") == 3)
ctx, rec = pushctx(0, active=False)
NetProbeHealer().run(ctx)
srow = [r for r in probe_rows(ctx) if r["k"] == "s"]
check("K10 service not running -> push is null, NOT 0",
      srow and "push" in srow[0] and srow[0]["push"] is None)

# a service that stops mid-outage must not leave the episode open forever
ctx, rec = pushctx(0)
NetProbeHealer().run(ctx)
st = dict(rec["saved"] or {})
check("K11 an open stream outage is recorded in state", st.get("push_since"))
ctx2, rec2 = pushctx(0, active=False, state=st)
ctx2.cfg.state_dir = ctx.cfg.state_dir
NetProbeHealer().run(ctx2)
check("K11 stopping the service clears the open outage instead of stranding it",
      not (rec2["saved"] or {}).get("push_since"))

# the stream machine must not disturb the WAN machine
ctx, rec = pushctx(0, wan=False)
NetProbeHealer().run(ctx)
rows = probe_rows(ctx)
check("K12 a stream outage does not suppress the WAN outage record",
      [r for r in rows if r["k"] == "down"] and [r for r in rows if r["k"] == "push_down"])
check("K13 the probe still never remediates", rec["restart"] == [] and rec["escalate"] == [])
check("K14 code is documented in the manifest",
      "probe.stream-session-lost" in __import__("pat_fleet_healer.events_schema",
                                                fromlist=["CODES"]).CODES)

# ---------------------------------------------------------------------------
# M1-M25  central push (MQTT).  The bug being closed: events.py hardcoded port
# 1883, which is CLOSED on this fleet's broker, inside a bare `except: pass`.
# Result: the centre received ZERO healer events for months and nothing said so.
# Two failures had to be fixed together - the wrong port, and the silence.
# The protocol tests run against a REAL socket server speaking real bytes, so
# they test the wire format, not a mock of our own assumptions.
# ---------------------------------------------------------------------------
import json as _json
import socket as _socket
import threading as _threading
from pat_fleet_healer.core import mqtt as _mqtt
from pat_fleet_healer.core import events as _events
from pat_fleet_healer.core import escalate as _escalate


class FakeBroker(object):
    """A socket server that speaks just enough MQTT to be answered honestly."""

    def __init__(self, connack_rc=0, mode="normal"):
        self.rc, self.mode = connack_rc, mode
        self.connect_pkt = b""
        self.publish_pkt = b""
        self.srv = _socket.socket()
        self.srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.t = _threading.Thread(target=self._run)
        self.t.daemon = True
        self.t.start()

    def _run(self):
        try:
            self.srv.settimeout(5)
            c, _ = self.srv.accept()
            c.settimeout(5)
            if self.mode == "hangup":
                c.close(); return
            self.connect_pkt = c.recv(4096)
            if self.mode == "garbage":
                c.sendall(b"\x99\x02\x00\x00")
            else:
                c.sendall(bytes(bytearray([0x20, 0x02, 0x00, self.rc])))
            if self.rc == 0:
                # Drain until the client closes. PUBLISH and DISCONNECT are two
                # separate sendall() calls, so a single recv() may return only the
                # PUBLISH - a TCP coalescing race that made "M4 DISCONNECT follows
                # the publish" flaky (it failed ~1 run in 3 on v530, and a release
                # gate that lies is worse than none). Reading to EOF is
                # deterministic; the client always closes after DISCONNECT.
                buf = b""
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                self.publish_pkt = buf
            c.close()
        except Exception:
            pass
        finally:
            try: self.srv.close()
            except Exception: pass

    def join(self):
        self.t.join(6)


# ---- wire format ----------------------------------------------------------
check("M1 remaining-length 0", _mqtt._remaining_length(0) == b"\x00")
check("M1 remaining-length 127", _mqtt._remaining_length(127) == b"\x7f")
check("M1 remaining-length 128 (2-byte)", _mqtt._remaining_length(128) == b"\x80\x01")
check("M1 remaining-length 16383", _mqtt._remaining_length(16383) == b"\xff\x7f")
check("M1 remaining-length 16384 (3-byte)", _mqtt._remaining_length(16384) == b"\x80\x80\x01")

_b = FakeBroker()
_ok = _mqtt.publish("127.0.0.1", _b.port, "fleet/events/N1", "hello", "N1")
_b.join()
check("M2 accepted publish returns True", _ok is True)
check("M3 CONNECT starts with 0x10", _b.connect_pkt[:1] == b"\x10")
check("M3 CONNECT names protocol MQTT", b"MQTT" in _b.connect_pkt[:12])
check("M3 CONNECT is protocol level 4", _b.connect_pkt[8:9] == b"\x04")
check("M3 CONNECT sets clean-session", _b.connect_pkt[9:10] == b"\x02")
check("M3 CONNECT carries the client id", b"N1" in _b.connect_pkt)
check("M4 PUBLISH starts with 0x30 (QoS 0)", _b.publish_pkt[:1] == b"\x30")
check("M4 PUBLISH carries the topic", b"fleet/events/N1" in _b.publish_pkt)
check("M4 PUBLISH carries the payload", b"hello" in _b.publish_pkt)
check("M4 DISCONNECT follows the publish", _b.publish_pkt.endswith(b"\xe0\x00"))

_b = FakeBroker(connack_rc=5)                     # 5 = not authorised
_ok = _mqtt.publish("127.0.0.1", _b.port, "t", "x", "N1"); _b.join()
check("M5 refused CONNACK is NOT reported as sent", _ok is False)

_b = FakeBroker(mode="hangup")
_ok = _mqtt.publish("127.0.0.1", _b.port, "t", "x", "N1"); _b.join()
check("M6 broker hangs up -> False", _ok is False)

_b = FakeBroker(mode="garbage")
_ok = _mqtt.publish("127.0.0.1", _b.port, "t", "x", "N1"); _b.join()
check("M11 non-CONNACK reply -> False", _ok is False)

_s = _socket.socket(); _s.bind(("127.0.0.1", 0)); _dead = _s.getsockname()[1]; _s.close()
check("M7 nothing listening -> False (no raise)",
      _mqtt.publish("127.0.0.1", _dead, "t", "x", "N1") is False)
check("M8 unresolvable host -> False (no raise)",
      _mqtt.publish("no-such-host.invalid", 8883, "t", "x", "N1", timeout=2) is False)
check("M12 nonsense port -> False (no raise)",
      _mqtt.publish("127.0.0.1", "not-a-port", "t", "x", "N1") is False)

_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", u"\u0e19\u0e49\u0e33\u0e17\u0e48\u0e27\u0e21", "N1"); _b.join()
check("M9 non-ASCII payload goes out as UTF-8",
      u"\u0e19\u0e49\u0e33\u0e17\u0e48\u0e27\u0e21".encode("utf-8") in _b.publish_pkt)

_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", "x", "X" * 40); _b.join()
check("M10 over-long client id truncated to 23", b"X" * 23 in _b.connect_pkt
      and b"X" * 24 not in _b.connect_pkt)

# ---- config: the port itself ----------------------------------------------
# v529: the default moved 8883 -> 1883 when the fleet left the public NAT for the
# overlay broker (10.0.4.80), whose plain listener is 1883 and whose 8883 is REAL
# TLS. A node with no MQTT_PORT line (the PISN signs) must land on 1883.
check("M13 default port is 1883 (overlay plain listener; NOT the retired 8883)",
      Config(env_path=os.devnull, overrides={}).mqtt_port == 1883)
_envf = os.path.join(tempfile.mkdtemp(), ".env"); _TMP.append(os.path.dirname(_envf))
open(_envf, "w").write("MQTT_HOST=h\nMQTT_PORT=1234\n")
check("M14 .env MQTT_PORT wins", Config(env_path=_envf, overrides={}).mqtt_port == 1234)
open(_envf, "w").write("MQTT_PORT=eight-thousand\n")
check("M15 malformed MQTT_PORT falls back, does not raise",
      Config(env_path=_envf, overrides={}).mqtt_port == 1883)

# ---- config: the centre probe target --------------------------------------
# v529: PROBE_CENTRE derives from the publish target instead of hardcoding the
# retired public NAT, and is overridable from .env (before: os.environ only, so
# no .env edit could ever correct it).
open(_envf, "w").write("MQTT_HOST=10.9.9.9\nMQTT_PORT=1883\n")
check("M26 probe_centre defaults to MQTT_HOST:MQTT_PORT (never drifts from the publish target)",
      Config(env_path=_envf, overrides={}).probe_centre == "10.9.9.9:1883")
check("M27 probe_centre with no .env at all is still host:port, never the public NAT",
      Config(env_path=os.devnull, overrides={}).probe_centre == "localhost:1883")
open(_envf, "w").write("MQTT_HOST=10.9.9.9\nMQTT_PORT=1883\nPROBE_CENTRE=probe.example:9999\n")
check("M28 .env PROBE_CENTRE overrides the derived default",
      Config(env_path=_envf, overrides={}).probe_centre == "probe.example:9999")
check("M29 environment PROBE_CENTRE still works (systemd drop-in path)",
      Config(env_path=os.devnull, overrides={"PROBE_CENTRE": "env.example:7"}).probe_centre
      == "env.example:7")
check("M30 no default anywhere names the retired public endpoint",
      "pattaya-smart-sanitary" not in Config(env_path=os.devnull, overrides={}).probe_centre)

# ---- config + wire format: broker credentials (v530) -----------------------
# The healer cannot speak TLS (raw socket, no ssl import), so username/password is its
# ONLY way to authenticate once the broker drops allow_anonymous.
check("M31 no credentials configured by default",
      Config(env_path=os.devnull, overrides={}).mqtt_username == ""
      and Config(env_path=os.devnull, overrides={}).mqtt_password == "")
open(_envf, "w").write("MQTT_USERNAME=node-a\nMQTT_PASSWORD=pw123\n")
_cc = Config(env_path=_envf, overrides={})
check("M32 credentials load from .env", _cc.mqtt_username == "node-a" and _cc.mqtt_password == "pw123")
check("M33 credentials also settable from the environment",
      Config(env_path=os.devnull, overrides={"MQTT_USERNAME": "e"}).mqtt_username == "e")

# wire format: inspect the CONNECT packet a real broker would receive.
# Layout: [0]=0x10 [1]=remlen 0x00 0x04 'M''Q''T''T' [8]=level [9]=flags
_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", "x", "CID"); _b.join()
check("M34 anonymous CONNECT unchanged from pre-v530 (level 4, flags 0x02, no creds)",
      _b.connect_pkt[8:10] == b"\x04\x02" and b"node-a" not in _b.connect_pkt)

_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", "x", "CID", username="node-a", password="pw123"); _b.join()
check("M35 user+password sets CONNECT flags 0x80|0x40|0x02 = 0xC2",
      _b.connect_pkt[8:10] == b"\x04\xc2")
check("M36 username then password both present, in spec order",
      b"node-a" in _b.connect_pkt and b"pw123" in _b.connect_pkt
      and _b.connect_pkt.index(b"node-a") < _b.connect_pkt.index(b"pw123"))

_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", "x", "CID", username="node-a"); _b.join()
check("M37 username alone sets 0x80|0x02 = 0x82",
      _b.connect_pkt[8:10] == b"\x04\x82" and b"node-a" in _b.connect_pkt)

_b = FakeBroker()
_mqtt.publish("127.0.0.1", _b.port, "t", "x", "CID", password="orphan"); _b.join()
check("M38 password WITHOUT username is ignored (MQTT 3.1.1 3.1.2.9)",
      _b.connect_pkt[8:10] == b"\x04\x02" and b"orphan" not in _b.connect_pkt)

check("M39 CONNACK rc=4 (bad user/password) -> False, no raise",
      _mqtt.publish("127.0.0.1", FakeBroker(connack_rc=4).port, "t", "x", "CID",
                    username="u", password="bad") is False)
check("M40 CONNACK rc=5 (not authorized) -> False, no raise",
      _mqtt.publish("127.0.0.1", FakeBroker(connack_rc=5).port, "t", "x", "CID",
                    username="u", password="p") is False)

# ---- config + wire: TLS material (v531) -----------------------------------
# The healer shares ~/.config/pat-smart/.env with the station workers, so it uses the
# SAME key names (MQTT_CA / MQTT_CERT / MQTT_PRIVATE_KEY). Before v531 it could not do
# TLS at all, which is what made moving MQTT_PORT to 8883 a fleet-wide deadlock.
check("M41 no TLS material by default",
      Config(env_path=os.devnull, overrides={}).mqtt_ca == "")
_tlsdir = tempfile.mkdtemp(); _TMP.append(_tlsdir)
_fake_ca = os.path.join(_tlsdir, "ca.pem"); open(_fake_ca, "w").write("x")
_w = lambda body: open(_envf, "w").write(body)
_w("MQTT_CA=" + _fake_ca + "\n")
check("M42 CA ALONE -> TLS stays OFF (matches the workers; broker needs a client cert)",
      Config(env_path=_envf, overrides={}).mqtt_ca == "")
_w("MQTT_CA=/nope/missing.pem\n")
check("M43 CA path that does not exist -> TLS stays OFF (half-provisioned node stays plaintext)",
      Config(env_path=_envf, overrides={}).mqtt_ca == "")
_fake_cert = os.path.join(_tlsdir, "c.pem"); open(_fake_cert, "w").write("x")
_fake_key  = os.path.join(_tlsdir, "k.pem"); open(_fake_key, "w").write("x")
_w("MQTT_CA=" + _fake_ca + "\nMQTT_CERT=/nope/c.pem\nMQTT_PRIVATE_KEY=/nope/k.pem\n")
check("M44 cert/key configured but MISSING on disk -> TLS OFF, not a half state",
      Config(env_path=_envf, overrides={}).mqtt_ca == "")
_w("MQTT_CA=" + _fake_ca + "\nMQTT_CERT=" + _fake_cert + "\nMQTT_PRIVATE_KEY=" + _fake_key + "\n")
check("M44b all three present -> mutual TLS engages",
      Config(env_path=_envf, overrides={}).mqtt_ca == _fake_ca
      and Config(env_path=_envf, overrides={}).mqtt_cert == _fake_cert
      and Config(env_path=_envf, overrides={}).mqtt_key == _fake_key)
check("M45 plaintext path unchanged when no TLS configured",
      _mqtt.publish("127.0.0.1", FakeBroker().port, "t", "x", "CID") is True)
check("M46 TLS requested against a PLAINTEXT broker -> False, no raise, no silent downgrade",
      _mqtt.publish("127.0.0.1", FakeBroker().port, "t", "x", "CID", ca=_fake_ca) is False)

# ---- integration: emit / heartbeat / escalate ------------------------------
def mqctx(ok=True):
    """Capture what emit() hands the transport, without opening a socket."""
    cfg = Config(env_path=os.devnull, overrides={})
    cfg.state_dir = tempfile.mkdtemp(); _TMP.append(cfg.state_dir)
    cfg.device_id = "PAT-TEST-M"; cfg.dry_run = False
    # a port that is NEITHER default (old 8883, new 1883): proves the transport takes
    # cfg.mqtt_port rather than any hardcoded number - the old `== 8883` assertion
    # against a default-built Config could not tell those apart.
    cfg.mqtt_port = 18830
    calls = []
    def fake(host, port, topic, payload, client_id, **kw):
        calls.append({"host": host, "port": port, "topic": topic,
                      "payload": payload, "cid": client_id})
        return ok
    return cfg, calls, fake

_orig_pub = _mqtt.publish
try:
    cfg, calls, fake = mqctx(ok=True)
    _events.mqtt.publish = fake
    _events.emit(cfg, "radar.sensor-absent")                 # sev=error -> pushes
    check("M16 emit pushes on error severity", len(calls) == 1)
    check("M16 emit uses cfg.mqtt_port, not a hardcoded 1883/8883",
          calls and calls[0]["port"] == 18830)
    check("M16 emit topic is fleet/events/<node>",
          calls and calls[0]["topic"] == "fleet/events/%s" % cfg.node_id)
    _events.emit(cfg, "agent.alive")                          # sev=info -> no push
    check("M23 info severity does NOT push", len(calls) == 1)
    check("M18 success -> push=1, no pfail",
          _events.push_health(cfg) == {"push": 1})

    cfg, calls, fake = mqctx(ok=False)
    _events.mqtt.publish = fake
    check("M21 no push ever attempted -> {} (absent, NOT zero)",
          _events.push_health(cfg) == {})
    _events.emit(cfg, "radar.sensor-absent")
    check("M17 failure recorded as push=0 pfail=1",
          _events.push_health(cfg) == {"push": 0, "pfail": 1})
    _events.emit(cfg, "radar.sensor-absent")
    _events.emit(cfg, "radar.sensor-absent")
    check("M19 consecutive failures accumulate",
          _events.push_health(cfg).get("pfail") == 3)
    _events.mqtt.publish = lambda *a, **k: True
    _events.emit(cfg, "radar.sensor-absent")
    check("M20 a success resets the failure run",
          _events.push_health(cfg) == {"push": 1})

    # the tick must survive a transport that explodes
    cfg, calls, fake = mqctx()
    def boom(*a, **k): raise RuntimeError("socket exploded")
    _events.mqtt.publish = boom
    _raised = False
    try:
        _events.emit(cfg, "radar.sensor-absent")
    except Exception:
        _raised = True
    check("M24 a throwing transport must not kill the tick", _raised is False)

    # heartbeat carries the previous outcome
    cfg, calls, fake = mqctx(ok=False)
    _events.mqtt.publish = fake
    cfg.heartbeat_s = 0
    _events.emit(cfg, "radar.sensor-absent")                 # one failed push on record
    _events.heartbeat(cfg, sw="525")
    _hb = [_json.loads(l) for l in open(os.path.join(cfg.state_dir, "events.jsonl"))
           if '"agent.alive"' in l]
    check("M22 heartbeat carries push health",
          bool(_hb) and _hb[-1]["d"].get("push") == 0 and _hb[-1]["d"].get("pfail") >= 1)
    check("M22 heartbeat keeps its own fields too",
          bool(_hb) and _hb[-1]["d"].get("sw") == "525")

    # escalate
    cfg, calls, fake = mqctx(ok=False)
    _escalate.mqtt.publish = fake
    _escalate.escalate(cfg, "connectivity", "wan-down", {"k": 1})
    check("M25 escalate uses cfg.mqtt_port, not a hardcoded 1883/8883",
          calls and calls[0]["port"] == 18830)
    check("M25 escalate topic is healer/<id>/escalate",
          calls and calls[0]["topic"] == "healer/%s/escalate" % cfg.device_id)
    _log = open(os.path.join(cfg.state_dir, "healer.log")).read()
    check("M25 escalate records the failure instead of swallowing it",
          "escalate-publish-failed" in _log)
finally:
    _events.mqtt.publish = _orig_pub
    _escalate.mqtt.publish = _orig_pub

# ===========================================================================
# v536 - E1 stream socket wedge, E2 MQTT channel, E3 VEGAMET loop, E4 boot / dropler
# ===========================================================================
from pat_fleet_healer.healers.stream_socket import StreamSocketHealer
from pat_fleet_healer.healers.mqtt_channel import MqttChannelHealer
from pat_fleet_healer.healers.vegamet_loop import VegametLoopHealer
from pat_fleet_healer.healers.node_boot import NodeBootHealer
from pat_fleet_healer.healers.dropler_fitted import DroplerFittedHealer

def _ss_1935(acked, backoff=0, unacked=0, sendq=0):
    return (0, "Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
               "0 %d 192.168.225.51:54222 122.154.116.228:1935\n"
               "\t cubic rto:120000 backoff:%d bytes_sent:11358899878 bytes_acked:%d unacked:%d"
               % (sendq, backoff, acked, unacked), "")

def _wsh(acked, **kw):
    def sh(c, timeout=15):
        if "dport = :1935" in c:
            return _ss_1935(acked, **kw)
        if "systemctl restart" in c:
            sh.restarts.append(c)
            return (0, "", "")
        return (0, "", "")
    sh.restarts = []
    return sh

# a wedged socket has data stuck: the real PISN line (67 segments unacked, backoff 9, 227 KB queued).
# v537: a flow with NOTHING unacked and an empty queue is an idle encoder, never a wedge.
STUCK = {"unacked": 67, "backoff": 9, "sendq": 226885}

def _wedge_ctx(acked_seq, sock=None, **over):
    """Feed successive ticks with the given bytes_acked values, spaced 65 s apart (fake clock).
    sock = the socket shape kwargs for _ss_1935 (default: idle - nothing unacked)."""
    import pat_fleet_healer.healers.stream_socket as _m
    ctx, rec = mkctx(env={"RTMP_URL": "rtmp://rtmp.example/CCTVApp/STREAM-1"}, **over)
    ctx.cfg.wedge_window_s = 300
    ctx.cfg.wedge_min_bytes = 100000
    t0 = [1_790_000_000.0]
    _orig = _m.time.time
    _m.time.time = lambda: t0[0]
    h = StreamSocketHealer()
    shs = []
    try:
        for a in acked_seq:
            sh = _wsh(a, **(sock or {}))
            ctx._svc["sh"] = sh
            shs.append(sh)
            h.run(ctx)
            t0[0] += 65
    finally:
        _m.time.time = _orig
    return ctx, rec, shs

# W1 healthy stream: bytes_acked grows by megabytes -> nothing happens
ctx, rec, shs = _wedge_ctx([1_000_000 * i for i in range(1, 8)])
check("W1 healthy socket -> no event, no restart", not rec["events"] and not any(s.restarts for s in shs))

# W2 total black hole: bytes_acked frozen for > window -> one event + one restart of pat-smart-stream
ctx, rec, shs = _wedge_ctx([500] * 7, sock=STUCK)
_ev = [c for c, f in rec["events"] if c == "stream.socket-wedged"]
check("W2 frozen bytes_acked -> stream.socket-wedged", len(_ev) == 1)
_all = [x for s in shs for x in s.restarts]
check("W2 -> restart pat-smart-stream (long timeout path)", len(_all) == 1 and "pat-smart-stream" in _all[0])
check("W2 -> rate quota used", "stream-socket" in rec["rate_hit"])
check("W2 -> not a second restart inside the settle window", sum(len(s.restarts) for s in shs) == 1)

# W3 trickle: +1388 B every tick (the 09-12/15/16 flavour) is BELOW the threshold -> wedge
ctx, rec, shs = _wedge_ctx([1388 * i for i in range(1, 8)], sock=STUCK)
check("W3 trickle (1.4 KB/tick) -> counted as wedged", any(c == "stream.socket-wedged" for c, _ in rec["events"]))

# W4 AMS unreachable -> not our problem (F17 / supervisor), no event
ctx, rec, shs = _wedge_ctx([500] * 7, sock=STUCK, tcp_up=lambda *a, **k: False)
check("W4 AMS down -> silent", not rec["events"] and not any(s.restarts for s in shs))

# W5 unit inactive / in grace -> silent
ctx, rec, shs = _wedge_ctx([500] * 7, sock=STUCK, svc_active=lambda s: False)
check("W5 unit inactive -> silent", not rec["events"])
ctx, rec, shs = _wedge_ctx([500] * 7, sock=STUCK, svc_age=lambda s: 10)
check("W5 unit in startup grace -> silent", not rec["events"])

# W6 no socket at all -> silent (liveness / supervisor territory)
ctx, rec = mkctx(env={"RTMP_URL": "rtmp://rtmp.example/x"}, sh=lambda c, timeout=15: (0, "", ""))
StreamSocketHealer().run(ctx)
check("W6 no :1935 socket -> silent", not rec["events"])

# W7 quota spent -> escalate, no restart
ctx, rec, shs = _wedge_ctx([500] * 7, sock=STUCK, rate_ok=lambda n: False)
check("W7 rate exceeded -> socket-wedge-persists", esc_has(rec, "socket-wedge-persists"))
check("W7 rate exceeded -> no restart", not any(s.restarts for s in shs))

# W8 dry run -> event + log, no restart
ctx, rec, shs = _wedge_ctx([500] * 7)
ctx2, rec2, shs2 = (None, None, None)
ctx, rec = mkctx(env={"RTMP_URL": "rtmp://rtmp.example/x"})
ctx.cfg.dry_run = True
ctx.cfg.wedge_window_s = 300; ctx.cfg.wedge_min_bytes = 100000
import pat_fleet_healer.healers.stream_socket as _m
_t = [1_790_000_000.0]; _o = _m.time.time; _m.time.time = lambda: _t[0]
try:
    sh = _wsh(500, **STUCK); ctx._svc["sh"] = sh; h = StreamSocketHealer()
    for _ in range(7):
        h.run(ctx); _t[0] += 65
finally:
    _m.time.time = _o
check("W8 dry run -> event but NO restart", any(c == "stream.socket-wedged" for c, _ in rec["events"]) and not sh.restarts
      and any("would restart" in m for m in rec["log"]))

# W9 sign (no DEVICE_ID) -> pat-sig-stream is the unit
ctx, rec = mkctx(unit_exists=lambda s: s == "pat-sig-stream")
ctx.cfg.device_id = ""
check("W9 sign -> unit pat-sig-stream", StreamSocketHealer()._unit(ctx) == "pat-sig-stream")
ctx, rec = mkctx(unit_exists=lambda s: False)
ctx.cfg.device_id = ""
check("W9 node with neither unit -> no target", StreamSocketHealer()._unit(ctx) is None)

# W10 ss parsing: the real sign line
_ctx, _ = mkctx(sh=lambda c, timeout=15: (0, "State Recv-Q Send-Q\n0 226885 192.168.225.51:42514 122.154.116.228:1935\n\t cubic rto:120000 backoff:9 bytes_sent:11358898490 bytes_acked:11352306180 unacked:67", ""))
_s = StreamSocketHealer()._socket(_ctx)
check("W10 ss parse: acked/backoff/unacked/sendq/peer", _s and _s["acked"] == 11352306180 and _s["backoff"] == 9
      and _s["unacked"] == 67 and _s["sendq"] == 226885 and _s["peer"].endswith(":1935"))
check("W10 ss parse: flow key local>peer", _s and _s["key"] == "192.168.225.51:42514>122.154.116.228:1935")

# --- v537: the PIT043 false wedge (2026-09-24 13:20, acked_delta=-6445423813, backoff 0) and its kin ---
# W11 reconnect inside the window: the flow changes (new local port), the counter restarts small.
# v536 compared 6.4 GB on the old flow with 1 MB on the new one and restarted a healthy unit.
def _ss_flow(acked, port, **kw):
    def sh(c, timeout=15):
        if "dport = :1935" in c:
            return (0, "Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
                       "0 %d 192.168.1.128:%d 122.154.116.228:1935\n"
                       "\t cubic rto:284 backoff:%d bytes_sent:1 bytes_acked:%d unacked:%d lastack:%d"
                       % (kw.get("sendq", 30616), port, kw.get("backoff", 0), acked, kw.get("unacked", 23), kw.get("lastack", 20)), "")
        if "systemctl restart" in c:
            sh.restarts.append(c); return (0, "", "")
        return (0, "", "")
    sh.restarts = []
    return sh

def _flow_ctx(seq, **over):
    """seq = [(bytes_acked, local_port), ...] one per tick, 65 s apart."""
    import pat_fleet_healer.healers.stream_socket as _m
    ctx, rec = mkctx(env={"RTMP_URL": "rtmp://rtmp.example/CCTVApp/STREAM-1"}, **over)
    ctx.cfg.wedge_window_s = 300; ctx.cfg.wedge_min_bytes = 100000
    t0 = [1_790_000_000.0]; _o = _m.time.time; _m.time.time = lambda: t0[0]
    h = StreamSocketHealer(); shs = []
    try:
        for a, p in seq:
            sh = _ss_flow(a, p); ctx._svc["sh"] = sh; shs.append(sh); h.run(ctx); t0[0] += 65
    finally:
        _m.time.time = _o
    return ctx, rec, shs

_seq = [(6_445_000_000 + 1_000_000 * i, 48100) for i in range(4)] + [(1_000_000 * i, 48188) for i in range(1, 5)]
ctx, rec, shs = _flow_ctx(_seq)
check("W11 reconnect mid-window (new flow, small counter) -> NOT a wedge, no restart",
      not rec["events"] and not any(s.restarts for s in shs))
check("W11 samples restart on the new flow (keyed)", all(len(s) == 3 for s in rec["saved"]["samples"]) and
      all(s[2].startswith("192.168.1.128:48188>") for s in rec["saved"]["samples"]))

# W12 same flow, counter goes backwards (kernel/ss anomaly), then grows healthily from the new
# baseline -> never a wedge (v536 saw 4 MB - 9.5 MB < 0 -> "stall")
ctx, rec, shs = _flow_ctx([(9_000_000, 48100), (9_500_000, 48100), (100, 48100), (1_000_100, 48100), (2_000_100, 48100), (3_000_100, 48100), (4_000_100, 48100)])
check("W12 backwards counter on the same flow -> baseline restarts, no event, no restart", not rec["events"] and not any(s.restarts for s in shs))

# W13 idle encoder: frozen counter but nothing unacked and an empty queue -> not a wedge (camera healers' job)
ctx, rec, shs = _wedge_ctx([500] * 7)                       # _ss_1935 defaults: unacked 0, sendq 0
check("W13 idle socket (unacked 0, sendq 0) -> silent, no restart", not rec["events"] and not any(s.restarts for s in shs))

# W14 two flows: a lingering black-holed one (huge counter, lastack 8 min ago, backoff 9) and the
# live reconnect (small counter, acked 20 ms ago) -> the live one is judged -> healthy -> silent
def _two_flows(live_acked):
    def sh(c, timeout=15):
        if "dport = :1935" in c:
            return (0, "Recv-Q Send-Q Local Address:Port Peer Address:Port\n"
                       "0 226885 192.168.1.128:48100 122.154.116.228:1935\n"
                       "\t cubic rto:120000 backoff:9 bytes_sent:1 bytes_acked:6445000000 unacked:67 lastack:480000\n"
                       "0 30616 192.168.1.128:48188 122.154.116.228:1935\n"
                       "\t cubic rto:284 backoff:0 bytes_sent:1 bytes_acked:%d unacked:23 lastack:20" % live_acked, "")
        if "systemctl restart" in c:
            sh.restarts.append(c); return (0, "", "")
        return (0, "", "")
    sh.restarts = []
    return sh
_c, _ = mkctx(sh=_two_flows(5_000_000))
_pick = StreamSocketHealer()._socket(_c)
check("W14 two flows -> the live one (smallest lastack) is picked", _pick and _pick["key"].startswith("192.168.1.128:48188>") and _pick["acked"] == 5_000_000)
import pat_fleet_healer.healers.stream_socket as _m14
_c, _r = mkctx(env={"RTMP_URL": "rtmp://rtmp.example/x"}); _c.cfg.wedge_window_s = 300; _c.cfg.wedge_min_bytes = 100000
_t = [1_790_000_000.0]; _o14 = _m14.time.time; _m14.time.time = lambda: _t[0]
_shs = []
try:
    _h = StreamSocketHealer()
    for i in range(1, 8):
        _sh = _two_flows(1_000_000 * i); _c._svc["sh"] = _sh; _shs.append(_sh); _h.run(_c); _t[0] += 65
finally:
    _m14.time.time = _o14
check("W14 lingering corpse beside a healthy flow -> silent, no restart", not _r["events"] and not any(s.restarts for s in _shs))

# W15 the PISN black hole under the new rules still fires: one flow, frozen, data stuck, backoff climbing
ctx, rec, shs = _flow_ctx([(500, 48100)] * 7)
check("W15 same flow frozen with data unacked -> stream.socket-wedged + restart",
      any(c == "stream.socket-wedged" for c, _ in rec["events"]) and sum(len(s.restarts) for s in shs) == 1)
_f = [f for c, f in rec["events"] if c == "stream.socket-wedged"][0]
check("W15 event carries acked_delta 0 and lastack_ms", _f.get("acked_delta") == 0 and "lastack_ms" in _f)

# --- E2 MQTT channel ---
def _mqctx(sockets, ticks_state=0, **over):
    calls = {"n": 0}
    def sh(c, timeout=15):
        if "dport = :1883" in c:
            return (0, "\n".join("0 0 10.0.0.2:5%d 10.0.4.80:1883" % i for i in range(sockets)), "")
        return (0, "", "")
    ctx, rec = mkctx(state={"dead_ticks": ticks_state}, sh=sh, **over)
    ctx.cfg.mqtt_dead_ticks = 5
    return ctx, rec

ctx, rec = _mqctx(sockets=1)
MqttChannelHealer().run(ctx)
check("Q1 broker socket present -> silent", not rec["events"] and not rec["restart"])

ctx, rec = _mqctx(sockets=0, ticks_state=3)
MqttChannelHealer().run(ctx)
check("Q2 no socket, 4th tick -> still counting, no restart", not rec["restart"] and rec["saved"]["dead_ticks"] == 4)

ctx, rec = _mqctx(sockets=0, ticks_state=4)
MqttChannelHealer().run(ctx)
check("Q3 no socket for 5 ticks -> mqtt.channel-dead + restart radar", any(c == "mqtt.channel-dead" for c, _ in rec["events"])
      and rec["restart"] == ["pat-smart-radar"] and rec["saved"]["dead_ticks"] == 0)

ctx, rec = _mqctx(sockets=0, ticks_state=4, tcp_up=lambda *a, **k: False)
MqttChannelHealer().run(ctx)
check("Q4 WAN down -> the connectivity healer's problem, not ours", not rec["events"] and not rec["restart"])

ctx, rec = _mqctx(sockets=0, ticks_state=4, rate_ok=lambda n: False)
MqttChannelHealer().run(ctx)
check("Q5 rate exceeded -> escalate, no restart", esc_has(rec, "channel-restart-rate-exceeded") and not rec["restart"])

ctx, rec = _mqctx(sockets=0, ticks_state=4, unit_exists=lambda s: s == "pat-sig")
ctx.cfg.device_id = ""
MqttChannelHealer().run(ctx)
check("Q6 sign -> restarts pat-sig", rec["restart"] == ["pat-sig"])

ctx, rec = mkctx(state={"dead_ticks": 4}, sh=lambda c, timeout=15: (1, "", "no ss"))
MqttChannelHealer().run(ctx)
check("Q7 broken probe (ss failed) -> never acts", not rec["events"] and not rec["restart"])

# --- E3 VEGAMET loop (report only) ---
# the real page shape (PIT003 2026-09-24): the label is a section heading AND the row label
_PAGE_OK = ("<html><h2>Stromeingang</h2><table><tr><th>Eingang</th><th>Wert</th><th>Einheit</th></tr>"
            "<tr><td>Stromeingang</td><td>5,296</td><td>mA</td></tr></table>HART Sensoren</html>")
_PAGE_E015 = "<html><td>Stromeingang</td><td>E 015</td><td>mA</td></html>"
_PAGE_LOW = "<html><td>Stromeingang</td><td>3,100</td><td>mA</td></html>"
_PAGE_HIGH = "<html><td>Stromeingang</td><td>21,7</td><td>mA</td></html>"

def _loopctx(page, neigh="REACHABLE", tcp=True, state=None):
    ctx, rec = mkctx(state=state or {}, tcp_up=lambda *a, **k: tcp)
    h = VegametLoopHealer()
    h._neigh = lambda ctx, host: neigh
    h._page = lambda ctx, host: page
    return h, ctx, rec

h, ctx, rec = _loopctx(_PAGE_OK)
h.run(ctx)
check("L1 in-band current, first sight -> no event (ok is the quiet state)", not rec["events"] and rec["saved"]["verdict"] == "ok")

h, ctx, rec = _loopctx(_PAGE_E015)
h.run(ctx)
check("L2 E 015 -> radar.loop-open with the code", any(c == "radar.loop-open" and f.get("err") == "E015" for c, f in rec["events"]))
check("L2 -> no restart, no escalate (report only)", not rec["restart"] and not rec["escalate"])

h, ctx, rec = _loopctx(_PAGE_E015, state={"verdict": "loop-open", "reported": True})
h.run(ctx)
check("L3 same verdict next tick -> silent (transition only)", not rec["events"])

h, ctx, rec = _loopctx(_PAGE_OK, state={"verdict": "loop-open", "reported": True})
h.run(ctx)
check("L4 back in band -> radar.loop-ok", any(c == "radar.loop-ok" for c, _ in rec["events"]))

h, ctx, rec = _loopctx(_PAGE_LOW)
h.run(ctx)
check("L5 3.1 mA -> loop-open", any(c == "radar.loop-open" and f.get("ma") == 3.1 for c, f in rec["events"]))
h, ctx, rec = _loopctx(_PAGE_HIGH)
h.run(ctx)
check("L6 21.7 mA -> loop-over", any(c == "radar.loop-over" for c, _ in rec["events"]))

h, ctx, rec = _loopctx(None, neigh="INCOMPLETE", tcp=False)
h.run(ctx)
check("L7 ARP INCOMPLETE + :502 closed -> vegamet-off-network", any(c == "radar.vegamet-off-network" for c, _ in rec["events"]))

h, ctx, rec = _loopctx(None, neigh="REACHABLE")
h.run(ctx)
check("L8 page unreadable this tick -> say nothing", not rec["events"] and rec["saved"] is None)

h, ctx, rec = _loopctx("<html><td>Stromeingang</td><td>E 042</td><td>mA</td></html>")
h.run(ctx)
check("L9 other E-code -> vegamet-error", any(c == "radar.vegamet-error" and f.get("err") == "E042" for c, f in rec["events"]))

check("L10 the real PIT043 page text parses to E 015",
      VegametLoopHealer._input_cell(" Eingänge Stromeingang Eingang Wert Einheit Stromeingang E 015 mA HART Sensoren ") == "E 015")
_PIT003 = (" Eing&auml;nge Eing&auml;nge vom: 24.09.26 05:34:20 Seite aktualisieren Stromeingang Eingang Wert Einheit "
           "Stromeingang 7,044 mA HART Sensoren Sensor Adresse Seriennr. Wert Einheit HART-Sensor 0 - 0.000 - ")
check("L11 the real PIT003 page text parses to the loop current, not the date or the HART row",
      VegametLoopHealer._input_cell(_PIT003) == "7,044")
h, ctx, rec = _loopctx("<html>" + _PIT003 + "</html>")
h.run(ctx)
check("L11 -> in band, quiet, verdict ok", not rec["events"] and rec["saved"]["verdict"] == "ok")
check("L12 no current-input row and no 'n,nnn mA' anywhere (the real HART row has no unit) -> cell is None",
      VegametLoopHealer._input_cell(" Eingänge HART Sensoren HART-Sensor 0 - 0.000 - ") is None)

# --- v537: the controllers are not all German. PIT002 (2026-09-24) was a false radar.loop-unreadable.
_PIT002 = (" Inputs Inputs from: 24/09/26 07:46:01 reload page current input input reading dimension "
           "current input 16,106 mA HART input sensor address serialno. reading dimension digital input ")
check("L13 the real PIT002 page (English) parses to the loop current", VegametLoopHealer._input_cell(_PIT002) == "16,106")
h, ctx, rec = _loopctx("<html>" + _PIT002 + "</html>")
h.run(ctx)
check("L13 -> in band, quiet, verdict ok (no false loop-unreadable)", not rec["events"] and rec["saved"]["verdict"] == "ok")
h, ctx, rec = _loopctx("<html><td>current input</td><td>E 015</td><td>mA</td></html>")
h.run(ctx)
check("L14 English page with E 015 -> radar.loop-open err=E015", any(c == "radar.loop-open" and f.get("err") == "E015" for c, f in rec["events"]))
check("L15 a third language falls back to the workers' rule (first 'n,nnn mA')",
      VegametLoopHealer._input_cell(" Entrées entrée courant 7,044 mA HART ") == "7,044")

# --- E4 boot + dropler ---
h = NodeBootHealer(); h._boot_id = staticmethod(lambda: "abc-123"); h._uptime = staticmethod(lambda: 42.0)
ctx, rec = mkctx()
h.run(ctx)
check("B1 first sight of a boot id -> node.boot with uptime", any(c == "node.boot" and f.get("uptime_s") == 42 for c, f in rec["events"]))
ctx, rec = mkctx(state={"boot_id": "abc-123"})
h.run(ctx)
check("B2 same boot id -> silent", not rec["events"])
ctx, rec = mkctx(state={"boot_id": "old-999"})
h.run(ctx)
check("B3 new boot id -> node.boot names the previous boot", any(c == "node.boot" and f.get("prev") == "old-999" for c, f in rec["events"]))

d = DroplerFittedHealer()
d._serial_devices = staticmethod(lambda: [])
ctx, rec = mkctx(env={"MODE": "FULL"})
d.run(ctx)
check("D1 FULL without ttyUSB -> dropler.not-fitted once", [c for c, _ in rec["events"]] == ["dropler.not-fitted"])
ctx, rec = mkctx(env={"MODE": "FULL"}, state={"missing": True, "boot": ""})
d.run(ctx)
check("D2 already reported this boot -> silent", not rec["events"])
ctx, rec = mkctx(env={"MODE": "RADAR"})
d.run(ctx)
check("D3 RADAR node -> silent", not rec["events"])
d._serial_devices = staticmethod(lambda: ["/dev/ttyUSB0"])
ctx, rec = mkctx(env={"MODE": "FULL"}, state={"missing": True, "boot": ""})
d.run(ctx)
check("D4 adapter appears -> dropler.fitted", any(c == "dropler.fitted" for c, _ in rec["events"]))

# --- heartbeat carries the worker identity ---
_wd = tempfile.mkdtemp(); _TMP.append(_wd)
os.makedirs(os.path.join(_wd, ".good"))
open(os.path.join(_wd, "WORKERS_VERSION"), "w").write("2\n"); open(os.path.join(_wd, "BUILD"), "w").write("2+gce84269\n")
open(os.path.join(_wd, ".good", "WORKERS_VERSION"), "w").write("2\n")
ctx, rec = mkctx()
ctx.cfg.workers_dir = _wd
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[])
check("H-workers heartbeat carries workers/workers_build/workers_good",
      rec["hb"] and rec["hb"][-1].get("workers") == "2" and rec["hb"][-1].get("workers_build") == "2+gce84269" and rec["hb"][-1].get("workers_good") == "2")
ctx, rec = mkctx()
ctx.cfg.workers_dir = tempfile.mkdtemp(); _TMP.append(ctx.cfg.workers_dir)
runner.run(cfg=ctx.cfg, ctx=ctx, registry=[])
check("H-workers no identity files -> fields absent, not empty", rec["hb"] and "workers" not in rec["hb"][-1])

# --- manifest covers every new code ---
_new = ["stream.socket-wedged", "stream-socket.socket-wedge-persists", "stream-socket.socket-restart-failed",
        "mqtt.channel-dead", "mqtt-channel.channel-restart-rate-exceeded", "mqtt-channel.channel-restart-failed",
        "radar.vegamet-off-network", "radar.loop-open", "radar.loop-over", "radar.loop-unreadable", "radar.vegamet-error",
        "radar.loop-ok", "node.boot", "dropler.not-fitted", "dropler.fitted"]
from pat_fleet_healer import events_schema as _SCH2
check("E-manifest decodes every v536 code", all(c in _SCH2.CODES for c in _new))
check("E-manifest: physical causes are report-only sev (error) with a field fix",
      all("on-site" in _SCH2.CODES[c]["fix"] for c in ("radar.vegamet-off-network", "radar.loop-open")))


# ===========================================================================
# v538 (2026-09-25) - selftest never publishes, camera verdicts on change, NAMUR loop limits
# Found by the 2026-09-25 validation of the status record: update selftests posted node.boot under
# real ids and a phantom node "SELFTEST"; camera-absent went out every tick; PIR010 at 3.799 mA was
# called loop-open.
# ===========================================================================
import socket as _socket
import subprocess as _sp
import threading as _th
from pat_fleet_healer.core import mqtt as _mq

# V1 the publisher switch: HEALER_NO_PUSH=1 -> False, and no socket is ever opened
_calls = []
_orig_cc = _mq.socket.create_connection
_mq.socket.create_connection = lambda *a, **k: (_calls.append(a), (_ for _ in ()).throw(OSError("blocked")))[1]
try:
    os.environ["HEALER_NO_PUSH"] = "1"
    _r = _mq.publish("127.0.0.1", 1, "fleet/events/X", "{}", "X")
finally:
    os.environ.pop("HEALER_NO_PUSH", None)
    _mq.socket.create_connection = _orig_cc
check("V1 HEALER_NO_PUSH=1 -> publish returns False", _r is False)
check("V1 HEALER_NO_PUSH=1 -> no connection attempted at all", _calls == [])

def _listener():
    s = _socket.socket(); s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0)); s.listen(64)
    hits = []                     # MQTT CONNECTs only: net_probe's centre probe is a bare TCP connect
    def acc():                    # to the broker address every tick (a measurement, not a publish)
        while True:
            try:
                c, _ = s.accept()
            except OSError:
                return
            try:
                c.settimeout(2.0)
                if c.recv(1) == b"":        # MQTT 3.1.1 fixed header of CONNECT
                    hits.append(1)
            except OSError:
                pass
            finally:
                c.close()
    _th.Thread(target=acc, daemon=True).start()
    return s, s.getsockname()[1], hits

# V2 control: the listener does see a real publish attempt (so V3's zero means something)
_srv, _port, _hits = _listener()
_mq.publish("127.0.0.1", _port, "fleet/events/X", "{}", "X", timeout=2.0)
time.sleep(0.3)
check("V2 control: a normal publish reaches the test listener as an MQTT CONNECT", len(_hits) >= 1)
_srv.close()

# V3 end to end: `healer selftest` with a node .env pointing MQTT at the listener -> ZERO connections
if os.name == "posix":
    _srv, _port, _hits = _listener()
    _envd = tempfile.mkdtemp(); _TMP.append(_envd)
    _envf = os.path.join(_envd, ".env")
    open(_envf, "w").write("DEVICE_ID=PAT-TEST-SELF\nMQTT_HOST=127.0.0.1\nMQTT_PORT=%d\nMODE=RADAR\n" % _port)
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _e = dict(os.environ, HEALER_ENV_PATH=_envf, MQTT_HOST="127.0.0.1", MQTT_PORT=str(_port))
    _e.pop("HEALER_NO_PUSH", None)
    try:
        _p = _sp.run([sys.executable, "-m", "pat_fleet_healer", "selftest"], cwd=_root, env=_e,
                     stdout=_sp.PIPE, stderr=_sp.STDOUT, universal_newlines=True, timeout=240)
        _out, _rc = _p.stdout, _p.returncode
    except _sp.TimeoutExpired:
        _out, _rc = "TIMEOUT", -1
    time.sleep(0.3)
    check("V3 selftest still passes (exit 0, 'selftest OK')", _rc == 0 and "selftest OK" in _out)
    check("V3 selftest sends ZERO MQTT CONNECTs to the configured broker", len(_hits) == 0)
    _srv.close()
    # V3c control: the SAME dry tick without the selftest switch does publish (= the pre-v538 selftest)
    _srv, _port, _hits = _listener()
    open(_envf, "w").write("DEVICE_ID=PAT-TEST-SELF\nMQTT_HOST=127.0.0.1\nMQTT_PORT=%d\nMODE=RADAR\n" % _port)
    _e = dict(os.environ, HEALER_ENV_PATH=_envf, MQTT_HOST="127.0.0.1", MQTT_PORT=str(_port),
              HEALER_DRY_RUN="1", HEALER_STATE_DIR=tempfile.mkdtemp())
    _TMP.append(_e["HEALER_STATE_DIR"]); _e.pop("HEALER_NO_PUSH", None)
    try:
        _sp.run([sys.executable, "-c", "from pat_fleet_healer.runner import main; main()"], cwd=_root, env=_e,
                stdout=_sp.PIPE, stderr=_sp.STDOUT, universal_newlines=True, timeout=240)
    except _sp.TimeoutExpired:
        pass
    time.sleep(0.3)
    check("V3c control: a plain dry tick DOES publish (so V3's zero is the fix, not a quiet tick)", len(_hits) >= 1)
    _srv.close()
else:
    print("V3 skipped: POSIX only (run the suite on a node)")

# V4 camera verdicts: once, not every tick; again when the verdict changes; hourly reminder; ok on recovery
def _camctx(state=None, pushing=False):
    return mkctx(state=state, env={"RTSP_URL": "rtsp://admin:x@192.168.1.99:554/s"},
                 svc_active=lambda s: True, svc_age=lambda s: 999,
                 estab_1935=(lambda: 1) if pushing else (lambda: 0), tcp_up=lambda *a, **k: False)
ctx, rec = _camctx()
h = StreamCameraHealer(); h._scan_554 = lambda ctx: []
h.run(ctx); h.run(ctx); h.run(ctx)
check("V4 camera-absent over 3 ticks -> escalated ONCE", [v for _, v in rec["escalate"]].count("camera-absent") == 1)
check("V4 ... and still no restart", len(rec["restart"]) == 0)
h._say(ctx, "camera-path-unknown", {"ip": "192.168.1.99"})
h._say(ctx, "camera-path-unknown", {"ip": "192.168.1.99"})
check("V4 a DIFFERENT verdict goes out at once, then is quiet too",
      [v for _, v in rec["escalate"]] == ["camera-absent", "camera-path-unknown"])
ctx, rec = _camctx(state={"v": "camera-absent", "t": time.time() - 3700})
h = StreamCameraHealer(); h._scan_554 = lambda ctx: []
h.run(ctx)
check("V4 the same verdict is re-sent after an hour (reminder)", esc_has(rec, "camera-absent"))
ctx, rec = _camctx(state={"v": "camera-absent", "t": time.time()}, pushing=True)
h = StreamCameraHealer()
h.run(ctx); h.run(ctx)
_ok = [f for c, f in rec["events"] if c == "stream.camera-ok"]
check("V4 stream pushing again -> stream.camera-ok once, naming the cleared verdict", len(_ok) == 1 and _ok[0].get("was") == "camera-absent")
ctx, rec = _camctx(pushing=True)
StreamCameraHealer().run(ctx)
check("V4 healthy stream with nothing outstanding -> silent", not rec["events"] and not rec["escalate"])
ctx, rec = mkctx(env={"RTSP_URL": "rtsp://admin:x@192.168.1.99:554/s"}, estab_1935=lambda: 0, rate_ok=lambda n: False)
h = StreamCameraHealer(); h.run(ctx); h.run(ctx)
check("V4 repair-rate-exceeded also goes out once, not every tick",
      [v for _, v in rec["escalate"]].count("stream-repair-rate-exceeded") == 1)

# V5 NAMUR NE43: 3.8-4.0 and 20.0-20.5 are measurements (dry / full scale), <= 3.6 and >= 21.0 are faults
def _pg(v):
    return "<html><td>Stromeingang</td><td>%s</td><td>mA</td></html>" % v
for _v, _want in (("3,799", "ok"), ("3,610", "ok"), ("3,550", "loop-open"), ("3,600", "loop-open"),
                  ("20,010", "ok"), ("20,900", "ok"), ("21,000", "loop-over"), ("21,700", "loop-over")):
    h, ctx, rec = _loopctx(_pg(_v))
    h.run(ctx)
    _got = (rec["saved"] or {}).get("verdict")
    check("V5 %s mA -> %s" % (_v, _want), _got == _want)

# V6 manifest + version
from pat_fleet_healer import events_schema as _SCH3
from pat_fleet_healer import __version__ as _V
check("V6 manifest has stream.camera-ok (info)", _SCH3.CODES.get("stream.camera-ok", {}).get("sev") == "info")
check("V6 manifest loop-over names the NE43 limit", "21.0" in _SCH3.CODES["radar.loop-over"]["desc"])
check("V6 version 538", _V == "538")

# ---------------------------------------------------------------------------
for d in _TMP:
    shutil.rmtree(d, ignore_errors=True)
print("=== UNIT TEST RESULTS (modular) ===")
for n, ok in R:
    print("%-58s %s" % (n, "PASS" if ok else "FAIL"))
print("TOTAL: %d/%d PASS" % (sum(1 for _, ok in R if ok), len(R)))
sys.exit(0 if all(ok for _, ok in R) else 1)
