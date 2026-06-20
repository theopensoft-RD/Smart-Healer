"""healer-collect (run: `python3 healer.pyz collect`) - produce ONE compact,
self-contained diagnostic bundle for an external AI agent:

  manifest (decoder + cause/fix playbook) + recent structured events + a live
  state snapshot + version. gzip'd, sanitized (no secrets/PII -> the bundle leaves
  the system). The AI agent ingests this single file and can decode every event
  and reason about cause/fix with no prior knowledge of the fleet.

Phase A = node-local bundle. Phase B (central) aggregates these + server-layer
events (AMS / dashboard-k8s / netbird) into one cross-layer fleet bundle.
"""
import os
import time
import json
import gzip
from .. import __version__, events_schema
from ..config import Config
from ..context import production_context

# fields that must never leave the system even if they appear in event d{}
_REDACT = ("pass", "password", "token", "secret", "key", "cred")


def _sanitize_line(line):
    low = line.lower()
    if any(r in low for r in _REDACT):
        try:
            o = json.loads(line)
            d = o.get("d") or {}
            for k in list(d):
                if any(r in k.lower() for r in _REDACT):
                    d[k] = "<redacted>"
            return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            return None        # drop a line we cannot safely sanitize
    return line


def _recent_events(cfg, max_lines=3000):
    path = os.path.join(cfg.state_dir, "events.jsonl")
    try:
        lines = open(path).read().splitlines()[-max_lines:]
    except Exception:
        lines = []
    out = []
    for ln in lines:
        s = _sanitize_line(ln)
        if s:
            out.append(s)
    return out


def _state_snapshot(cfg):
    ctx = production_context(cfg)
    snap = {"device_id": cfg.device_id, "sw": __version__, "ts": int(time.time()), "services": {}}
    for s in ("pat-smart-radar", "pat-smart-stream", "redis-server", "beszel-agent"):
        snap["services"][s] = "active" if ctx.svc_active(s) else "inactive"
    try:
        snap["rtmp_estab"] = ctx.estab_1935()
    except Exception:
        pass
    return snap


def build_bundle(cfg=None, out=None):
    cfg = cfg or Config()
    bundle = {
        "bundle_version": 1,
        "generated_ts": int(time.time()),
        "node": cfg.device_id,
        "sw": __version__,
        "manifest": {"schema_version": events_schema.SCHEMA_VERSION, "codes": events_schema.CODES},
        "state": _state_snapshot(cfg),
        "events_jsonl": "\n".join(_recent_events(cfg)),     # raw JSONL -> AI splits + parses
    }
    out = out or os.path.join(cfg.state_dir, "healer-bundle-%s.json.gz" % time.strftime("%Y%m%dT%H%M%S"))
    raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode()
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with gzip.open(out, "wb") as f:
        f.write(raw)
    return out, len(raw), os.path.getsize(out)


def main():
    out, raw, comp = build_bundle()
    print("bundle %s  raw=%dB  gz=%dB  (%.1fx)" % (out, raw, comp, raw / comp if comp else 0))
