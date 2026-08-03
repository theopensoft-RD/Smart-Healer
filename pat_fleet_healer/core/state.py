"""Persisted state: the per-healer rate limiter + generic JSON state files
(e.g. F17's bounce-tracking). All under cfg.state_dir."""
import os
import json
import time


def _rate_file(cfg):
    return os.path.join(cfg.state_dir, "healer-rate.json")


def _rates(cfg):
    try:
        return json.load(open(_rate_file(cfg)))
    except Exception:
        return {}


def rate_ok(cfg, name):
    now = time.time()
    st = _rates(cfg)
    return len([t for t in st.get(name, []) if now - t < cfg.rate_win]) < cfg.rate_max


def rate_hit(cfg, name):
    """Record one quota use. Returns True if it reached disk.

    MUST NOT raise. Every healer calls this on the line BEFORE it acts, so a raise
    here means runner catches it as agent.exc and the remediation never happens -
    while systemd still reports the unit active. That is exactly what silently
    disabled healing on pit002/007/008/012 for months (KB rev 1138: 649 aborted
    remediations in 7 days, 0 restarts). Failing to record a quota is not a reason
    to withhold a repair - but the cap IS gone, so runner emits
    agent.state-unwritable to make that visible instead of silent.
    """
    now = time.time()
    st = _rates(cfg)
    st[name] = [t for t in st.get(name, []) if now - t < 86400] + [now]
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        json.dump(st, open(_rate_file(cfg), "w"))
        return True
    except Exception:
        return False


def writable(cfg):
    """Can state actually be persisted? Round-trip probe, not a permission guess."""
    probe = os.path.join(cfg.state_dir, ".wtest")
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)
        open(probe, "w").close()
        os.remove(probe)
        return True
    except Exception:
        return False


def load(cfg, name):
    try:
        return json.load(open(os.path.join(cfg.state_dir, name + ".json")))
    except Exception:
        return {}


def save(cfg, name, d):
    try:
        os.makedirs(cfg.state_dir, exist_ok=True)      # inside the guard: an
        json.dump(d, open(os.path.join(cfg.state_dir, name + ".json"), "w"))
    except Exception:                                  # unwritable PARENT makes
        pass                                           # makedirs itself raise
