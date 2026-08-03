"""F10/F2 - 4G WAN down. UPLINK-AWARE recovery:

  robustel (RPi5 + Robustel router): DETECT + ESCALATE ONLY. The Robustel's own
      emergency_reboot ping-watchdog performs WAN recovery off-node (edge
      autonomy); the healer never reboots Robustel/netbird from the node.

  ec25 (IRIV with internal Quectel EC25 in ECM mode): there is NO external
      watchdog, so the healer IS the recovery - it resets the modem via
      ModemManager (`mmcli -m any --reset`), which re-registers + re-establishes
      the ECM data link (usb0). Resetting the MODEM is NOT rebooting the NODE
      (self-preservation invariant intact: the healer + node survive). Used by
      pisn signage IRIV now + Sensor P2 (IRIV + EC25, CM4/CM5) later.

  Guarded: WAN must be down >= WAN_DOWN_CONFIRM consecutive ticks (verify-before-
  concluding, not a transient flap) before any reset; after a reset, wait
  EC25_SETTLE_S for re-register before judging; rate-limit -> escalate (never
  reset-loop); dry-run aware. Uplink type = cfg.uplink (UPLINK env) or auto-detect.
"""
import time
from .base import Healer

_S = "conn"   # single state blob: {down, reset_ts, uplink}


class ConnectivityHealer(Healer):
    name = "connectivity"
    requires_identity = False        # infra: runs on identity-less nodes (pisn signage / IRIV) too

    def run(self, ctx):
        st = ctx.state_load(_S) or {}
        if ctx.tcp_up("8.8.8.8", 53, 3) or ctx.tcp_up("1.1.1.1", 53, 3):
            if st.get("down") or st.get("reset_ts"):
                if st.get("reset_ts"):
                    ctx.log("ec25 WAN recovered after modem reset")
                st["down"] = 0
                st["reset_ts"] = 0
                ctx.state_save(_S, st)
            return

        if self._uplink(ctx, st) != "ec25":
            # robustel / unknown: detect + escalate only (off-node Robustel self-reboot recovers)
            if ctx.rate_ok(self.name):
                ctx.rate_hit(self.name)
                ctx.escalate(self.name, "wan-down-detect-only", {"uplink": st.get("uplink", "?")})
            return

        # --- ec25 (IRIV): confirm a real outage, then active modem-reset recovery ---
        st["down"] = int(st.get("down", 0)) + 1
        if st["down"] < ctx.cfg.wan_down_confirm:
            ctx.state_save(_S, st)
            return                                          # transient drop; wait for confirmation

        now = time.time()
        if st.get("reset_ts"):
            if now - st["reset_ts"] < ctx.cfg.ec25_settle_s:
                ctx.state_save(_S, st)
                return                                      # just reset; give it time to re-register
            st["reset_ts"] = 0                              # settle elapsed and still down -> reset didn't fix it
            ctx.state_save(_S, st)
            return ctx.escalate(self.name, "ec25-reset-no-recovery",
                                {"down": st["down"], "note": "WAN still down after modem reset+settle (SIM/coverage/hw?)"})

        if not ctx.rate_ok(self.name):
            ctx.state_save(_S, st)
            return ctx.escalate(self.name, "ec25-reset-rate-exceeded", {"down": st["down"]})

        if ctx.dry_run:
            ctx.state_save(_S, st)
            return ctx.log("WAN down x%d (ec25) -> would reset modem (mmcli -m any --reset)" % st["down"])

        ctx.log("WAN down x%d (ec25) -> resetting modem (mmcli -m any --reset)" % st["down"])
        ctx.rate_hit(self.name)
        rc, _, err = ctx.sh("sudo -n /usr/bin/mmcli -m any --reset", 30)
        if rc != 0:
            ctx.state_save(_S, st)
            return ctx.escalate(self.name, "ec25-reset-failed", {"rc": rc, "err": (err or "")[:120]})
        st["reset_ts"] = now                               # next tick judges recovery after the settle window
        ctx.state_save(_S, st)

    # uplink class: explicit cfg.uplink wins, else auto-detect (Quectel EC25 -> ec25), cached in state
    def _uplink(self, ctx, st):
        u = (getattr(ctx.cfg, "uplink", "auto") or "auto").lower()
        if u in ("ec25", "robustel", "none"):
            st["uplink"] = u
            return u
        if st.get("uplink"):
            return st["uplink"]
        rc, o, _ = ctx.sh("mmcli -L 2>/dev/null", 8)
        is_ec25 = rc == 0 and ("EC25" in o or "Quectel" in o)
        if not is_ec25:
            rc2, o2, _ = ctx.sh("lsusb 2>/dev/null | grep -i 2c7c", 8)
            is_ec25 = rc2 == 0 and bool(o2.strip())
        v = "ec25" if is_ec25 else "robustel"
        st["uplink"] = v
        ctx.state_save(_S, st)                             # persist detection (don't re-probe every down-tick)
        return v
