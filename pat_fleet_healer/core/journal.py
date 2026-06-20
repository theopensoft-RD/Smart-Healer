"""journald reads (read-only evidence for healers)."""
from .shell import sh


def journal(unit, n=20):
    return sh("journalctl -u %s -n %d --no-pager 2>/dev/null" % (unit, n))[1]


def online_recent(unit, mins=3):
    rc, o, _ = sh("journalctl -u %s --since '-%d min' --no-pager 2>/dev/null | grep -cE 'ONLINE|\"level\"'"
                  % (unit, mins))
    try:
        return int(o) > 0
    except Exception:
        return False
