#!/usr/bin/env python3
"""pat-fleet-healer - node-local self-healing agent (ADR-037).

Runs as a systemd oneshot (~every 60s). Detects + remediates in-node faults with
graduated, least-invasive-first remediation and hard guardrails.

SAFETY INVARIANTS (do not weaken):
  - NEVER touches netbird (no netbird in any remediation path).
  - NEVER reboots the node (self-preservation): L5 -> escalate, not reboot.
  - ConnectivityHealer is DETECT+ESCALATE only (does not reboot Robustel here;
    the Robustel's own emergency_reboot handles WAN recovery).
  - Startup grace + per-healer rate-limit -> escalate (distinguishes a recoverable
    glitch from a genuine hardware fault instead of restart-looping).
  - Backup before any .env edit. Identity-gate to own DEVICE_ID.
  - HEALER_DRY_RUN=1 -> log intended actions, change nothing.
"""
import os, sys, json, time, subprocess, socket, re, glob

ENV_PATH  = os.environ.get("HEALER_ENV_PATH") or os.path.expanduser("~/.config/pat-smart/.env")  # override = testability (unset on node)
STATE_DIR = os.path.expanduser("~/.local/state/pat-smart")
RATE_FILE = os.path.join(STATE_DIR, "healer-rate.json")
LOG_FILE  = os.path.join(STATE_DIR, "healer.log")
DRY_RUN   = os.environ.get("HEALER_DRY_RUN", "0") == "1"
GRACE_S   = int(os.environ.get("HEALER_GRACE_S", "120"))   # don't act on a service younger than this (env override for tests)
RATE_WIN  = 1800         # rate-limit window (s)
RATE_MAX  = 3            # max remediations per window per healer -> else escalate
WORKERS_DIR        = os.path.expanduser("~/.config/pat-smart/workers")
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "90"))   # keep 90d = DB-safety margin (user 2026-06-17 opt-B): data reaches central DB long before purge; no node->DB coupling / no server cred on field nodes
BAK_RETENTION_DAYS = int(os.environ.get("BAK_RETENTION_DAYS", "30"))   # keep worker backups 30d

def load_env():
    d = {}
    try:
        for ln in open(ENV_PATH):
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            d[k] = v.strip().strip("'").strip('"')
    except Exception:
        pass
    return d

ENV         = load_env()
DEVICE_ID   = ENV.get("DEVICE_ID", "")
MODBUS_HOST = ENV.get("HOST") or ENV.get("MODBUS_HOST") or "192.168.1.106"  # radar.py reads env HOST (default .106) -> match it so the healer checks the RIGHT sensor IP
MQTT_HOST   = ENV.get("MQTT_HOST", "localhost")

def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    line = "%s [healer] %s%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "DRY " if DRY_RUN else "", msg)
    print(line, flush=True)
    try:
        open(LOG_FILE, "a").write(line + "\n")
    except Exception:
        pass

def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 124, "", str(e)

def svc_active(s):
    return sh("systemctl is-active %s" % s)[1] == "active"

def unit_exists(s):
    return bool(sh("systemctl list-unit-files %s.service --no-legend 2>/dev/null" % s)[1])

def svc_age(s):
    rc, o, _ = sh("systemctl show -p ActiveEnterTimestampMonotonic --value %s" % s)
    try:
        mono = int(o) / 1e6
        up = float(open("/proc/uptime").read().split()[0])
        return up - mono if mono > 0 else 1e9
    except Exception:
        return 1e9

def tcp_up(host, port, t=3):
    try:
        s = socket.create_connection((host, int(port)), t); s.close(); return True
    except Exception:
        return False

def estab_1935():
    rc, o, _ = sh("ss -tn state established 2>/dev/null | grep -c :1935")
    try: return int(o)
    except Exception: return 0

def journal(unit, n=20):
    return sh("journalctl -u %s -n %d --no-pager 2>/dev/null" % (unit, n))[1]

def online_recent(unit, mins=3):
    rc, o, _ = sh("journalctl -u %s --since '-%d min' --no-pager 2>/dev/null | grep -cE 'ONLINE|\"level\"'" % (unit, mins))
    try: return int(o) > 0
    except Exception: return False

# ---- rate limiter (persisted) ----
def _rates():
    try: return json.load(open(RATE_FILE))
    except Exception: return {}
