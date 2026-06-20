"""core - the side-effect chokepoints. Each module is one concern: shell-out,
logging, systemd, network probes, journal reads, persisted state, escalation.
Healers never shell out directly; they go through these via the Context."""
