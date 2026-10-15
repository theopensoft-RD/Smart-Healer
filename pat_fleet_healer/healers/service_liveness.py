"""F11 - a core pat-smart service is dead/failed -> reset-failed + restart (L1).
Startup grace skips a just-(re)started unit; rate-limit -> escalate (crash-loop =
genuine fault, not a glitch).
v540: only the units ENABLED on this node. A disabled unit is off on purpose (flow-only
node: no radar worker; no camera: no stream worker); v539 started them on every node.
The flow worker (pat-smart-dropler) is covered too: it is the only worker of a flow node."""
from .base import Healer


class ServiceLivenessHealer(Healer):
    name = "liveness"
    SERVICES = ("pat-smart-radar", "pat-smart-dropler", "pat-smart-stream")

    def run(self, ctx):
        for svc in self.SERVICES:
            if not ctx.svc_enabled(svc):
                continue                                    # off on purpose on this node
            if ctx.svc_active(svc):
                continue
            if ctx.svc_age(svc) < ctx.grace_s:
                continue                                    # startup grace
            name = "liveness:" + svc
            if not ctx.rate_ok(name):
                ctx.escalate(name, "svc-crash-loop", {"svc": svc})
                continue
            ctx.log("%s inactive -> L1 restart" % svc)
            ctx.rate_hit(name)
            if not ctx.restart(svc):
                ctx.escalate(name, "svc-restart-failed", {"svc": svc})
