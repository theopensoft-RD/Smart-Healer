"""F12 - redis (the radar/stream pub-sub bus) down -> L2 restart redis + bounce
its dependents (their pub/sub dies with redis)."""
import time
from .base import Healer


class DependencyHealer(Healer):
    name = "dependency"

    def run(self, ctx):
        redis_ok = ctx.svc_active("redis-server") and ctx.sh("redis-cli -h localhost ping")[1] == "PONG"
        if redis_ok:
            return
        if not ctx.rate_ok(self.name):
            return ctx.escalate(self.name, "redis-down-rate-exceeded")
        ctx.log("redis down -> L2 restart redis + dependents")
        ctx.rate_hit(self.name)
        if ctx.restart("redis-server"):
            time.sleep(3)
            for dep in ("pat-smart-radar", "pat-smart-stream"):
                ctx.restart(dep)
        else:
            ctx.escalate(self.name, "redis-restart-failed")
