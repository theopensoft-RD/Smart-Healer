"""F17 - stale broadcast status after AMS/central restart (or this node's WAN flap).
When AMS restarts, ffmpeg auto-reconnects RTMP *transparently* -> AMS keeps
INGESTING + serving HLS (video is live) but never sees a fresh 'publish-start', so
the broadcast STATUS stays stale and the dashboard shows the camera OFFLINE despite
live video. StreamCameraHealer cannot catch this (the stream IS pushing -> looks
healthy). Fix = ONE clean re-publish (restart) -> fresh publish-start -> AMS status
correct -> dashboard online.
  Triggers: (a) observed AMS:1935 down->up bounce (handles FUTURE restarts), or
            (b) deploy sentinel `republish-once` (force once to clear a CURRENT stale
                state - the bounce-detector only sees bounces it was running for).
  Guardrails: confirm-bounce (>=K ticks) · startup-grace · per-node JITTER (never
              slam a just-restarted AMS) · rate-limit -> escalate · verify RTMP re-ESTAB."""
import os
import re
import time
import hashlib
from .base import Healer


class StreamRepublishHealer(Healer):
    name = "stream-republish"

    def run(self, ctx):
        svc = "pat-smart-stream"
        ams = self._ams_host(ctx)
        if not ctx.svc_active(svc) or not ams:
            return                                          # not a streaming node / no RTMP target
        st = ctx.state_load("stream-republish")
        nowt = time.time()

        # --- bounce detection (state tracked across ticks) ---
        if not ctx.tcp_up(ams, 1935):
            st["down_ticks"] = st.get("down_ticks", 0) + 1
            return ctx.state_save("stream-republish", st)   # AMS/path down -> node side is fine, wait
        if st.get("down_ticks", 0) >= ctx.cfg.ams_down_confirm and not st.get("pending"):
            st["pending"] = True
            st["ams_back_ts"] = nowt
            ctx.log("F17 AMS bounce (down %d ticks, now up) -> queue clean re-publish" % st["down_ticks"])
        st["down_ticks"] = 0

        # --- deploy sentinel = force ONE re-publish (clears a CURRENT stale state) ---
        sentinel = ctx.cfg.state_path("republish-once")
        if os.path.exists(sentinel):
            if not st.get("pending"):
                st["pending"] = True
                st["ams_back_ts"] = nowt
                ctx.log("F17 forced one-time re-publish (deploy sentinel)")
            try:
                os.remove(sentinel)                         # consume once -> 'pending' now carries it
            except Exception:
                pass

        if not st.get("pending"):
            return ctx.state_save("stream-republish", st)

        # --- fire (guardrailed) ---
        if ctx.svc_age(svc) < ctx.grace_s:
            return ctx.state_save("stream-republish", st)   # our own boot -> grace
        if ctx.estab_1935() == 0:
            st["pending"] = False                           # not pushing -> StreamCameraHealer restarts (= fresh publish)
            return ctx.state_save("stream-republish", st)
        offset = self._stable_offset(ctx.device_id, ctx.cfg.republish_spread_s)
        if nowt < st.get("ams_back_ts", nowt) + ctx.cfg.republish_settle_s + offset:
            return ctx.state_save("stream-republish", st)   # JITTER: not this node's turn yet
        name = self.name
        if not ctx.rate_ok(name):
            st["pending"] = False
            ctx.state_save("stream-republish", st)
            return ctx.escalate(name, "republish-rate-exceeded", {"ams": ams})
        ctx.log("F17 clean re-publish (fresh publish-start to AMS) -> restart %s" % svc)
        ctx.rate_hit(name)
        st["pending"] = False
        st["last_republish_ts"] = nowt
        ctx.state_save("stream-republish", st)
        if not ctx.restart(svc):
            return ctx.escalate(name, "republish-restart-failed", {"ams": ams})
        time.sleep(6)
        if ctx.estab_1935() > 0:
            ctx.log("F17 re-publish OK (RTMP re-ESTAB to %s)" % ams)
        else:
            ctx.escalate(name, "republish-no-rtmp-after-restart", {"ams": ams})

    def _ams_host(self, ctx):
        m = re.search(r"rtmp://([^:/]+)", ctx.env.get("RTMP_URL", ""))
        return m.group(1) if m else None

    @staticmethod
    def _stable_offset(seed, spread):
        if spread <= 0:
            return 0
        return int(hashlib.md5(("%s" % seed).encode()).hexdigest(), 16) % spread  # deterministic per-node (hash() is salted)
