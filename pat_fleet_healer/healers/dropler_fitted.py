"""E4 - "the water-speed sensor is offline" that was never there. A MODE=FULL station runs the
Doppler worker against /dev/ttyUSB0; on several nodes the RS485 adapter was never fitted, so the
worker faults forever and every screen calls it an outage. A config truth, not a fault: report it
ONCE per boot (radar-only nodes are silent, and a fitted adapter clears it), so the statistics
service can exclude these from downtime instead of alerting on them."""
import glob
from .base import Healer

_S = "dropler"


class DroplerFittedHealer(Healer):
    name = "dropler"

    def run(self, ctx):
        if (ctx.env.get("MODE") or "RADAR").upper() != "FULL":
            return
        st = ctx.state_load(_S) or {}
        fitted = bool(self._serial_devices())
        boot = ctx.state_load("boot").get("boot_id", "")
        if fitted:
            if st.get("missing"):
                st["missing"] = False
                ctx.state_save(_S, st)
                ctx.event("dropler.fitted", devices=self._serial_devices())
            return
        if st.get("missing") and st.get("boot") == boot:
            return
        st["missing"] = True
        st["boot"] = boot
        ctx.state_save(_S, st)
        ctx.event("dropler.not-fitted", mode="FULL", port=ctx.env.get("MODBUS_USBPORT", "/dev/ttyUSB0"))

    @staticmethod
    def _serial_devices():
        return sorted(glob.glob("/dev/ttyUSB*"))
