"""healers - one module per failure-family. Each is a Healer subclass with a
single entry, run(ctx), and carries its own guardrails (grace / rate-limit /
verify). registry.default_registry() returns them in dependency-first order."""
