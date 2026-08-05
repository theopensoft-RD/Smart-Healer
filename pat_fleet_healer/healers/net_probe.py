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
import socket
from .base import Healer
from ..core.events import _rotate_if_big

_S = "probe"
PROBE_FILE = "netprobe.jsonl"
UPTIME_FILE = "/proc/uptime"   # overridable so the contract is testable off-Linux


class NetProbeHealer(Healer):
    name = "net-probe"
    requires_identity = False        # infra: every node, identity or not

    def run(self, ctx):
        now = int(time.time())
        st = ctx.state_load(_S) or {}
        wan = bool(ctx.tcp_up("8.8.8.8", 53, 3) or ctx.tcp_up("1.1.1.1", 53, 3))
        dev, gw = self._route(ctx)
        # cheap 2-packet check every tick; the rich 5-packet one runs only in the sample
        gw_ok = self._ping(ctx, gw, 2)[0] if gw else None

        if not wan:
            st = self._on_down(ctx, st, now, gw_ok, dev)
        elif st.get("down_since"):
            st = self._on_recover(ctx, st, now, gw_ok, dev)

        # The centre being unreachable while the internet is fine is its OWN failure
        # class - it is the evidence for "everything relays through one point", which
        # is the report's strongest architectural argument and its least-measured one.
        cen_ok, cen_ms = self._centre(ctx)
        st = self._track_centre(ctx, st, now, cen_ok, wan)

        # Is the node's stream session to the centre actually established, and if not,
        # was the internet up at that moment? Recorded on the SAME row so the question
        # never has to be answered by lining up two logs by hand.
        st, push_n = self._track_push(ctx, st, now, wan)

        # periodic telemetry, independent of the up/down machines
        if now - int(st.get("sampled", 0)) >= ctx.cfg.probe_sample_s:
            st["sampled"] = now
            g_ok, g_rtt, g_jit, g_loss = self._ping(ctx, gw, 5) if gw else (None, None, None, None)
            n_ok, n_rtt, n_jit, n_loss = self._ping(ctx, "8.8.8.8", 5)
            row = {"k": "s", "wan": 1 if wan else 0,
                   "gw": self._tri(g_ok), "rtt_gw": g_rtt,
                   "rtt_net": n_rtt, "jit_net": n_jit, "loss_net": n_loss,
                   "ctr": self._tri(cen_ok), "rtt_ctr": cen_ms,
                   "push": push_n,
                   "up": self._uptime(), "ntp": self._tri(self._ntp(ctx)),
                   "temp": self._temp(), "dev": dev,
                   "disk": self._disk(ctx), "mem": self._mem(ctx)}
            row.update(self._netbird(ctx))
            row.update(self._counters(dev) or {"rx": None, "tx": None, "rxe": None,
                                               "txe": None, "rxd": None, "txd": None})
            self._write(ctx, row)
        ctx.state_save(_S, st)

    def _track_centre(self, ctx, st, now, cen_ok, wan):
        """Second, independent state machine: was the CENTRE reachable? Only counted
        while the WAN itself was up - otherwise it just mirrors the WAN outage."""
        if cen_ok is False and wan:
            if not st.get("ctr_since"):
                st["ctr_since"] = now
                self._write(ctx, {"k": "ctr_down", "wan": 1})
        elif st.get("ctr_since") and (cen_ok is not False or not wan):
            # close on reachable OR unmeasurable. Leaving it open when the centre
            # becomes unmeasurable would park the clock indefinitely and then report
            # one absurd duration - "end" records whether the end was confirmed.
            dur = now - int(st["ctr_since"])
            self._write(ctx, {"k": "ctr_up", "dur": dur, "wan": 1 if wan else 0,
                              "end": self._tri(cen_ok)})
            ctx.event("probe.centre-unreachable", dur=dur)
            st.pop("ctr_since", None)
        return st

    def _track_push(self, ctx, st, now, wan):
        """Track the RTMP session to the centre, and WHAT THE INTERNET WAS DOING while
        it was down.

        WHY THIS EXISTS. The node keeps two separate records: the healer's stream logs
        say "the stream broke", and this probe says "the internet went away". Deciding
        whether a given stream break belongs to the mobile network meant lining the two
        up by hand, which is guesswork at 60-second resolution. Recording both facts in
        ONE row makes the question answerable directly:
            v="net-ok" -> the internet was reachable for the WHOLE outage, so the
                          mobile network did NOT cause it - the fault is in the
                          streaming path (session, ingest, pipeline).
            v="wan"    -> the internet was gone too.
        That distinction is the difference between an argument about the carrier and an
        argument about our own software.

        Only tracked while the stream service is ACTIVE. A stopped service is not
        pushing on purpose, and counting that as an outage would fabricate a fault -
        the PISN signage nodes would otherwise report a permanent stream outage.
        Returns (state, established-count-or-None); None means "not measured".
        """
        if not ctx.svc_active("pat-smart-stream"):
            for k in ("push_since", "pw_up", "pw_down"):
                st.pop(k, None)
            return st, None                       # not measured, NOT zero

        n = ctx.estab_1935()
        if not n:
            if not st.get("push_since"):
                st.update({"push_since": now, "pw_up": 0, "pw_down": 0})
                self._write(ctx, {"k": "push_down", "wan": 1 if wan else 0})
            key = "pw_up" if wan else "pw_down"
            st[key] = int(st.get(key, 0)) + 1
        elif st.get("push_since"):
            dur = now - int(st["push_since"])
            up, down = int(st.get("pw_up", 0)), int(st.get("pw_down", 0))
            verdict = self._push_verdict(up, down)
            self._write(ctx, {"k": "push_up", "dur": dur, "wan_up": up,
                              "wan_down": down, "v": verdict})
            ctx.event("probe.stream-session-lost", dur=dur, verdict=verdict,
                      wan_up=up, wan_down=down)
            for k in ("push_since", "pw_up", "pw_down"):
                st.pop(k, None)
        return st, n

    @staticmethod
    def _push_verdict(wan_up, wan_down):
        """Who was missing while the stream session was down. Never guessed."""
        if wan_up and not wan_down:
            return "net-ok"     # internet fine throughout -> NOT the mobile network
        if wan_down and not wan_up:
            return "wan"        # the internet was gone too
        if wan_up and wan_down:
            return "mixed"
        return "unknown"

    # --- state machine ----------------------------------------------------
    def _on_down(self, ctx, st, now, gw_ok, dev=None):
        if not st.get("down_since"):
            st.update({"down_since": now, "gw_up": 0, "gw_down": 0, "gw_unk": 0,
                       "dev": dev, "gwk": self._gw_kind(dev)})
            self._write(ctx, {"k": "down", "gw": self._tri(gw_ok),
                              "dev": dev, "gwk": self._gw_kind(dev)})
        # tally what the gateway was doing WHILE the WAN was gone - this tally
        # is the whole point of the probe
        key = "gw_up" if gw_ok is True else ("gw_down" if gw_ok is False else "gw_unk")
        st[key] = int(st.get(key, 0)) + 1
        return st

    def _on_recover(self, ctx, st, now, gw_ok, dev=None):
        dur = now - int(st["down_since"])
        up, down, unk = int(st.get("gw_up", 0)), int(st.get("gw_down", 0)), int(st.get("gw_unk", 0))
        # the kind is taken from when the outage OPENED. If the node failed over to
        # a different interface mid-outage the two disagree and we can no longer say
        # what was pinged, so the verdict must not claim a side.
        gwk = st.get("gwk")
        if self._gw_kind(dev) != gwk:
            gwk = None
        verdict = self._verdict(up, down, unk, gwk)
        self._write(ctx, {"k": "up", "dur": dur, "gw_up": up, "gw_down": down,
                          "gw_unk": unk, "v": verdict,
                          "dev": st.get("dev"), "gwk": gwk})
        ctx.event("probe.wan-outage", dur=dur, verdict=verdict, gw_up=up,
                  gw_down=down, gwk=gwk)
        for k in ("down_since", "gw_up", "gw_down", "gw_unk", "dev", "gwk"):
            st.pop(k, None)
        return st

    # Interfaces on which the default gateway is this node's OWN cellular module
    # rather than a separate box (usb0 = Quectel EC25 RNDIS on the IRIV/PISN nodes).
    MODEM_IFACES = ("usb", "wwan", "wwp", "ppp")

    @classmethod
    def _gw_kind(cls, dev):
        """Is the default gateway a SEPARATE device, or this node's own modem?

        This decides what a "the gateway answered" verdict is actually worth:
          pit/pir  -> default route is eth0, gateway 192.168.1.1 = the Robustel
                      router, a separately powered box in the cabinet. Its
                      answering rules out a local power/cable/node fault.
          pisn     -> default route is usb0, gateway 192.168.225.1 = the EC25
                      modem's OWN RNDIS interface. It answers for as long as the
                      module is enumerated on USB - with no cellular registration
                      at all. Calling that "carrier" would read as evidence about
                      the network when it is nothing of the kind.
        Returns None when the interface could not be read - which must NOT be
        treated as "router".
        """
        if not dev:
            return None
        return "modem" if str(dev).lower().startswith(cls.MODEM_IFACES) else "router"

    @staticmethod
    def _verdict(up, down, unk, gwk=None):
        """Who was missing during the outage. 'unknown' when we could not tell -
        never guessed, never defaulted to a side.

        gwk gates the "carrier" answer ONLY, and it must be a positively identified
        separate router: on a modem gateway (or when we could not tell which it
        was) an answering gateway proves the module is alive, not that the fault
        was upstream. "onsite" needs no gate - a gateway that STOPPED answering is
        a local fault whichever kind it is.
        """
        if up and not down:
            return "carrier" if gwk == "router" else "unknown"
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

    def _ping(self, ctx, host, count=2):
        """(reachable, avg_ms, jitter_ms, loss_pct). All None if ping could not run.
        jitter is ping's own mdev - the number a QoS argument actually needs."""
        if not host:
            return None, None, None, None
        rc, out, _ = ctx.sh("ping -c%d -W2 -q %s 2>/dev/null" % (count, host), 8 + 2 * count)
        if "packet loss" not in out:
            return None, None, None, None          # ping unavailable/unparseable
        ok = "100% packet loss" not in out
        avg = jit = loss = None
        try:
            loss = int(out.split("% packet loss")[0].rsplit(" ", 1)[1])
        except Exception:
            loss = None
        if "min/avg/max" in out:
            try:
                parts = out.rsplit("=", 1)[1].strip().split()[0].split("/")
                avg = round(float(parts[1]), 1)
                jit = round(float(parts[3]), 1) if len(parts) > 3 else None
            except Exception:
                avg = jit = None
        return ok, avg, jit, loss

    def _centre(self, ctx):
        """TCP-connect to the central endpoint. ICMP is filtered on the way in, so
        pinging it would always look dead; a TCP handshake is the honest test."""
        target = getattr(ctx.cfg, "probe_centre", "") or ""
        if ":" not in target:
            return None, None
        host, _, port = target.rpartition(":")
        try:
            port = int(port)
        except Exception:
            return None, None
        t = time.time()
        s = socket.socket()
        s.settimeout(4)
        try:
            s.connect((host, port))
            return True, round((time.time() - t) * 1000, 1)
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False, None
        except Exception:
            return None, None                      # could not even try -> null
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _netbird(self, ctx):
        """Relay/overlay health. Every field null when it cannot be read - a node
        WITHOUT the overlay must never look like a node whose relays are DOWN.

        Uses --json, not the human output: the text layout differs between the two
        netbird versions in this fleet (0.70.5 on RPi5, 0.71.4 on IRIV).

        Measured 2026-08-04: on the 0.71.4 nodes `netbird status` intermittently
        blocks for longer than 12s - every invocation form, including with no
        timeout wrapper at all - while 0.70.5 answers in ~200ms. Cause not
        established, so it is bounded rather than worked around: 5s, then null.
        Null is the honest answer here; a fabricated zero would read as "the relays
        are down", which is the opposite of what we know.
        """
        blank = {"nb_mgmt": None, "nb_sig": None, "nb_relay_up": None,
                 "nb_relay_tot": None, "nb_peer_up": None, "nb_peer_tot": None}
        rc, out, _ = ctx.sh("timeout 5 netbird status --json 2>/dev/null", 9)
        if rc != 0 or not out.strip():
            return blank
        try:
            d = json.loads(out)
        except Exception:
            return blank
        o = dict(blank)
        o["nb_mgmt"] = self._tri(self._dig(d, "management", "connected"))
        o["nb_sig"] = self._tri(self._dig(d, "signal", "connected"))
        o["nb_relay_up"] = self._int(self._dig(d, "relays", "available"))
        o["nb_relay_tot"] = self._int(self._dig(d, "relays", "total"))
        o["nb_peer_up"] = self._int(self._dig(d, "peers", "connected"))
        o["nb_peer_tot"] = self._int(self._dig(d, "peers", "total"))
        return o

    @staticmethod
    def _dig(d, *path):
        """Fetch a nested key. Missing -> None (never a default value)."""
        for k in path:
            if not isinstance(d, dict) or k not in d:
                return None
            d = d[k]
        return d

    @staticmethod
    def _int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    def _uptime(self):
        """Seconds since boot. Lets analysis tell a REBOOT apart from a network
        outage - today those are indistinguishable in the data."""
        try:
            with open(UPTIME_FILE) as f:
                return int(float(f.read().split()[0]))
        except Exception:
            return None

    def _ntp(self, ctx):
        """Clock synced? Phase 2 asks 'did these nodes fail at the same moment' -
        that question is meaningless if the clocks disagree."""
        rc, out, _ = ctx.sh("timedatectl show -p NTPSynchronized --value 2>/dev/null", 8)
        v = out.strip().lower()
        if v in ("yes", "true", "1"):
            return True
        if v in ("no", "false", "0"):
            return False
        return None

    def _disk(self, ctx):
        rc, out, _ = ctx.sh("df -P / 2>/dev/null | tail -1", 8)
        for tok in out.split():
            if tok.endswith("%") and tok[:-1].isdigit():
                return int(tok[:-1])
        return None

    def _mem(self, ctx):
        """Percent of RAM in use."""
        try:
            tot = avail = None
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        tot = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail = int(line.split()[1])
            if tot and avail is not None:
                return int(round((tot - avail) * 100.0 / tot))
        except Exception:
            pass
        return None

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
