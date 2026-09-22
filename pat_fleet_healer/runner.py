"""The engine. Build the production Context, run the registry in dependency-first
order with per-healer isolation: one healer raising must never stop the others
(the tick must always complete)."""
from . import __version__, buildinfo
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
    # carry the BUILD id, not just the version: two artifacts can claim the same
    # number (dist/ shipped v529 while source said 531 on 2026-09-22).
    ctx.heartbeat(sw=__version__, build=buildinfo.describe(__version__), healers=ran)
    _clear_update_pending(ctx)              # rate-limited proof-of-life (NOT a per-tick log)


def main():
    run()


def _clear_update_pending(ctx):
    """Tell the self-updater this build completed a real tick.

    healer-selfupdate.sh drops `update.pending` after installing a new artifact and
    rolls back to .prev if it is still there ~15 min later. Reaching this line means
    the engine built a context, ran the registry and emitted a heartbeat - i.e. the
    new build actually works on this node, not merely that it imported (which is all
    selftest proves). Deleting the marker is therefore the honest "it works" signal.

    Never raises: a failure here must not turn a healthy tick into a failed one, and
    a leftover marker only costs one unnecessary rollback to a build that was fine.
    """
    try:
        import os
        os.unlink(os.path.join(ctx.cfg.state_dir, "update.pending"))
    except Exception:
        pass
