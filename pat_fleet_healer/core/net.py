"""Network probes: TCP reachability, RTMP push count, LAN port scan."""
import socket
from .shell import sh


def tcp_up(host, port, t=3):
    try:
        s = socket.create_connection((host, int(port)), t)
        s.close()
        return True
    except Exception:
        return False


def estab_1935():
    """Count ESTABLISHED RTMP (:1935) connections = 'this node is pushing a stream'."""
    rc, o, _ = sh("ss -tn state established 2>/dev/null | grep -c :1935")
    try:
        return int(o)
    except Exception:
        return 0


def scan_port(port):
    """Parallel scan of 192.168.1.2-220 for an open TCP port (find a relocated
    camera :554 / Modbus sensor :502). Returns sorted list of responding IPs."""
    rc, o, _ = sh(
        "for i in $(seq 2 220); do (timeout 1 bash -c \"exec 3<>/dev/tcp/192.168.1.$i/%d\" "
        "2>/dev/null && echo 192.168.1.$i) & done; wait 2>/dev/null | sort -u" % int(port), timeout=40)
    return [x for x in o.split() if x]
