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
Stations (pat-smart-stream) and signs (pat-sig-stream) alike; identity not required."""
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
        samples = [s for s in st.get("samples", []) if now - s[0] <= ctx.cfg.wedge_window_s + 90]
        samples.append([now, sock["acked"]])
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
        ev = {"svc": svc, "stall_s": int(span), "acked_delta": int(delta), "backoff": sock["backoff"],
              "unacked": sock["unacked"], "sendq": sock["sendq"], "peer": sock["peer"]}
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
        """The established :1935 flow (largest bytes_acked if several), or None."""
        rc, out, _ = ctx.sh("ss -tin state established '( dport = :1935 )' 2>/dev/null", timeout=10)
        if rc != 0 or not out:
            return None
        best = None
        peer, sendq = "", 0
        for ln in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(\d+)\s+\S+\s+(\S+)", ln)
            if m and "bytes_acked" not in ln:
                sendq, peer = int(m.group(2)), m.group(3)
                continue
            m = re.search(r"bytes_acked:(\d+)", ln)
            if not m:
                continue
            cand = {"acked": int(m.group(1)), "peer": peer, "sendq": sendq,
                    "backoff": int((re.search(r"backoff:(\d+)", ln) or [None, 0])[1] or 0),
                    "unacked": int((re.search(r"unacked:(\d+)", ln) or [None, 0])[1] or 0)}
            if best is None or cand["acked"] > best["acked"]:
                best = cand
        return best

    def _reset(self, ctx, st, why):
        if st.get("samples"):
            st["samples"] = []
            ctx.state_save(_S, st)
