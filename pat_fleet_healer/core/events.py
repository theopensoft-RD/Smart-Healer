"""Structured event emitter (the canonical log channel).

Each call writes ONE compact JSONL atom -> {t,n,e,d?} (epoch, node, code, fields).
severity/desc/cause/fix are NOT on the line; they live in events_schema (the
manifest the AI agent decodes with). Also derives a human line to journald and
pushes notable events (>=warn, or push=True) to central via MQTT.

Size: ~60-90 B/atom raw; JSONL of repeating keys/codes compresses ~10-15x (zstd).
Compaction levers: codes not prose · log on transition not every tick (see runner
heartbeat) · gzip-rotate the local file past a cap.
"""
import os
import time
import json
import gzip
import shutil
from ..events_schema import CODES

EVENTS_FILE = "events.jsonl"
HUMAN_FILE = "healer.log"
ROTATE_BYTES = 4 * 1024 * 1024          # gzip-rotate the local JSONL past 4 MB


def _atom(cfg, code, fields):
    a = {"t": int(time.time()), "n": cfg.device_id, "e": code}
    if fields:
        a["d"] = fields
    return json.dumps(a, ensure_ascii=False, separators=(",", ":"))


def _rotate_if_big(path):
    try:
        if os.path.getsize(path) < ROTATE_BYTES:
            return
        dst = path + "." + time.strftime("%Y%m%dT%H%M%S") + ".gz"
        with open(path, "rb") as fi, gzip.open(dst, "wb") as fo:
            shutil.copyfileobj(fi, fo)
        open(path, "w").close()
    except Exception:
        pass


def emit(cfg, code, fields=None, push=None):
    fields = fields or {}
    meta = CODES.get(code, {})
    sev = meta.get("sev", "info")
    line = _atom(cfg, code, fields)
    os.makedirs(cfg.state_dir, exist_ok=True)

    # 1. canonical structured JSONL
    jpath = os.path.join(cfg.state_dir, EVENTS_FILE)
    try:
        open(jpath, "a").write(line + "\n")
        _rotate_if_big(jpath)
    except Exception:
        pass

    # 2. human line (derived from the manifest) -> stdout/journald + log file
    desc = meta.get("desc", code)
    extra = (" | " + ", ".join("%s=%s" % (k, v) for k, v in fields.items())) if fields else ""
    human = "%s [healer] %s%s | %s%s" % (time.strftime("%Y-%m-%dT%H:%M:%S"),
                                         "DRY " if cfg.dry_run else "", code, desc, extra)
    print(human, flush=True)
    try:
        open(os.path.join(cfg.state_dir, HUMAN_FILE), "a").write(human + "\n")
    except Exception:
        pass

    # 3. central (MQTT) - notable events only, to keep the central stream small
    if push is None:
        push = sev in ("warn", "error", "escalate")
    if push:
        try:
            import paho.mqtt.publish as publish
            publish.single("fleet/events/%s" % cfg.device_id, payload=line,
                           hostname=cfg.mqtt_host, port=1883, keepalive=10)
        except Exception:
            pass


def heartbeat(cfg, **fields):
    """Rate-limited proof-of-life (NOT every tick -> the engine runs every ~60s but
    we only emit a heartbeat every cfg.heartbeat_s). Pushed to central."""
    f = os.path.join(cfg.state_dir, "hb.ts")
    try:
        last = float(open(f).read().strip())
    except Exception:
        last = 0
    now = time.time()
    if now - last < cfg.heartbeat_s:
        return
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        open(f, "w").write(str(now))
    except Exception:
        pass
    emit(cfg, "agent.alive", fields or None, push=True)
