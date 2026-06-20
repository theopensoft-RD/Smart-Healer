"""F1/F3/F16 - radar active but stuck (circuit=open, no recent ONLINE).
  sensor reachable  -> L1 restart (circuit-open backstop; rate-exceeded -> the
                       VEGAMET is genuinely dead -> escalate, do not loop).
  sensor UNREACHABLE (technician relocated/re-IP'd the level sensor) -> SCAN :502
                       and escalate 'sensor-moved' WITH the candidate IP. We do
                       NOT auto-repoint a Modbus level sensor (unlike the camera):
                       wrong :502 device = wrong flood level = safety-critical
                       (HARD RULE: verify field semantics before patching). A human
                       confirms, then HOST is repointed."""
from .base import Healer


class RadarSensorHealer(Healer):
    name = "radar"

    def run(self, ctx):
        svc = "pat-smart-radar"
        if not ctx.svc_active(svc) or ctx.svc_age(svc) < ctx.grace_s:
            return
        j = ctx.journal(svc, 25)
        stuck = ("circuit=open" in j) and not ctx.online_recent(svc, 3)
        if not stuck:
            return
        name = self.name
        if not ctx.tcp_up(ctx.modbus_host, 502):
            found = [ip for ip in ctx.scan_port(502) if ip != ctx.modbus_host]
            if len(found) == 1:
                return ctx.escalate(name, "sensor-moved", {"old": ctx.modbus_host, "candidate": found[0],
                                    "note": "verify device then set HOST=candidate (safety-critical)"})
            if len(found) == 0:
                return ctx.escalate(name, "sensor-absent", {"configured": ctx.modbus_host})
            return ctx.escalate(name, "sensor-ambiguous", {"configured": ctx.modbus_host, "found": found})
        if not ctx.rate_ok(name):
            return ctx.escalate(name, "vegamet-fault-or-stuck", {"vegamet_tcp": True})
        ctx.log("radar stuck (circuit=open, no ONLINE 3m, vegamet_tcp=True) -> L1 restart")
        ctx.rate_hit(name)
        ctx.restart(svc)
