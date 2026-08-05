"""pat-fleet-healer - node-local self-healing agent (ADR-037).

Modular package (rev 525 - probe also records relay/overlay health, whether the
CENTRE was reachable while the internet was fine, node uptime (reboot vs outage),
jitter/loss, clock sync, disk and memory;
rev 524 - phase-1 network probe: outage duration + a
carrier-vs-cabinet discriminator that needs no router credentials;
rev 523 - self-update works on both fleet OS generations, and an
unwritable state dir can no longer silence remediation; rev 522 - multi-brand camera
repair + verified H.264 enforcement; rev 521 - uplink-aware IRIV Quectel EC25 4G
recovery; rev 520 - structured events for AI diagnosis). Deployed as a single zipapp
artifact (healer.pyz) and driven by a systemd oneshot timer (~60s). Detect +
remediate in-node faults with graduated, least-invasive-first remediation under hard
guardrails. Runs on RPi5+Robustel AND IRIV(CM4/CM5)+EC25 nodes (pisn signage now,
Sensor P2 later); identity-less infra nodes run infra healers (4G/disk/beszel).

SAFETY INVARIANTS (do not weaken - enforced across healers):
  - NEVER touches netbird (no netbird in any remediation path).
  - NEVER reboots the node (self-preservation): escalate, do not reboot.
    (ec25 4G recovery resets the MODEM via mmcli, not the node -> invariant holds.)
  - Startup grace + per-healer rate-limit -> escalate (a recoverable glitch must
    not become a restart-loop; a genuine hardware fault must reach a human).
  - Backup before any .env edit. Identity-gate to own DEVICE_ID.
  - HEALER_DRY_RUN=1 -> log intended actions, change nothing.

Architecture:
  config.Config        - all tunables + .env loading (one place)
  core/*               - side-effect chokepoints (shell, systemd, net, journal,
                         state, escalate, log) - each a single concern
  context.Context      - config + injected services (DI -> healers are unit-testable
                         with stubs, no global monkeypatching)
  healers/*            - one module per failure-family; Healer.run(ctx)
  healers/registry.py  - ordered registry (dependency-first run order)
  runner.run()         - the engine: build ctx, run registry, per-healer isolation
"""
__version__ = "528"
