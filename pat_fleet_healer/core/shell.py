"""The single shell-out chokepoint. Never raises (returns 124 on timeout/error)
so one bad command can never crash a healer."""
import subprocess


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 124, "", str(e)
