"""Healer base. A healer is a named unit of detect+remediate logic with one
entry point, run(ctx). Implementations MUST stay guardrailed:
  - respect startup grace (don't act on a just-(re)started service)
  - rate-limit -> escalate (never restart-loop; a real fault reaches a human)
  - verify after acting where possible
  - never touch netbird; never reboot the node."""


class Healer:
    name = "healer"
    requires_identity = True        # default: gated on DEVICE_ID. Infra healers (4G/disk/beszel) override to False.

    def run(self, ctx):
        raise NotImplementedError
