"""Configuration + .env loading. Every tunable lives here (one place), each
env-overridable for tests and per-node tuning. Pass `overrides` (a dict) to
construct a Config without touching os.environ - used by the test suite."""
import os
import socket


class Config:
    def __init__(self, env_path=None, overrides=None):
        o = overrides if overrides is not None else os.environ
        self.env_path = env_path or o.get("HEALER_ENV_PATH") or os.path.expanduser("~/.config/pat-smart/.env")
        self.env = self._load_env(self.env_path)

        # identity / targets
        self.device_id   = self.env.get("DEVICE_ID", "")       # SENSOR identity; gates sensor/stream healers (empty on signage)
        self.hostname    = socket.gethostname() or "unknown"
        # radar.py reads env HOST (default .106) -> match it so we probe the RIGHT sensor IP
        self.modbus_host = self.env.get("HOST") or self.env.get("MODBUS_HOST") or "192.168.1.106"
        self.mqtt_host   = self.env.get("MQTT_HOST", "localhost")
        # Default 8883, NOT the conventional 1883: the fleet broker listens on 8883
        # in CLEARTEXT and 1883 is closed (measured on pit003, 2026-08-05). The 6
        # PISN nodes have no MQTT_PORT line in .env at all, so this default is
        # exactly what they will use. A bad value must not crash the tick.
        self.mqtt_port   = self._int(self.env.get("MQTT_PORT") or o.get("MQTT_PORT"), 8883)
        # uplink class: robustel (RPi5+Robustel) | ec25 (IRIV internal Quectel EC25) | none ; "auto" = detect
        self.uplink      = (self.env.get("UPLINK") or o.get("UPLINK") or "auto").lower()

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

        # phase-1 network probe: outage duration + carrier-vs-onsite discriminator.
        # Transitions are recorded every tick; this is only the telemetry cadence.
        self.probe_sample_s = int(o.get("PROBE_SAMPLE_S", "300"))
        # TCP endpoint that stands for "the centre". ICMP is filtered inbound, so a
        # handshake is the only honest reachability test. 8883 not 1883: plain MQTT
        # is closed fleet-wide (verified 2026-08-04) - see the note in core/events.
        self.probe_centre = o.get("PROBE_CENTRE", "mqtt.pattaya-smart-sanitary.com:8883")

        # uplink recovery (ec25/IRIV): EC25 has no external watchdog -> the healer resets the modem
        self.wan_down_confirm = int(o.get("WAN_DOWN_CONFIRM", "3"))   # consecutive WAN-down ticks before acting (verify-before-concluding)
        self.ec25_settle_s    = int(o.get("EC25_SETTLE_S", "45"))     # wait after modem reset before judging recovery (< ~60s tick)

    @staticmethod
    def _int(v, default):
        """Never let a malformed .env line crash the tick - fall back to default."""
        try:
            return int(str(v).strip())
        except Exception:
            return default

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

    @property
    def node_id(self):
        # telemetry identity for events/MQTT: sensor DEVICE_ID if present, else hostname
        # -> infra-only signage nodes (pisn, no sensor DEVICE_ID) stay attributable (PTY-PISN00x)
        return self.device_id or self.hostname

    def state_path(self, name):
        return os.path.join(self.state_dir, name)
