"""Event manifest = the self-describing decoder + diagnostic playbook.

Every structured event carries only a compact CODE; severity / description /
likely-cause / suggested-fix live HERE (once), not on every log line. An external
AI agent reads this manifest and can interpret + reason about every event with no
prior knowledge of the system -> this is what makes the log 'AI-resolvable'.

Compaction: codes are namespaced "<domain>.<verdict>"; the line is just
{t,n,e,d?} (timestamp, node, code, optional fields). sev/desc/cause/fix are NOT
repeated per line.
"""
SCHEMA_VERSION = 1

CODES = {
    # --- agent / engine ---
    "agent.alive":   {"sev": "info",  "desc": "heartbeat (proof-of-life)",
                      "cause": "normal", "fix": "none"},
    "agent.log":     {"sev": "debug", "desc": "free-text action log (transitional; carries d.msg)",
                      "cause": "informational", "fix": "none"},
    "agent.exc":     {"sev": "error", "desc": "a healer raised an exception (isolated; tick continued)",
                      "cause": "bug or unexpected node state in d.healer", "fix": "inspect d.err; reproduce with HEALER_DRY_RUN=1"},
    "agent.abort":   {"sev": "warn",  "desc": "tick aborted: no DEVICE_ID in .env",
                      "cause": ".env missing/unreadable or DEVICE_ID unset", "fix": "restore ~/.config/pat-smart/.env"},

    # --- dependency (F12 redis) ---
    "dependency.redis-down-rate-exceeded": {"sev": "warn", "desc": "redis down + restart rate exceeded",
                      "cause": "redis crash-looping", "fix": "check redis-server journal + disk; reinstall if corrupt"},
    "dependency.redis-restart-failed":     {"sev": "error", "desc": "redis restart failed",
                      "cause": "redis package/perm/disk", "fix": "manual systemctl status redis-server"},

    # --- liveness (F11) ---
    "liveness.svc-crash-loop":   {"sev": "error", "desc": "core service crash-loop (rate exceeded)",
                      "cause": "service in d.svc crashing on start", "fix": "journalctl -u d.svc; check config/deps"},
    "liveness.svc-restart-failed": {"sev": "error", "desc": "core service restart failed",
                      "cause": "sudoers/unit/binary", "fix": "manual restart; check NOPASSWD sudoers"},

    # --- radar / sensor (F1/F3/F16) ---
    "radar.vegamet-fault-or-stuck": {"sev": "warn", "desc": "radar stuck, sensor reachable, restart rate exceeded",
                      "cause": "VEGAMET genuinely faulted/wedged", "fix": "on-site VEGAMET check; power-cycle sensor"},
    "radar.sensor-moved":  {"sev": "warn", "desc": "Modbus sensor unreachable; ONE candidate :502 found",
                      "cause": "technician relocated / re-IP'd the level sensor",
                      "fix": "VERIFY device at d.candidate is the right sensor, then set HOST=candidate (safety-critical: wrong device = wrong flood level)"},
    "radar.sensor-absent": {"sev": "error", "desc": "Modbus sensor unreachable; nothing on :502",
                      "cause": "sensor dead / unplugged / LAN down", "fix": "on-site: check sensor power + LAN"},
    "radar.sensor-ambiguous": {"sev": "warn", "desc": "configured sensor gone; MULTIPLE :502 candidates",
                      "cause": "several Modbus devices on LAN", "fix": "human disambiguate d.found, set HOST"},

    # --- stream / camera (F4-F9) ---
    "stream.stream-repair-rate-exceeded": {"sev": "warn", "desc": "stream repair rate exceeded",
                      "cause": "stream won't stay up", "fix": "check camera reachability/codec + ffmpeg journal"},
    "stream.camera-absent":  {"sev": "error", "desc": "stream down + no camera on LAN :554",
                      "cause": "camera unplugged / PoE water-ingress / LAN strain (physical)", "fix": "on-site: re-seat + waterproof PoE connector; strain-relief LAN"},
    "stream.camera-ambiguous": {"sev": "warn", "desc": "multiple cameras on LAN :554",
                      "cause": "more than one RTSP device", "fix": "human pick correct cam IP from d.found"},

    # --- stream re-publish (F17) ---
    "stream-republish.republish-rate-exceeded":       {"sev": "warn",  "desc": "F17 re-publish rate exceeded",
                      "cause": "AMS flapping or stream repeatedly wedged", "fix": "check AMS ingest health; if AMS ok, check node stream"},
    "stream-republish.republish-restart-failed":      {"sev": "error", "desc": "F17 re-publish restart failed",
                      "cause": "sudoers/unit", "fix": "manual restart pat-smart-stream"},
    "stream-republish.republish-no-rtmp-after-restart": {"sev": "error", "desc": "F17 restarted but RTMP did not re-establish",
                      "cause": "AMS unreachable from node, or camera/codec fault", "fix": "verify AMS :1935 reachable; check camera H.264 + RTSP"},

    # --- beszel (F15) ---
    "beszel.beszel-agent-restart-rate-exceeded": {"sev": "warn", "desc": "beszel-agent restart rate exceeded",
                      "cause": "agent wedged", "fix": "reinstall beszel-agent; check token/hub reach"},
    "beszel.beszel-agent-restart-failed":        {"sev": "warn", "desc": "beszel-agent restart failed",
                      "cause": "unit/binary", "fix": "manual restart beszel-agent"},

    # --- connectivity (F10) ---
    "connectivity.wan-down-detect-only": {"sev": "warn", "desc": "4G WAN down (detect+escalate only; never reboot from healer)",
                      "cause": "Robustel/4G uplink down", "fix": "Robustel self-reboot handles recovery; if persists, on-site check antenna/SIM"},
}


def to_json():
    """The manifest as the AI agent receives it (in the diagnostic bundle)."""
    import json
    return json.dumps({"schema_version": SCHEMA_VERSION, "codes": CODES}, ensure_ascii=False, indent=0)
