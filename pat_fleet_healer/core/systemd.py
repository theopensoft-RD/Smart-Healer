"""systemd unit probes + the restart actuator (the ONLY service-state mutator).
restart() honours cfg.dry_run and reset-failed's a failed unit before restart."""
from .shell import sh
from .log import log


def svc_active(s):
    return sh("systemctl is-active %s" % s)[1] == "active"


def unit_exists(s):
    return bool(sh("systemctl list-unit-files %s.service --no-legend 2>/dev/null" % s)[1])


def svc_age(s):
    """Seconds since the unit last entered active (1e9 if unknown). Drives startup grace."""
    rc, o, _ = sh("systemctl show -p ActiveEnterTimestampMonotonic --value %s" % s)
    try:
        mono = int(o) / 1e6
        up = float(open("/proc/uptime").read().split()[0])
        return up - mono if mono > 0 else 1e9
    except Exception:
        return 1e9


def restart(cfg, svc):
    if cfg.dry_run:
        log(cfg, "would restart %s" % svc)
        return True
    sh("sudo -n systemctl reset-failed %s" % svc)
    rc, _, e = sh("sudo -n systemctl restart %s" % svc)
    if rc != 0:
        log(cfg, "restart %s FAILED rc=%d %s" % (svc, rc, e))
        return False
    return True
