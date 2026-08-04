"""Phase-1 network probe - it MEASURES and never remediates.

Why it exists: the fleet report (2026-08-04) can count 1,480 WAN outages but
cannot say (a) how long any of them lasted, or (b) whether the carrier went away
or the on-site equipment did. (b) is the question that decides whether a private
network is worth building at all - if the box in the cabinet is what fails, a new
radio link changes nothing.

The discriminator needs no credentials and no router access: every tick, check the
internet AND the LAN gateway (the Robustel on RPi5 nodes, the EC25's ECM gateway
on IRIV). During an outage:

    gateway answered the whole time  -> upstream/carrier   (a private link helps)
    gateway also unreachable         -> on-site equipment  (a private link does not)

RECORDING DISCIPLINE - this is load-bearing, not decoration:

    null  = could not measure
    0     = measured, and the value really is zero

These are NEVER conflated. Treating "no data" as "zero" is exactly what silently
invalidated the 2026-08-03 report: nodes whose logger had died were counted as
nodes with no problems, which pulled every average, correlation and spatial
conclusion the same wrong way. Any field this probe cannot read is written as
null so downstream analysis can tell "no problem" apart from "no instrument".

Output goes to its own file (netprobe.jsonl), NOT events.jsonl - a 5-minute
sample cadence would otherwise drown the healer's own event stream.
"""
import os
import time
import json
from .base import Healer
from ..core.events import _rotate_if_big

_S = "probe"
PROBE_FILE = "netprobe.jsonl"


class NetProbeHealer(Healer):
    name = "net-probe"
    requires_identity = False        # infra: every node, identity or not

    def run(self, ctx):
        now = int(time.time())
        st = ctx.state_load(_S) or {}
        wan = bool(ctx.tcp_up("8.8.8.8", 53, 3) or ctx.tcp_up("1.1.1.1", 53, 3))
        dev, gw = self._route(ctx)
        gw_ok, gw_rtt = self._ping(ctx, gw) if gw else (None, None)

        if not wan:
            st = self._on_down(ctx, st, now, gw_ok)
        elif st.get("down_since"):
            st = self._on_recover(ctx, st, now, gw_ok)

        # periodic telemetry, independent of the up/down machine
        if now - int(st.get("sampled", 0)) >= ctx.cfg.probe_sample_s:
            st["sampled"] = now
            self._write(ctx, {"k": "s", "wan": 1 if wan else 0,
                              "gw": self._tri(gw_ok), "rtt_gw": gw_rtt,
                              "rtt_net": self._ping(ctx, "8.8.8.8")[1],
                              "temp": self._temp(), "dev": dev,
                              **(self._counters(dev) or {"ctr": None})})
        ctx.state_save(_S, st)

    # --- state machine ----------------------------------------------------
    def _on_down(self, ctx, st, now, gw_ok):
        if not st.get("down_since"):
            st.update({"down_since": now, "gw_up": 0, "gw_down": 0, "gw_unk": 0})
            self._write(ctx, {"k": "down", "gw": self._tri(gw_ok)})
        # tally what the gateway was doing WHILE the WAN was gone - this tally
        # is the whole point of the probe
        key = "gw_up" if gw_ok is True else ("gw_down" if gw_ok is False else "gw_unk")
        st[key] = int(st.get(key, 0)) + 1
        return st

    def _on_recover(self, ctx, st, now, gw_ok):
        dur = now - int(st["down_since"])
        up, down, unk = int(st.get("gw_up", 0)), int(st.get("gw_down", 0)), int(st.get("gw_unk", 0))
        verdict = self._verdict(up, down, unk)
        self._write(ctx, {"k": "up", "dur": dur, "gw_up": up, "gw_down": down,
                          "gw_unk": unk, "v": verdict})
        ctx.event("probe.wan-outage", dur=dur, verdict=verdict, gw_up=up, gw_down=down)
        for k in ("down_since", "gw_up", "gw_down", "gw_unk"):
            st.pop(k, None)
        return st

    @staticmethod
    def _verdict(up, down, unk):
        """Who was missing during the outage. 'unknown' when we could not tell -
        never guessed, never defaulted to a side."""
        if up and not down:
            return "carrier"        # gateway fine, internet gone -> upstream
        if down and not up:
            return "onsite"         # gateway gone too -> equipment/cabinet
        if up and down:
            return "mixed"
        return "unknown"            # only unmeasurable ticks

    # --- measurements: every one returns None when it cannot be measured ---
    def _route(self, ctx):
        rc, out, _ = ctx.sh("ip route 2>/dev/null | grep -m1 '^default'", 8)
        if rc != 0 or not out.strip():
            return None, None
        f = out.split()
        gw = f[f.index("via") + 1] if "via" in f else None
        dev = f[f.index("dev") + 1] if "dev" in f else None
        return dev, gw

    def _ping(self, ctx, host):
        """(reachable, rtt_ms). (None, None) if ping itself could not run."""
        if not host:
            return None, None
        rc, out, _ = ctx.sh("ping -c2 -W2 -q %s 2>/dev/null" % host, 12)
        if "packet loss" not in out:
            return None, None                      # ping unavailable/unparseable
        ok = "100% packet loss" not in out
        rtt = None
        if "min/avg/max" in out:
            try:
                rtt = round(float(out.rsplit("=", 1)[1].strip().split("/")[1]), 1)
            except Exception:
                rtt = None
        return ok, rtt

    def _temp(self):
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                return round(int(f.read().strip()) / 1000.0, 1)
        except Exception:
            return None                            # no sensor -> null, NOT 0

    def _counters(self, dev):
        if not dev:
            return None
        base = "/sys/class/net/%s/statistics/" % dev
        out = {}
        for short, name in (("rx", "rx_bytes"), ("tx", "tx_bytes"),
                            ("rxe", "rx_errors"), ("txe", "tx_errors"),
                            ("rxd", "rx_dropped"), ("txd", "tx_dropped")):
            try:
                with open(base + name) as f:
                    out[short] = int(f.read().strip())
            except Exception:
                out[short] = None                  # unreadable counter -> null
        return out if any(v is not None for v in out.values()) else None

    @staticmethod
    def _tri(v):
        """True/False/None -> 1/0/None. Keeps 'unknown' distinct from 'down'."""
        return None if v is None else (1 if v else 0)

    # --- output -----------------------------------------------------------
    def _write(self, ctx, rec):
        rec = dict(rec)
        rec["t"] = int(time.time())
        rec["n"] = ctx.cfg.node_id
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
        path = os.path.join(ctx.cfg.state_dir, PROBE_FILE)
        try:
            os.makedirs(ctx.cfg.state_dir, exist_ok=True)
            open(path, "a").write(line + "\n")
            _rotate_if_big(path)
        except Exception:
            pass                                   # a probe must never break a tick
