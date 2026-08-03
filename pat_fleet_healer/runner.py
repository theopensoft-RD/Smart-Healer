"""The engine. Build the production Context, run the registry in dependency-first
order with per-healer isolation: one healer raising must never stop the others
(the tick must always complete)."""
from . import __version__
from .config import Config
from .context import production_context
from .healers.registry import default_registry


def run(cfg=None, ctx=None, registry=None):
    cfg = cfg or Config()
    ctx = ctx or production_context(cfg)
    reg = registry if registry is not None else default_registry()
    if not ctx.state_writable():
        # State dir unwritable -> the rate limiter AND the local event log are both
        # dead. Healers still act (a lost quota record must never block a repair),
        # but the cap is gone - so say it loudly: sev=error pushes to central and
        # the human line reaches journald even when nothing can be written locally.
        # Silence here is what hid 649 aborted remediations on 4 nodes for months.
        ctx.event("agent.state-unwritable", dir=cfg.state_dir)
    has_id = bool(cfg.device_id)
    if not has_id:
        ctx.event("agent.infra-only")                       # identity-less node (e.g. pisn signage IRIV): run infra healers only
    ran = 0
    for h in reg:
        if getattr(h, "requires_identity", True) and not has_id:
            continue                                        # sensor/stream healers need a DEVICE_ID; infra (4G/disk/beszel) don't
        try:
            h.run(ctx)
            ran += 1
        except Exception as e:
            ctx.event("agent.exc", healer=getattr(h, "name", "?"), err=repr(e))  # isolation: one fault must not stop the engine
    ctx.heartbeat(sw=__version__, healers=ran)              # rate-limited proof-of-life (NOT a per-tick log)


def main():
    run()
