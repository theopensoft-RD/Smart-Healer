#!/usr/bin/env python3
"""Unit tests for pat-fleet-healer decision logic (import + monkeypatch).
No production impact: restart/escalate/scan are stubbed before any healer runs.
Covers paths that are unsafe/hard to induce live (connectivity=would drop netbird,
camera-absent, rate-limit->escalate, radar circuit-stuck recoverable vs hw)."""
import importlib.util, sys, os

# Load healer.py: env override > co-located (KB/local regression) > node path
_here = os.path.dirname(os.path.abspath(__file__))
_cand = [os.environ.get("HEALER_PATH"), os.path.join(_here, "healer.py"),
         "/home/admin/.config/pat-smart/workers/healer.py"]
_path = next((p for p in _cand if p and os.path.isfile(p)), _cand[-1])
spec = importlib.util.spec_from_file_location("healer", _path)
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)                     # safe: main() only runs under __main__

R = []
def check(name, cond): R.append((name, bool(cond)))

calls = {"escalate": [], "restart": []}
H.escalate = lambda h, v, ev=None: calls["escalate"].append((h, v))
H.restart  = lambda s: (calls["restart"].append(s) or True)
H.rate_hit = lambda n: None

def reset():
    calls["escalate"].clear(); calls["restart"].clear()

# --- T9 ConnectivityHealer: WAN down -> escalate ONLY, never restart/reboot (netbird-safe) ---
reset()
H.tcp_up = lambda *a, **k: False               # both 8.8.8.8 and 1.1.1.1 fail
H.rate_ok = lambda n: True
H.heal_connectivity()
check("T9 connectivity wan-down -> escalate fired", any("wan-down" in v for _, v in calls["escalate"]))
check("T9 connectivity -> NO restart/reboot (netbird-safe)", len(calls["restart"]) == 0)

# --- T6 StreamCameraHealer: stream up, not pushing, scan finds NO camera -> escalate camera-absent ---
reset()
H.svc_active = lambda s: True
H.svc_age    = lambda s: 999
H.estab_1935 = lambda: 0
H._fix_station_name_quote = lambda: False
H._scan_554  = lambda: []
H.tcp_up     = lambda *a, **k: False           # configured cam unreachable
H.rate_ok    = lambda n: True
H.ENV["RTSP_URL"] = "rtsp://admin:x@192.168.1.99:554/stream0"
H.heal_stream_camera()
check("T6 camera-absent -> escalate", any("camera-absent" in v for _, v in calls["escalate"]))
check("T6 camera-absent -> NO restart", len(calls["restart"]) == 0)

# --- T5b StreamCameraHealer: scan finds exactly ONE camera -> repoint+codec+restart (no escalate) ---
reset()
H.svc_active = lambda s: True; H.svc_age = lambda s: 999; H.estab_1935 = lambda: 0
H._scan_554 = lambda: ["192.168.1.150"]
H._set_codec_h264 = lambda ip, cred: calls.setdefault("codec", []).append(ip)
H._repoint_cam   = lambda ip: calls.setdefault("repoint", []).append(ip)
H.tcp_up = lambda *a, **k: False
H.rate_ok = lambda n: True
H.heal_stream_camera()
check("T5b cam-drift -> repoint to scanned cam", calls.get("repoint") == ["192.168.1.150"])
check("T5b cam-drift -> set H.264", calls.get("codec") == ["192.168.1.150"])
check("T5b cam-drift -> restart stream", "pat-smart-stream" in calls["restart"])
check("T5b cam-drift -> NO false escalate", len(calls["escalate"]) == 0)

# --- T4 RadarSensorHealer: circuit stuck + recoverable -> restart ; rate exceeded -> escalate (hw) ---
reset()
H.svc_active = lambda s: True; H.svc_age = lambda s: 999
H.journal = lambda u, n=20: "[radar] read error #99 (circuit=open): TimeoutError"
H.online_recent = lambda u, m=3: False
H.tcp_up = lambda *a, **k: True
H.rate_ok = lambda n: True
H.heal_radar_sensor()
check("T4 radar stuck (recoverable) -> restart radar", "pat-smart-radar" in calls["restart"])
reset()
H.rate_ok = lambda n: False                    # keeps re-faulting -> genuine hw
H.heal_radar_sensor()
check("T4 radar stuck (rate exceeded) -> escalate, NOT restart",
      len(calls["restart"]) == 0 and len(calls["escalate"]) >= 1)

