"""pat-fleet-healer - node-local self-healing agent (ADR-037).

Modular package (rev 520 - structured events for AI diagnosis). Deployed as a
single zipapp artifact (healer.pyz) and
driven by a systemd oneshot timer (~60s). Detect + remediate in-node faults with
graduated, least-invasive-first remediation under hard guardrails.

SAFETY INVARIANTS (do not weaken - enforced across healers):
  - NEVER touches netbird (no netbird in any remediation path).
  - NEVER reboots the node (self-preservation): escalate, do not reboot.
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
__version__ = "520"
