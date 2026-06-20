"""Context = config + injected services. Healers receive a Context and call
ctx.<service>(...) instead of importing core directly. Production wiring is the
default (production_context); the test suite builds a Context with stub services,
so each healer is unit-tested in isolation with NO global monkeypatching."""


class Context:
    def __init__(self, cfg, services):
        self.cfg = cfg
        self._svc = dict(services)

    def __getattr__(self, name):
        # only reached when normal lookup misses -> resolve injected services
        svc = self.__dict__.get("_svc", {})
        if name in svc:
            return svc[name]
        raise AttributeError(name)

    # --- config passthroughs (read-only convenience) ---
    @property
    def env(self):          return self.cfg.env
    @property
    def device_id(self):    return self.cfg.device_id
    @property
    def dry_run(self):      return self.cfg.dry_run
    @property
    def grace_s(self):      return self.cfg.grace_s
    @property
    def modbus_host(self):  return self.cfg.modbus_host


def production_context(cfg):
    """Wire the real core services for on-node operation. log + escalate route
    through the structured event system: escalate -> a coded event ('<domain>.
    <verdict>', the ':svc' rate-suffix stripped) pushed to central; log -> a
    transitional free-text 'agent.log' event."""
    from .core import shell, systemd, net, journal, state, events
    return Context(cfg, {
        "sh":            shell.sh,
        "svc_active":    systemd.svc_active,
        "unit_exists":   systemd.unit_exists,
        "svc_age":       systemd.svc_age,
        "restart":       lambda svc: systemd.restart(cfg, svc),
        "journal":       journal.journal,
        "online_recent": journal.online_recent,
        "tcp_up":        net.tcp_up,
        "estab_1935":    net.estab_1935,
        "scan_port":     net.scan_port,
        "rate_ok":       lambda n: state.rate_ok(cfg, n),
        "rate_hit":      lambda n: state.rate_hit(cfg, n),
        "state_load":    lambda name: state.load(cfg, name),
        "state_save":    lambda name, d: state.save(cfg, name, d),
        # structured events (canonical) + back-compat shims
        "event":         lambda code, **f: events.emit(cfg, code, f or None),
        "escalate":      lambda h, v, ev=None: events.emit(cfg, h.split(":")[0] + "." + v, ev or {}, push=True),
        "log":           lambda m: events.emit(cfg, "agent.log", {"msg": m}),
        "heartbeat":     lambda **f: events.heartbeat(cfg, **f),
    })