# --- T4b RadarSensorHealer: radar ONLINE recently -> no action ---
reset()
H.svc_active = lambda s: True; H.svc_age = lambda s: 999
H.journal = lambda u, n=20: "state STARTING -> ONLINE"
H.online_recent = lambda u, m=3: True
H.rate_ok = lambda n: True
H.heal_radar_sensor()
check("T4b radar healthy -> no restart no escalate",
      len(calls["restart"]) == 0 and len(calls["escalate"]) == 0)

# --- T8 startup grace: service young -> skip even if down ---
reset()
H.svc_active = lambda s: False                 # down
H.svc_age = lambda s: 10                        # but just started (< GRACE_S)
H.rate_ok = lambda n: True
H.heal_service_liveness()
check("T8 startup-grace -> skip restart on young service", len(calls["restart"]) == 0)

# --- T8b liveness: down + old -> restart ---
reset()
H.svc_active = lambda s: (s != "pat-smart-stream")   # stream down, radar up
H.svc_age = lambda s: 999
H.rate_ok = lambda n: True
H.heal_service_liveness()
check("T8b liveness down+old -> restart stream", "pat-smart-stream" in calls["restart"])

# --- T-hygiene: purge old files, keep recent (real temp files) ---
import os, time, tempfile, shutil
_d = tempfile.mkdtemp()
_old = os.path.join(_d, "sensor_20200101.log"); open(_old, "w").write("x")
_new = os.path.join(_d, "sensor_today.log"); open(_new, "w").write("x")
os.utime(_old, (time.time() - 999*86400, time.time() - 999*86400))
H.ENV["LOG_DIR"] = _d; H.WORKERS_DIR = _d; H.DRY_RUN = False
H.heal_disk_hygiene()
check("T-hygiene old log -> purged", not os.path.exists(_old))
check("T-hygiene recent log -> kept", os.path.exists(_new))
shutil.rmtree(_d, ignore_errors=True)

# --- T-beszel: monitoring agent inactive -> restart (restore fleet visibility) ---
reset()
H.unit_exists = lambda s: True
H.svc_active  = lambda s: False
H.svc_age     = lambda s: 999
H.rate_ok     = lambda n: True
H.heal_beszel_agent()
check("T-beszel inactive -> restart beszel-agent", "beszel-agent" in calls["restart"])

# --- T-beszel: agent active -> no action (hub-side non-report is NOT node-local) ---
reset()
H.unit_exists = lambda s: True
H.svc_active  = lambda s: True
H.heal_beszel_agent()
check("T-beszel active -> no restart", len(calls["restart"]) == 0)

# --- T-beszel: unit not present on node -> no action ---
reset()
H.unit_exists = lambda s: False
H.svc_active  = lambda s: False
H.svc_age     = lambda s: 999
H.rate_ok     = lambda n: True
H.heal_beszel_agent()
check("T-beszel unit absent -> no action", len(calls["restart"]) == 0 and len(calls["escalate"]) == 0)

# --- T-sensormove: radar stuck + configured sensor gone + ONE new :502 -> escalate sensor-moved (NO restart/repoint) ---
reset()
H.svc_active    = lambda s: True; H.svc_age = lambda s: 999
H.journal       = lambda u, n=20: "[radar] read error (circuit=open): TimeoutError"
H.online_recent = lambda u, m=3: False
H.tcp_up        = lambda *a, **k: False          # configured sensor unreachable
H._scan_port    = lambda p: ["192.168.1.107"]
H.MODBUS_HOST   = "192.168.1.106"
H.rate_ok       = lambda n: True
H.heal_radar_sensor()
check("T-sensormove -> escalate sensor-moved w/ candidate", any("sensor-moved" in v for _, v in calls["escalate"]))
check("T-sensormove -> NO restart (no auto-repoint · safety-critical)", len(calls["restart"]) == 0)

# --- T-sensorabsent: radar stuck + configured sensor gone + nothing on :502 -> escalate sensor-absent ---
reset()
H.svc_active    = lambda s: True; H.svc_age = lambda s: 999
H.journal       = lambda u, n=20: "circuit=open"
H.online_recent = lambda u, m=3: False
H.tcp_up        = lambda *a, **k: False
H._scan_port    = lambda p: []
H.MODBUS_HOST   = "192.168.1.106"
H.rate_ok       = lambda n: True
H.heal_radar_sensor()
check("T-sensorabsent -> escalate sensor-absent", any("sensor-absent" in v for _, v in calls["escalate"]))
check("T-sensorabsent -> NO restart", len(calls["restart"]) == 0)

print("=== UNIT TEST RESULTS ===")
for n, ok in R:
    print("%-58s %s" % (n, "PASS" if ok else "FAIL"))
print("TOTAL: %d/%d PASS" % (sum(1 for _, ok in R if ok), len(R)))
sys.exit(0 if all(ok for _, ok in R) else 1)
