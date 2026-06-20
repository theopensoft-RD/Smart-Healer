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
    now = time.time()
    st = _rates(cfg)
    st[name] = [t for t in st.get(name, []) if now - t < 86400] + [now]
    os.makedirs(cfg.state_dir, exist_ok=True)
    json.dump(st, open(_rate_file(cfg), "w"))


def load(cfg, name):
    try:
        return json.load(open(os.path.join(cfg.state_dir, name + ".json")))
    except Exception:
        return {}


def save(cfg, name, d):
    os.makedirs(cfg.state_dir, exist_ok=True)
    try:
        json.dump(d, open(os.path.join(cfg.state_dir, name + ".json"), "w"))
    except Exception:
        pass
