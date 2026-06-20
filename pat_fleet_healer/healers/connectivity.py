"""F10/F2 - 4G WAN down. DETECT + ESCALATE ONLY (netbird safety invariant):
the Robustel's own emergency_reboot performs WAN recovery; the healer never
reboots Robustel/netbird from the node."""
from .base import Healer


class ConnectivityHealer(Healer):
    name = "connectivity"

    def run(self, ctx):
        if ctx.tcp_up("8.8.8.8", 53, 3) or ctx.tcp_up("1.1.1.1", 53, 3):
            return
        name = self.name
        if ctx.rate_ok(name):
            ctx.rate_hit(name)
            ctx.escalate(name, "wan-down-detect-only", {"note": "Robustel self-reboot handles recovery"})
