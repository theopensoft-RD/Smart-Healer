"""Logging: stdout (journald captures it) + an append-only node log file.
The log line is the record of truth (escalation MQTT is best-effort)."""
import os
import time


def log(cfg, msg):
    os.makedirs(cfg.state_dir, exist_ok=True)
    line = "%s [healer] %s%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "DRY " if cfg.dry_run else "", msg)
    print(line, flush=True)
    try:
        open(os.path.join(cfg.state_dir, "healer.log"), "a").write(line + "\n")
    except Exception:
        pass
