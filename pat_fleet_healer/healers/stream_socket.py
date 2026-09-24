"""E1 - the RTMP push socket is wedged: ffmpeg alive, unit active, AMS reachable, but the
TCP flow to :1935 moves no data. Seen five times on the signs in September 2026 (PISN001 x2,
PISN003 x2, PISN006) and watched live on 2026-09-23: after a 4G blip the carrier's CGNAT drops
the mapping, the kernel retransmits into a black hole, ffmpeg sits in poll() forever (4.3.9 has
no write timeout) with its encoder threads still burning CPU into a full buffer. Two flavours:
  total black hole - bytes_acked frozen, backoff climbing; the kernel gives up after ~16 min
  trickle          - a partial ACK every ~15 s resets the retransmit clock; lasts HOURS (9-10 h
                     recorded) until someone restarts the unit
Every existing watchdog is blind to it (liveness sees the unit active; the usb0 watchdog only
checks for an address; the mqtt watchdog counts reconnect lines). The one honest signal is the
socket's own byte counter: a healthy encoder acks megabytes a minute, a wedge acks kilobytes or
nothing. So: bytes_acked over the last WEDGE_WINDOW_S below WEDGE_MIN_BYTES while the unit is
active and AMS answers on :1935 -> restart the stream unit once (rate-limited; a wedged ffmpeg
ignores SIGTERM for ~90 s, so the restart is given time).
Stations (pat-smart-stream) and signs (pat-sig-stream) alike; identity not required.

v537 (2026-09-24, five false restarts in the first hour of v536, PIT043 first): the counter is only
meaningful WITHIN ONE FLOW. Samples carry the flow identity (local:port>peer:port); a reconnect
starts a new baseline; a counter that goes backwards starts over; when several :1935 flows coexist
(the old black-holed one lingers after ffmpeg reconnects) the LIVE one - most recently acked - is
judged, so a corpse never speaks for a healthy stream; and a flow with nothing unacked and an empty
send queue is an idle encoder (camera/encoder healers' territory), not a wedged socket."""
import re
import time
from .base import Healer

_S = "stream-socket"


class StreamSocketHealer(Healer):
    name = "stream-socket"
    requires_identity = False

    def run(self, ctx):
        svc = self._unit(ctx)
        if not svc:
            return
        st = ctx.state_load(_S) or {}
        if not ctx.svc_active(svc) or ctx.svc_age(svc) < ctx.grace_s:
            return self._reset(ctx, st, "unit-not-steady")
        ams = self._ams_host(ctx)
        if ams and not ctx.tcp_up(ams, 1935):
            return self._reset(ctx, st, "ams-unreachable")   # the path is down, not the socket: F17's job
        sock = self._socket(ctx)
        if sock is None:
            return self._reset(ctx, st, "no-socket")         # nothing pushing: the supervisor / liveness handle it
        now = time.time()
        # only keyed samples (v537+) from the same flow count; a reconnect or a backwards counter
        # is a new baseline, never a comparison across sockets
        samples = [s for s in st.get("samples", []) if len(s) > 2 and now - s[0] <= ctx.cfg.wedge_window_s + 90]
        if samples and (samples[-1][2] != sock["key"] or samples[-1][1] > sock["acked"]):
            samples = []
        samples.append([now, sock["acked"], sock["key"]])
        st["samples"] = samples[-8:]
        # a restart we just did: give the new socket a clean window before judging again
        if now - st.get("restart_ts", 0) < ctx.cfg.wedge_window_s:
            return ctx.state_save(_S, st)
        span = samples[-1][0] - samples[0][0]
        if len(samples) < 3 or span < ctx.cfg.wedge_window_s * 0.8:
            return ctx.state_save(_S, st)                    # not enough history yet
        delta = samples[-1][1] - samples[0][1]
        if delta >= ctx.cfg.wedge_min_bytes:
            return ctx.state_save(_S, st)                    # data is flowing
        if sock["unacked"] == 0 and sock["sendq"] == 0:
            return ctx.state_save(_S, st)                    # nothing waiting to go out: idle encoder, not a wedged socket
        ev = {"svc": svc, "stall_s": int(span), "acked_delta": int(delta), "backoff": sock["backoff"],
              "unacked": sock["unacked"], "sendq": sock["sendq"], "lastack_ms": sock["lastack"], "peer": sock["peer"]}
        if not ctx.rate_ok(self.name):
            st["samples"] = []
            ctx.state_save(_S, st)
            return ctx.escalate(self.name, "socket-wedge-persists", ev)
        ctx.rate_hit(self.name)
        ctx.event("stream.socket-wedged", **ev)
        st["samples"] = []
        st["restart_ts"] = now
        st["wedges"] = int(st.get("wedges", 0)) + 1
        ctx.state_save(_S, st)
        if ctx.dry_run:
            return ctx.log("would restart %s (socket wedged %ds, acked +%d)" % (svc, span, delta))
        # not ctx.restart(): its 15 s shell timeout is shorter than the ~90 s a wedged ffmpeg
        # takes to die (SIGTERM ignored, then SIGKILL). The job is synchronous and rare.
        ctx.sh("sudo -n systemctl reset-failed %s" % svc)
        rc, _, err = ctx.sh("sudo -n systemctl restart %s" % svc, timeout=150)
        if rc != 0:
            ctx.escalate(self.name, "socket-restart-failed", {"svc": svc, "rc": rc, "err": (err or "")[:100]})

    # --- helpers (instance methods -> stubbable) ---
    def _unit(self, ctx):
        """The stream unit of this node kind: station worker or sign encoder; None if neither."""
        if ctx.device_id:
            return "pat-smart-stream"
        if ctx.unit_exists("pat-sig-stream"):
            return "pat-sig-stream"
        return None

    def _ams_host(self, ctx):
        m = re.search(r"rtmp://([^:/]+)", ctx.env.get("RTMP_URL", ""))
        if m:
            return m.group(1)
        # signs keep the RTMP target in the pat-sig project .env, not in the healer's; probe nothing
        return None

    def _socket(self, ctx):
        """The LIVE established :1935 flow, or None. Each flow is keyed local:port>peer:port. If
        several coexist (a reconnect leaves the old black-holed flow lingering until the kernel
        gives up), the most recently acknowledged one (smallest lastack) is the live one; with no
        lastack in the output (old ss), the largest bytes_acked wins as before."""
        rc, out, _ = ctx.sh("ss -tin state established '( dport = :1935 )' 2>/dev/null", timeout=10)
        if rc != 0 or not out:
            return None
        flows = []
        local, peer, sendq = "", "", 0
        for ln in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+(\S+)", ln)
            if m and "bytes_acked" not in ln:
                sendq, local, peer = int(m.group(2)), m.group(3), m.group(4)
                continue
            m = re.search(r"bytes_acked:(\d+)", ln)
            if not m:
                continue
            def field(k, _ln=ln):
                f = re.search(r"\b%s:(\d+)" % k, _ln)
                return int(f.group(1)) if f else 0
            flows.append({"acked": int(m.group(1)), "key": "%s>%s" % (local, peer), "peer": peer, "sendq": sendq,
                          "backoff": field("backoff"), "unacked": field("unacked"), "lastack": field("lastack")})
        if not flows:
            return None
        return min(flows, key=lambda f: (f["lastack"], -f["acked"]))

    def _reset(self, ctx, st, why):
        if st.get("samples"):
            st["samples"] = []
            ctx.state_save(_S, st)
