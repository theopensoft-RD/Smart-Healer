"""F15 - beszel-agent (monitoring telemetry) inactive -> restart. Low-risk:
monitoring only; never touches netbird/stream/radar. Acts ONLY on an *inactive*
agent whose unit EXISTS (an active-but-hub-not-seeing agent is a hub-side/network
issue, not node-local; a node with no agent installed needs install, not restart).
rate-limit -> escalate (don't loop on a wedged agent)."""
from .base import Healer


class BeszelAgentHealer(Healer):
    name = "beszel"

    def run(self, ctx):
        svc = "beszel-agent"
        if not ctx.unit_exists(svc):
            return                                          # not installed here -> nothing to do
        if ctx.svc_active(svc):
            return                                          # active -> leave (hub-side non-report != node-local)
        if ctx.svc_age(svc) < ctx.grace_s:
            return
        name = self.name
        if not ctx.rate_ok(name):
            return ctx.escalate(name, "beszel-agent-restart-rate-exceeded")
        ctx.log("beszel-agent inactive -> restart (restore monitoring visibility)")
        ctx.rate_hit(name)
        if not ctx.restart(svc):
            ctx.escalate(name, "beszel-agent-restart-failed")
