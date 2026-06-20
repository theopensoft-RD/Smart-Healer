"""Housekeeping (low-risk · never touches a service): purge rotated logs + stale
worker backups older than retention. Strict glob pattern + mtime filter -> active
workers / config / today's log are never matched."""
import os
import glob
import time
from .base import Healer


class DiskHygieneHealer(Healer):
    name = "disk-hygiene"

    def run(self, ctx):
        now = time.time()
        rules = ((os.path.join(ctx.cfg.log_dir, "*.log"), ctx.cfg.log_retention_days),
                 (os.path.join(ctx.cfg.workers_dir, "*.bak-*"), ctx.cfg.bak_retention_days),
                 (os.path.join(ctx.cfg.workers_dir, "*.bak"), ctx.cfg.bak_retention_days))
        old = []
        for pat, days in rules:
            cutoff = now - days * 86400
            for f in glob.glob(pat):
                try:
                    if os.path.isfile(f) and os.path.getmtime(f) < cutoff:
                        old.append(f)
                except OSError:
                    pass
        if not old:
            return
        if ctx.dry_run:
            ctx.log("would purge %d old file(s): %s" % (len(old), ", ".join(os.path.basename(f) for f in old[:6])))
            return
        removed = []
        for f in old:
            try:
                os.remove(f)
                removed.append(os.path.basename(f))
            except OSError as e:
                ctx.log("purge fail %s: %r" % (f, e))
        if removed:
            ctx.log("disk-hygiene purged %d file(s): %s" % (len(removed), ", ".join(removed[:6])))
