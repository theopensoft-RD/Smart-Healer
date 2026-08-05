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
    "agent.alive":   {"sev": "info",  "desc": "heartbeat (proof-of-life). d.push, when present, is the outcome of the PREVIOUS central MQTT push: 1 = a broker accepted it, 0 = it reached none; d.pfail counts consecutive failures. ABSENT means no push has ever been attempted - that is 'not measured', NOT zero",
                      "cause": "normal", "fix": "d.push=0 with a rising d.pfail = the node is healing but the centre never hears about it: check MQTT_HOST/MQTT_PORT in .env (the fleet broker is 8883 CLEARTEXT; 1883 is closed) and that the node has a WAN path"},
    "agent.log":     {"sev": "debug", "desc": "free-text action log (transitional; carries d.msg)",
                      "cause": "informational", "fix": "none"},
    "agent.exc":     {"sev": "error", "desc": "a healer raised an exception (isolated; tick continued)",
                      "cause": "bug or unexpected node state in d.healer", "fix": "inspect d.err; reproduce with HEALER_DRY_RUN=1"},
    "agent.abort":   {"sev": "warn",  "desc": "tick aborted: no DEVICE_ID in .env",
                      "cause": ".env missing/unreadable or DEVICE_ID unset", "fix": "restore ~/.config/pat-smart/.env"},
    "agent.infra-only": {"sev": "info", "desc": "identity-less node (no DEVICE_ID): ran infra healers only (connectivity/disk/beszel), gated the sensor/stream healers",
                      "cause": "node has no sensor identity (e.g. pisn signage IRIV)", "fix": "normal for signage/infra nodes; set DEVICE_ID to enable sensor healers"},
    "agent.state-unwritable": {"sev": "error", "desc": "state dir not writable: the rate limiter AND the local event log are dead. Healers still act, but UNCAPPED - a repair is never withheld because a quota record failed",
                      "cause": "state dir owned by another user (the radar/stream services run as root; whoever created the dir first owns it) or the disk is full",
                      "fix": "sudo chown admin:admin ~/.local/state/pat-smart ~/.local/state/pat-smart/logs; check df -h; then systemctl start pat-fleet-healer.service and confirm events.jsonl appears"},

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
    "stream.camera-path-unknown": {"sev": "error", "desc": "camera on :554 but brand not recognised",
                      "cause": "camera replaced with an unsupported brand; RTSP path unknown",
                      "fix": "identify the camera model, add its RTSP path to CAM_RTSP_PATH, set RTSP_URL"},
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

    # --- phase-1 network probe (measures, never remediates) ---
    "probe.wan-outage": {"sev": "info", "desc": "a WAN outage ended: d.dur seconds, and d.verdict says who was missing. onsite = the gateway stopped answering too, so the fault was below the uplink (cabinet/power/cable) - valid on every node. carrier = the gateway answered throughout AND that gateway is a SEPARATE router (d.gwk='router', i.e. the Robustel on eth0), so the fault was above it. mixed/unknown = cannot attribute. d.gwk names what was pinged: 'router' = a separate box, 'modem' = this node's OWN cellular module (the EC25 on usb0, PISN nodes), null = could not tell",
                      "cause": "normal telemetry, not a fault report",
                      "fix": "none - but WEIGHT it by d.gwk: on a 'modem' node an answering gateway only proves the module is enumerated on USB, not that the cellular network was at fault, so those outages are reported 'unknown' and must NOT be pooled with 'carrier' ones. Detail rows are in netprobe.jsonl"},

    "probe.stream-session-lost": {"sev": "info", "desc": "the RTMP session to the centre was down for d.dur seconds. d.verdict says what the INTERNET was doing meanwhile: net-ok = the internet was reachable for the whole outage, so the mobile network did NOT cause it and the fault is in the streaming path; wan = the internet was gone too; mixed/unknown = cannot attribute. d.wan_up / d.wan_down are the tick tallies behind the verdict",
                      "cause": "normal telemetry, not a fault report",
                      "fix": "none - this is the record that decides whether a stream drop belongs to the carrier at all. A run of net-ok verdicts means the drops are ours to fix, not the network's. Only recorded while pat-smart-stream is active; a stopped service reports nothing rather than a false outage"},

    "probe.centre-unreachable": {"sev": "info", "desc": "the internet was fine but the CENTRE was not reachable for d.dur seconds - a failure class distinct from a WAN outage",
                      "cause": "relay/overlay, central ingest, or upstream of the datacentre",
                      "fix": "none - this is the measurement that shows how often 'everything relays through one point' actually bites"},

    # --- connectivity (F10) — uplink-aware ---
    "connectivity.wan-down-detect-only": {"sev": "warn", "desc": "4G WAN down; robustel uplink -> detect+escalate only (Robustel self-reboots off-node; healer never reboots Robustel/netbird)",
                      "cause": "Robustel/4G uplink down", "fix": "Robustel emergency_reboot handles recovery; if persists, on-site check antenna/SIM"},
    # ec25 (IRIV internal Quectel EC25): no external watchdog -> healer resets the modem (mmcli -m any --reset)
    "connectivity.ec25-reset-failed":      {"sev": "error", "desc": "EC25 modem reset command failed",
                      "cause": "mmcli/ModemManager error or missing NOPASSWD sudoers for 'mmcli -m any --reset'", "fix": "check ModemManager.service active + sudoers; run 'sudo -n mmcli -m any --reset' by hand"},
    "connectivity.ec25-reset-no-recovery": {"sev": "error", "desc": "EC25 modem was reset but WAN still down after settle (not a soft wedge)",
                      "cause": "SIM/coverage/antenna/hardware fault", "fix": "on-site: re-seat SIM + 4G antenna, check signal/data plan; swap modem if persistent"},
    "connectivity.ec25-reset-rate-exceeded": {"sev": "warn", "desc": "EC25 modem reset rate exceeded (repeated WAN drops)",
                      "cause": "flapping 4G / marginal coverage", "fix": "check antenna/signal/data-plan; relocate antenna or consider dual-SIM"},
}


def to_json():
    """The manifest as the AI agent receives it (in the diagnostic bundle)."""
    import json
    return json.dumps({"schema_version": SCHEMA_VERSION, "codes": CODES}, ensure_ascii=False, indent=0)