def rate_ok(name):
    now = time.time(); st = _rates()
    return len([t for t in st.get(name, []) if now - t < RATE_WIN]) < RATE_MAX
def rate_hit(name):
    now = time.time(); st = _rates()
    st[name] = [t for t in st.get(name, []) if now - t < 86400] + [now]
    os.makedirs(STATE_DIR, exist_ok=True); json.dump(st, open(RATE_FILE, "w"))

# ---- actuators (the ONLY things that change node state) ----
def restart(svc):
    if DRY_RUN:
        log("would restart %s" % svc); return True
    sh("sudo -n systemctl reset-failed %s" % svc)
    rc, _, e = sh("sudo -n systemctl restart %s" % svc)
    if rc != 0:
        log("restart %s FAILED rc=%d %s" % (svc, rc, e)); return False
    return True

def escalate(healer, verdict, ev=None):
    ev = ev or {}
    log("ESCALATE [%s] %s | %s" % (healer, verdict, json.dumps(ev, ensure_ascii=False)))
    payload = json.dumps({"device_id": DEVICE_ID, "healer": healer, "verdict": verdict,
                          "evidence": ev, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ")},
                         ensure_ascii=False)
    # publish to central (best-effort; the log line above is the record of truth).
    # paho ships in the pat-smart venv (radar uses it); mosquitto_pub is not installed.
    try:
        import paho.mqtt.publish as publish
        publish.single("healer/%s/escalate" % DEVICE_ID, payload=payload,
                       hostname=MQTT_HOST, port=1883, keepalive=10)
    except Exception as e:
        log("escalate-publish-skip (%r) - logged only" % e)

# ===========================================================================
# Healers  (run order = least invasive / dependency-first)
# ===========================================================================
def heal_dependency():
    """F12 - redis down -> restart redis + dependents (L2)."""
    name = "dependency"
    redis_ok = svc_active("redis-server") and sh("redis-cli -h localhost ping")[1] == "PONG"
    if redis_ok:
        return
    if not rate_ok(name):
        return escalate(name, "redis-down-rate-exceeded")
    log("redis down -> L2 restart redis + dependents")
    rate_hit(name)
    if restart("redis-server"):
        time.sleep(3)
        for dep in ("pat-smart-radar", "pat-smart-stream"):
            restart(dep)        # redis restart drops their pub/sub -> bounce dependents
    else:
        escalate(name, "redis-restart-failed")

def heal_service_liveness():
    """F11 - a pat-smart service is dead/failed -> reset-failed + restart (L1)."""
    for svc in ("pat-smart-radar", "pat-smart-stream"):
        if svc_active(svc):
            continue
        if svc_age(svc) < GRACE_S:
            continue                                   # P4 startup grace
        name = "liveness:" + svc
        if not rate_ok(name):
            escalate(name, "svc-crash-loop", {"svc": svc}); continue
        log("%s inactive -> L1 restart" % svc)
        rate_hit(name)
        if not restart(svc):
            escalate(name, "svc-restart-failed", {"svc": svc})

def heal_radar_sensor():
    """F1/F3 - radar active but stuck (circuit=open, no recent ONLINE).
    sensor reachable -> L1 restart (circuit-open backstop; rate-exceeded -> the
    VEGAMET is genuinely dead -> escalate, do not loop).
    sensor host UNREACHABLE (technician relocated/re-IP'd the level sensor) ->
    SCAN :502 and escalate 'sensor-moved' WITH the candidate IP. We deliberately
    do NOT auto-repoint a Modbus level sensor (unlike the camera): pointing at the
    wrong :502 device = wrong flood level = safety-critical (HARD RULE: verify
    field semantics before patching). A human confirms, then HOST is repointed."""
    svc = "pat-smart-radar"
    if not svc_active(svc) or svc_age(svc) < GRACE_S:
        return
    j = journal(svc, 25)
    stuck = ("circuit=open" in j) and not online_recent(svc, 3)
    if not stuck:
        return
    name = "radar"
    if not tcp_up(MODBUS_HOST, 502):
        # configured sensor gone -> relocated or dead. discover + escalate (NO auto-repoint).
        found = [ip for ip in _scan_port(502) if ip != MODBUS_HOST]
        if len(found) == 1:
            return escalate(name, "sensor-moved", {"old": MODBUS_HOST, "candidate": found[0],
                                                   "note": "verify device then set HOST=candidate (safety-critical)"})
        if len(found) == 0:
            return escalate(name, "sensor-absent", {"configured": MODBUS_HOST})
        return escalate(name, "sensor-ambiguous", {"configured": MODBUS_HOST, "found": found})
    # sensor reachable but radar stuck -> circuit-open backstop -> L1 restart
    if not rate_ok(name):
        return escalate(name, "vegamet-fault-or-stuck", {"vegamet_tcp": True})
    log("radar stuck (circuit=open, no ONLINE 3m, vegamet_tcp=True) -> L1 restart")
    rate_hit(name)
    restart(svc)

def _cam_cred():
    m = re.search(r"rtsp://([^@]+)@", ENV.get("RTSP_URL", ""))
    return m.group(1) if m else "admin:"

def _scan_port(port):
    rc, o, _ = sh(
        "for i in $(seq 2 220); do (timeout 1 bash -c \"exec 3<>/dev/tcp/192.168.1.$i/%d\" "
        "2>/dev/null && echo 192.168.1.$i) & done; wait 2>/dev/null | sort -u" % int(port), timeout=40)
    return [x for x in o.split() if x]

def _scan_554():
    return _scan_port(554)

def heal_stream_camera():
    """F4/F5/F6/F7/F9 - stream not pushing -> config repair (L3) + restart.
    quote STATION_NAME (parens bug) · re-resolve drifted/placeholder cam IP ·
    force camera H.264 · fill __CAM_IP__. cam absent -> escalate (technician)."""
    svc = "pat-smart-stream"
    if svc_age(svc) < GRACE_S and svc_active(svc):
        return
    # not broken if it is actively pushing
    if svc_active(svc) and estab_1935() > 0:
        # still enforce STATION_NAME quoting (idempotent, harmless)
        _fix_station_name_quote()
        return
    name = "stream"
    if not rate_ok(name):
        return escalate(name, "stream-repair-rate-exceeded")
    changed = _fix_station_name_quote()
    cred = _cam_cred()
    cur_ip = None
    m = re.search(r"@([0-9.]+):554", ENV.get("RTSP_URL", ""))
    if m: cur_ip = m.group(1)
    placeholder = "__CAM_IP__" in open(ENV_PATH).read()
    need_cam = placeholder or (cur_ip and not tcp_up(cur_ip, 554))
    if need_cam:
        found = _scan_554()
        if len(found) == 0:
            return escalate(name, "camera-absent", {"configured": cur_ip})
        if len(found) > 1:
            return escalate(name, "camera-ambiguous", {"found": found})
        newip = found[0]
        log("camera %s -> %s (drift/placeholder) + H.264" % (cur_ip, newip))
        _set_codec_h264(newip, cred)
        _repoint_cam(newip)
        changed = True
    if changed or not svc_active(svc) or estab_1935() == 0:
        rate_hit(name)
        restart(svc)

def _fix_station_name_quote():
    s = open(ENV_PATH).read()
    m = re.search(r"^STATION_NAME=(.*)$", s, re.M)
    if not m: return False
    val = m.group(1)
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        return False
    if not re.search(r"[()\s]", val):
        return False                                   # no special chars -> fine unquoted
    if DRY_RUN:
        log("would quote STATION_NAME"); return True
    _backup_env()
    open(ENV_PATH, "w").write(s.replace("STATION_NAME=" + val, "STATION_NAME='" + val + "'", 1))
    log("quoted STATION_NAME (parens/space bug)")
    return True

def _repoint_cam(newip):
    if DRY_RUN:
        log("would re-point RTSP -> %s" % newip); return
    _backup_env()
    s = open(ENV_PATH).read()
    s = re.sub(r"(rtsp://[^@]+@)[0-9.]+(:554)", r"\g<1>%s\g<2>" % newip, s)
    s = s.replace("__CAM_IP__", newip)
    open(ENV_PATH, "w").write(s)

def _set_codec_h264(ip, cred):
    if DRY_RUN:
        log("would set %s codec H.264" % ip); return
    b = "http://%s/ISAPI/Streaming/channels/101" % ip
    rc, cur, _ = sh("curl -sk -m8 --digest -u '%s' '%s'" % (cred, b))
    if "265" in cur or "HEVC" in cur.upper():
        new = re.sub(r"<videoCodecType>[^<]*</videoCodecType>",
                     "<videoCodecType>H.264</videoCodecType>", cur)
        open("/tmp/.cam_cfg", "w").write(new)
        sh("curl -sk -m8 --digest -u '%s' -X PUT -H 'Content-Type: application/xml' "
           "--data-binary @/tmp/.cam_cfg '%s' >/dev/null 2>&1" % (cred, b))
        log("set %s codec -> H.264" % ip)

def _backup_env():
    bak = ENV_PATH + ".bak-healer-" + time.strftime("%Y%m%d")
    if not os.path.exists(bak):
        try: sh("cp '%s' '%s'" % (ENV_PATH, bak))
        except Exception: pass

def heal_connectivity():
    """F10/F2 - 4G WAN down. DETECT + ESCALATE ONLY here (netbird safety):
    the Robustel's own emergency_reboot performs WAN recovery; we never reboot
    Robustel/netbird from the healer on this node."""
    if tcp_up("8.8.8.8", 53, 3) or tcp_up("1.1.1.1", 53, 3):
        return
    name = "connectivity"
    if rate_ok(name):
        rate_hit(name)
        escalate(name, "wan-down-detect-only", {"note": "Robustel self-reboot handles recovery"})

def heal_disk_hygiene():
    """Housekeeping (low-risk · ไม่แตะ service): purge rotated logs + stale worker
    backups เก่ากว่า retention. ลบเฉพาะไฟล์ที่ match pattern เคร่งครัด + mtime เก่า
    -> active workers/config/today's log ไม่โดน (mtime filter ป้องกัน)."""
    now = time.time()
    log_dir = ENV.get("LOG_DIR") or os.path.expanduser("~/.local/state/pat-smart/logs")
    rules = ((os.path.join(log_dir, "*.log"), LOG_RETENTION_DAYS),
             (os.path.join(WORKERS_DIR, "*.bak-*"), BAK_RETENTION_DAYS),
             (os.path.join(WORKERS_DIR, "*.bak"), BAK_RETENTION_DAYS))
    old = []
    for pat, days in rules:
        cutoff = now - days * 86400
        for f in glob.glob(pat):
            try:
                if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                    old.append(f)
            except OSError:
                pass
    if not old:
        return
    if DRY_RUN:
        log("would purge %d old file(s): %s" % (len(old), ", ".join(os.path.basename(f) for f in old[:6])))
        return
    removed = []
    for f in old:
        try:
            os.remove(f); removed.append(os.path.basename(f))
        except OSError as e:
            log("purge fail %s: %r" % (f, e))
    if removed:
        log("disk-hygiene purged %d file(s): %s" % (len(removed), ", ".join(removed[:6])))

def heal_beszel_agent():
    """beszel-agent (monitoring telemetry) inactive -> restart. Low-risk:
    monitoring only; never touches netbird/stream/radar. Restores fleet
    thermal/health visibility so a node is not a blind spot. An active-but-
    hub-not-seeing agent is a hub-side issue (not node-local) so we act ONLY on
    inactive; rate-limit -> escalate (don't loop on a wedged agent)."""
    svc = "beszel-agent"
    if not unit_exists(svc):
        return                                          # no beszel-agent on this node -> nothing to do
    if svc_active(svc):
        return                                          # active -> leave (hub-side non-report != node-local)
    if svc_age(svc) < GRACE_S:
        return                                          # just (re)started -> grace
    name = "beszel"
    if not rate_ok(name):
        return escalate(name, "beszel-agent-restart-rate-exceeded")
    log("beszel-agent inactive -> restart (restore monitoring visibility)")
    rate_hit(name)
    if not restart(svc):
        escalate(name, "beszel-agent-restart-failed")

# ===========================================================================
def main():
    if not DEVICE_ID:
        log("no DEVICE_ID in .env - abort"); return
    log("tick start device=%s dry=%s" % (DEVICE_ID, DRY_RUN))
    for h in (heal_dependency, heal_service_liveness, heal_radar_sensor,
              heal_stream_camera, heal_beszel_agent, heal_connectivity, heal_disk_hygiene):
        try:
            h()
        except Exception as e:
            log("healer %s EXC %r" % (h.__name__, e))     # one healer failing must not stop the engine
    log("tick done")

if __name__ == "__main__":
    main()
