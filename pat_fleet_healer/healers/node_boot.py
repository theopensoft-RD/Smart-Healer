"""E4 - say so when the node has (re)booted. PIT037 "came back online" on 2026-09-12 and the
dashboard read it as a recovery; the Pi had lost power and rebooted. Nothing in the fleet marked
the boot, so the outage looked like a blip. One event per boot (kernel boot_id vs the last one
recorded) with the uptime at first sight, so the statistics service can turn a heartbeat gap
into an outage with the cause attached. Runs on every node kind."""
import os
from .base import Healer

_S = "boot"


class NodeBootHealer(Healer):
    name = "boot"
    requires_identity = False

    def run(self, ctx):
        boot_id = self._boot_id()
        if not boot_id:
            return
        st = ctx.state_load(_S) or {}
        if st.get("boot_id") == boot_id:
            return
        ev = {"uptime_s": int(self._uptime()), "boot_id": boot_id[:8]}
        if st.get("boot_id"):
            ev["prev"] = st["boot_id"][:8]
        st["boot_id"] = boot_id
        ctx.state_save(_S, st)
        ctx.event("node.boot", **ev)

    @staticmethod
    def _boot_id():
        try:
            return open("/proc/sys/kernel/random/boot_id").read().strip()
        except Exception:
            return ""

    @staticmethod
    def _uptime():
        try:
            return float(open("/proc/uptime").read().split()[0])
        except Exception:
            return 0.0
