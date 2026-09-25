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
    "agent.alive":   {"sev": "info",  "desc": "heartbeat (proof-of-life). d.push, when present, is the outcome of the PREVIOUS central MQTT push: 1 = a broker accepted it, 0 = it reached none; d.pfail counts consecutive failures. ABSENT means no push has ever been attempted - that is 'not measured', NOT zero. d.build is the stamped build identity (version+gCOMMIT): two artifacts can carry the same version number, so d.sw alone does not identify what is running",
                      "cause": "normal", "fix": "d.push=0 with a rising d.pfail = the node is healing but the centre never hears about it: check MQTT_HOST/MQTT_PORT in .env (the overlay broker 10.0.4.80 is PLAIN on 1883; 8883 there is the real TLS listener and RESETS a plaintext publish) and that the node's NetBird overlay (wt0) is up"},
    # --- self-update (emitted by healer-selfupdate.sh writing events.jsonl directly,
    #     not through emit(); documented here so the manifest can still decode them) ---
    "healer.selfupdate.ok":       {"sev": "info", "desc": "a signed artifact was verified, self-tested and installed (d.from -> d.to). Unproven until a healthy tick clears update.pending",
                                   "cause": "normal", "fix": "none"},
    "healer.selfupdate.reject":   {"sev": "warn", "desc": "an update was refused BEFORE touching the running build (d.why: bad-signature | no-verifier | selftest-failed)",
                                   "cause": "bad-signature = tampering or a mis-signed release; no-verifier = this node cannot check signatures (missing python-cryptography), an operator problem NOT evidence of tampering",
                                   "fix": "bad-signature: re-sign and re-publish. no-verifier: install python3-cryptography on the node"},
    "healer.selfupdate.fail":     {"sev": "warn", "desc": "an update attempt failed at d.stage (download | sig | install)",
                                   "cause": "network, GitHub, or a full/read-only filesystem", "fix": "usually self-clears next run; check disk if it persists"},
    "healer.selfupdate.promote":  {"sev": "info", "desc": "d.good became the rollback target: it completed a real tick AND its sha256 matched what the updater installed",
                                   "cause": "normal", "fix": "none"},
    "healer.selfupdate.rollback": {"sev": "error", "desc": "restored .good after a failed update (d.why: installed-selftest-failed | no-healthy-tick). d.restored is the version now running",
                                   "cause": "the new build passed the pre-install gate then failed in place", "fix": "look at the build named in the preceding selfupdate.ok; do not re-publish it"},
    "healer.selfupdate.rollback-impossible": {"sev": "error", "desc": "a rollback was needed but no verified .good exists yet on this node",
                                   "cause": "no git-verified build has completed a healthy tick here yet", "fix": "the node may be stuck on a bad build - check it by hand"},
    "healer.selfupdate.foreign":  {"sev": "warn", "desc": "the running artifact is not the one the updater installed: a site hand-fix, or tampering. Left running, never promoted",
                                   "cause": "someone replaced healer.pyz by hand on site", "fix": "intended? fold the change into a release. unexpected? treat as tampering"},
    # --- worker OTA (emitted by Dashboard-Hardware ota/workers-selfupdate.sh, run by healer-selfupdate.sh's
    #     EXIT trap on every timer tick; same file-write path as the codes above). The "workers" are the
    #     station's radar.py / dropler.py / stream.sh / stream_supervisor.py; a release is dist/workers.tar.gz
    #     on Dashboard-Hardware main, signed with the same publisher key as healer.pyz. ---
    "workers.update.ok":       {"sev": "info", "desc": "a signed worker bundle was verified, self-tested with the units' python and installed (d.from -> d.to). d.changed lists the files that differ, d.restarted the per-unit restart rc (\"absent\" = unit not on this node, \"skipped-inactive\" = left stopped). d.promoted=true only for an identity-only release (nothing changed, nothing to prove)",
                                "cause": "normal", "fix": "none"},
    "workers.update.reject":   {"sev": "warn", "desc": "a worker release was refused BEFORE the running set was touched (d.why: bad-signature | no-verifier | no-pubkey | rollout-missing | rollout-version-mismatch | rollout-unreadable | build-mismatch | version-mismatch | bad-archive | incomplete-bundle | env-missing (d.key) | selftest-failed)",
                                "cause": "bad-signature = tampering or a mis-signed bundle; rollout-*/build-mismatch = dist/ half-pushed or the gate edited by hand (retries next tick); env-missing = this node's .env lacks a key the release needs; selftest-failed = the bundle does not run with this node's venv python",
                                "fix": "bad-signature: re-sign and re-publish. rollout/build: fix dist/workers.rollout.json. env-missing: add the key to .env. selftest-failed: read the [workers-update] selftest line in the journal; the release is bad for this node"},
    "workers.update.fail":     {"sev": "warn", "desc": "a worker update attempt failed at d.stage (download | sig | install | promote | restart). restart: a unit refused to restart after the new files went in; the previous files were put back (d.restored)",
                                "cause": "network/GitHub, a full or read-only filesystem, or sudo/systemctl refused", "fix": "download/sig self-clear next tick; install/promote: check disk; restart: check the per-unit NOPASSWD sudoers lines on this node"},
    "workers.update.deferred": {"sev": "info", "desc": "a newer worker release exists (d.rv) but the healer's own update is still proving; one change per node at a time",
                                "cause": "normal ordering", "fix": "none; retried next tick"},
    "workers.update.promote":  {"sev": "info", "desc": "the running worker set (version d.good) became .good, the rollback target: it came from the signed channel AND stayed healthy for the prove window (units active, NRestarts unmoved)",
                                "cause": "normal", "fix": "none"},
    "workers.update.rollback": {"sev": "error", "desc": "the running worker set was restored from .good (d.why: crash-loop = NRestarts +3 while proving | no-healthy-tick = never healthy before the deadline | restart-failed). d.restored is the version now running, d.changed the files put back, d.rc the restart results",
                                "cause": "the new worker files passed the pre-install gate then failed in place on this node", "fix": "look at the release named in the preceding workers.update.ok; do not widen its rollout; roll forward with a fixed version"},
    "workers.update.rollback-impossible": {"sev": "error", "desc": "a worker rollback was needed but no proven .good exists yet on this node (said once, not every tick)",
                                "cause": "the arming release (v1) never completed here, or .good was removed", "fix": "check the node by hand: workers dir, units, events; a later good release will re-arm it"},
    "workers.update.rollback-failed": {"sev": "error", "desc": "restoring .good failed at d.file",
                                "cause": "filesystem", "fix": "check disk and permissions in ~/.config/pat-smart/workers"},
    "workers.update.foreign":  {"sev": "warn", "desc": "the running worker set is not the one the updater installed: a site hand-fix, or tampering. Left running, never promoted (said once per foreign identity)",
                                "cause": "someone edited a worker file by hand on site", "fix": "intended? fold the change into a release. unexpected? treat as tampering"},
    "workers.rollback.manual": {"sev": "warn", "desc": "an operator ran workers-rollback.sh: the set was restored from .good (d.restored, d.changed, d.rc)",
                                "cause": "operator action", "fix": "none; record why in the case notes"},
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
    "stream.camera-ok":      {"sev": "info", "desc": "the stream is pushing again; clears the earlier verdict d.was (camera verdicts are sent on change, at most hourly while they stand)",
                      "cause": "camera back / repaired", "fix": "none"},
    "stream.camera-absent":  {"sev": "error", "desc": "stream down + no camera on LAN :554",
                      "cause": "camera unplugged / PoE water-ingress / LAN strain (physical)", "fix": "on-site: re-seat + waterproof PoE connector; strain-relief LAN"},
    "stream.camera-path-unknown": {"sev": "error", "desc": "camera on :554 but brand not recognised",
                      "cause": "camera replaced with an unsupported brand; RTSP path unknown",
                      "fix": "identify the camera model, add its RTSP path to CAM_RTSP_PATH, set RTSP_URL"},
    "stream.camera-ambiguous": {"sev": "warn", "desc": "multiple cameras on LAN :554",
                      "cause": "more than one RTSP device", "fix": "human pick correct cam IP from d.found"},

    # --- E1 stream socket wedge (v536) ---
    "stream.socket-wedged":  {"sev": "warn", "desc": "RTMP push socket wedged: unit active, AMS reachable, but bytes_acked moved < d.acked_delta B in d.stall_s s (backoff d.backoff, unacked d.unacked) -> the stream unit was restarted",
                      "cause": "after a 4G blip the carrier NAT dropped the TCP mapping; ffmpeg 4.3.9 has no write timeout and sits in poll() forever (the trickle flavour can last hours)",
                      "fix": "none - restarted automatically; if it repeats hourly the uplink is flapping (see connectivity events)"},
    "stream-socket.socket-wedge-persists": {"sev": "error", "desc": "socket wedge seen again with the restart quota spent (d.svc)",
                      "cause": "the uplink keeps dropping the mapping, or the restart is not clearing it", "fix": "check usb0/EC25 signal and the carrier path; restart the unit by hand once and watch bytes_acked"},
    "stream-socket.socket-restart-failed": {"sev": "error", "desc": "restart of d.svc after a socket wedge failed (rc d.rc)",
                      "cause": "sudoers/unit, or a process that would not die within 150 s", "fix": "systemctl status d.svc; kill the ffmpeg by hand"},

    # --- E2 MQTT channel dead (v536) ---
    "mqtt.channel-dead":     {"sev": "warn", "desc": "no ESTABLISHED socket to the broker for d.ticks ticks while d.svc was active and the WAN was up -> d.svc restarted",
                      "cause": "the client lost its connection and its reconnect loop stalled (PISN004 sat like this for 6 days: alerts never reached the sign)",
                      "fix": "none - restarted automatically; if it recurs check the broker ACL/credentials for this node"},
    "mqtt-channel.channel-restart-rate-exceeded": {"sev": "error", "desc": "MQTT channel dead again with the restart quota spent (d.svc)",
                      "cause": "the client cannot hold a session: broker refuses it, TLS/credential mismatch, or a broken client build", "fix": "journalctl -u d.svc; test a manual connection from the node"},
    "mqtt-channel.channel-restart-failed": {"sev": "error", "desc": "restart of d.svc for a dead MQTT channel failed",
                      "cause": "sudoers/unit", "fix": "manual restart; check NOPASSWD sudoers"},

    # --- E3 VEGAMET / 4-20 mA loop (v536, REPORT ONLY - nothing to restart) ---
    "radar.vegamet-off-network": {"sev": "error", "desc": "the VEGAMET at d.host is not on the LAN (ARP d.arp, :502 closed)",
                      "cause": "controller unpowered, cable/switch port, or it was re-IP'd (PIT001 2026-09-10)", "fix": "on-site: controller power + LAN; if re-IP'd set HOST"},
    "radar.loop-open":       {"sev": "error", "desc": "4-20 mA loop open: the controller reports d.err / d.ma mA (< 3.6 mA = line break) - the level shown is NOT a measurement",
                      "cause": "sensor cable/terminals open, or the VEGAPULS lost power or failed (PIT043 2026-09-17: 3.76 m -> 0.00 in four minutes, held for a week)",
                      "fix": "on-site: loop wires at the VEGAMET input and the sensor connector, then the sensor; the controller display shows the same code until fixed"},
    "radar.loop-over":       {"sev": "warn", "desc": "loop current d.ma mA at or above 21.0 mA (NAMUR NE43 failure: short or sensor fault; 20.0-20.5 is only the sensor at full scale)",
                      "cause": "sensor over-range / wiring short / wrong scaling", "fix": "on-site: check wiring and the sensor's range setting"},
    "radar.loop-unreadable": {"sev": "warn", "desc": "the controller's status page has no readable current value",
                      "cause": "page layout differs or the page is failing", "fix": "open http://<host>/ from the Pi and compare with the expected 'Stromeingang ... mA' row"},
    "radar.vegamet-error":   {"sev": "warn", "desc": "the controller shows error d.err on its current input",
                      "cause": "see the VEGAMET 391 manual for the code", "fix": "on-site check; note the code"},
    "radar.loop-ok":         {"sev": "info", "desc": "the loop is back in band (d.ma mA) after a reported fault",
                      "cause": "repaired / reconnected", "fix": "none"},

    # --- E4 node boot + fitted hardware (v536) ---
    "node.boot":             {"sev": "warn", "desc": "the node has (re)booted: first tick of boot d.boot_id, uptime d.uptime_s s (previous boot d.prev)",
                      "cause": "power loss, watchdog or a deliberate reboot", "fix": "none; the statistics service turns the heartbeat gap before this into an outage with this cause"},
    "dropler.not-fitted":    {"sev": "info", "desc": "MODE=FULL but no RS485/USB serial adapter present (d.port) - the flow sensor is not fitted, not faulted",
                      "cause": "site was configured FULL without the Doppler hardware", "fix": "set MODE=RADAR, or fit the adapter; do not count this as downtime"},
    "dropler.fitted":        {"sev": "info", "desc": "a serial adapter appeared (d.devices) after a not-fitted report",
                      "cause": "hardware fitted / re-plugged", "fix": "none"},

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
