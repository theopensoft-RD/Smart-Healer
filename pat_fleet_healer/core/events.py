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
from . import mqtt

EVENTS_FILE = "events.jsonl"
HUMAN_FILE = "healer.log"
PUSH_FILE = "push.state"                # last central-push outcome (see _record_push)
ROTATE_BYTES = 4 * 1024 * 1024          # gzip-rotate the local JSONL past 4 MB


def _record_push(cfg, ok):
    """Remember whether the last central push reached a broker, and how many have
    failed in a row. Local only - it must never itself push, or a broken uplink
    would recurse. Surfaced on the heartbeat by push_health()."""
    try:
        st = json.load(open(os.path.join(cfg.state_dir, PUSH_FILE)))
    except Exception:
        st = {}
    fail = 0 if ok else int(st.get("fail", 0) or 0) + 1
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        json.dump({"ok": bool(ok), "fail": fail, "t": int(time.time())},
                  open(os.path.join(cfg.state_dir, PUSH_FILE), "w"))
    except Exception:
        pass


def push_health(cfg):
    """Fields describing central reachability, for the heartbeat.

    Returns {} when no push has EVER been attempted - absent, not zero. A node
    that has had nothing worth pushing has not "failed to reach the centre", and
    conflating the two is the same null-vs-zero error that invalidated the first
    network report."""
    try:
        st = json.load(open(os.path.join(cfg.state_dir, PUSH_FILE)))
    except Exception:
        return {}
    out = {"push": 1 if st.get("ok") else 0}
    if st.get("fail"):
        out["pfail"] = int(st["fail"])
    return out


def _atom(cfg, code, fields):
    a = {"t": int(time.time()), "n": cfg.node_id, "e": code}
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

    # 1. canonical structured JSONL. makedirs is INSIDE the guard: an unwritable
    #    parent makes makedirs itself raise, and emit() is called from runner
    #    OUTSIDE the per-healer try -> a raise here would kill the whole tick.
    jpath = os.path.join(cfg.state_dir, EVENTS_FILE)
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
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

    # 3. central (MQTT) - notable events only, to keep the central stream small.
    #    The result is RECORDED (see _record_push): a push that silently fails is
    #    indistinguishable from "nothing to report", and that is precisely how the
    #    centre went months without receiving a single healer event.
    if push is None:
        push = sev in ("warn", "error", "escalate")
    if push:
        # mqtt.publish is written not to raise, but this guard is not redundant:
        # emit() is called from the runner OUTSIDE the per-healer try, so ANY
        # escape here kills the entire tick - every healer, not just the push.
        try:
            ok = mqtt.publish(cfg.mqtt_host, cfg.mqtt_port,
                              "fleet/events/%s" % cfg.node_id, line, cfg.node_id)
        except Exception:
            ok = False
        _record_push(cfg, ok)


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
    # carry the PREVIOUS push outcome: this is the one line that makes a dead
    # uplink to the centre visible in the local log instead of silent.
    fields = dict(fields or {})
    fields.update(push_health(cfg))
    emit(cfg, "agent.alive", fields or None, push=True)
