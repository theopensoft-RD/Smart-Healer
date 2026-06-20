"""Configuration + .env loading. Every tunable lives here (one place), each
env-overridable for tests and per-node tuning. Pass `overrides` (a dict) to
construct a Config without touching os.environ - used by the test suite."""
import os


class Config:
    def __init__(self, env_path=None, overrides=None):
        o = overrides if overrides is not None else os.environ
        self.env_path = env_path or o.get("HEALER_ENV_PATH") or os.path.expanduser("~/.config/pat-smart/.env")
        self.env = self._load_env(self.env_path)

        # identity / targets
        self.device_id   = self.env.get("DEVICE_ID", "")
        # radar.py reads env HOST (default .106) -> match it so we probe the RIGHT sensor IP
        self.modbus_host = self.env.get("HOST") or self.env.get("MODBUS_HOST") or "192.168.1.106"
        self.mqtt_host   = self.env.get("MQTT_HOST", "localhost")

        # runtime
        self.dry_run = o.get("HEALER_DRY_RUN", "0") == "1"
        self.grace_s = int(o.get("HEALER_GRACE_S", "120"))      # don't act on a service younger than this

        # paths
        self.state_dir   = o.get("HEALER_STATE_DIR") or os.path.expanduser("~/.local/state/pat-smart")
        self.workers_dir = os.path.expanduser("~/.config/pat-smart/workers")
        self.log_dir     = self.env.get("LOG_DIR") or os.path.join(self.state_dir, "logs")

        # rate limiter
        self.rate_win = 1800        # window (s)
        self.rate_max = 3           # max remediations / window / healer -> else escalate

        # structured events
        self.heartbeat_s = int(o.get("HEALER_HEARTBEAT_S", "1800"))   # proof-of-life cadence (NOT every ~60s tick)

        # disk hygiene retention (opt-B DB-safety: data reaches central DB long before purge)
        self.log_retention_days = int(o.get("LOG_RETENTION_DAYS", "90"))
        self.bak_retention_days = int(o.get("BAK_RETENTION_DAYS", "30"))

        # F17 stream re-publish
        self.ams_down_confirm   = int(o.get("AMS_DOWN_CONFIRM", "2"))      # AMS:1935 unreachable >= K ticks = a real bounce
        self.republish_settle_s = int(o.get("REPUBLISH_SETTLE_S", "30"))  # let AMS settle before re-publishing
        self.republish_spread_s = int(o.get("REPUBLISH_SPREAD_S", "150")) # stagger window across the fleet

    @staticmethod
    def _load_env(path):
        d = {}
        try:
            for ln in open(path):
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                d[k] = v.strip().strip("'").strip('"')
        except Exception:
            pass
        return d

    def state_path(self, name):
        return os.path.join(self.state_dir, name)
