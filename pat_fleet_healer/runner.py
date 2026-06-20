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
    if not cfg.device_id:
        ctx.event("agent.abort")                            # never act on an unidentified node
        return
    reg = registry if registry is not None else default_registry()
    for h in reg:
        try:
            h.run(ctx)
        except Exception as e:
            ctx.event("agent.exc", healer=getattr(h, "name", "?"), err=repr(e))  # isolation: one fault must not stop the engine
    ctx.heartbeat(sw=__version__, healers=len(reg))         # rate-limited proof-of-life (NOT a per-tick log)


def main():
    run()
