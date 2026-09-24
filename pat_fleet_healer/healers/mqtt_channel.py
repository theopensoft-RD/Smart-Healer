"""E2 - the MQTT channel is dead while the service looks fine. PISN004 (2026-09-12 -> 09-18): the
sign app was active and its watchdog counted reconnect lines, yet it held no socket to the broker
for six days, so it could not receive /alert, /data or /status - a flood warning would never have
reached the screen. The honest signal is the socket table: a node that talks MQTT always has at
least one ESTABLISHED flow to the broker port (the sign app; on a station the radar/dropler workers
and the stream supervisor). None for MQTT_DEAD_TICKS consecutive ticks, with the WAN up and the
unit active past its grace, -> restart the unit that owns the channel (rate-limited). WAN down is
the connectivity healer's job and is left alone here."""
from .base import Healer

_S = "mqtt-channel"


class MqttChannelHealer(Healer):
    name = "mqtt-channel"
    requires_identity = False

    def run(self, ctx):
        svc = self._unit(ctx)
        if not svc:
            return
        st = ctx.state_load(_S) or {}
        if not ctx.svc_active(svc) or ctx.svc_age(svc) < ctx.grace_s:
            return self._clear(ctx, st)
        if not (ctx.tcp_up("8.8.8.8", 53, 3) or ctx.tcp_up("1.1.1.1", 53, 3)):
            return self._clear(ctx, st)                       # no WAN: not a channel problem
        if self._broker_sockets(ctx) > 0:
            return self._clear(ctx, st)
        st["dead_ticks"] = int(st.get("dead_ticks", 0)) + 1
        if st["dead_ticks"] < ctx.cfg.mqtt_dead_ticks:
            return ctx.state_save(_S, st)
        ev = {"svc": svc, "ticks": st["dead_ticks"], "port": ctx.cfg.mqtt_port}
        st["dead_ticks"] = 0
        ctx.state_save(_S, st)
        if not ctx.rate_ok(self.name):
            return ctx.escalate(self.name, "channel-restart-rate-exceeded", ev)
        ctx.rate_hit(self.name)
        ctx.event("mqtt.channel-dead", **ev)
        if not ctx.restart(svc):
            ctx.escalate(self.name, "channel-restart-failed", ev)

    def _unit(self, ctx):
        if ctx.device_id:
            return "pat-smart-radar"                          # the station's MQTT owner (LWT, status, heartbeat)
        if ctx.unit_exists("pat-sig"):
            return "pat-sig"
        return None

    def _broker_sockets(self, ctx):
        rc, out, _ = ctx.sh("ss -tn state established '( dport = :1883 or dport = :8883 )' 2>/dev/null | tail -n +2", timeout=10)
        if rc != 0:
            return 1                                          # cannot tell: never act on a broken probe
        return len([ln for ln in out.splitlines() if ln.strip()])

    def _clear(self, ctx, st):
        if st.get("dead_ticks"):
            st["dead_ticks"] = 0
            ctx.state_save(_S, st)
