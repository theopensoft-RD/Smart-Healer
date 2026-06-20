#!/usr/bin/env python3
"""Build the single deployable artifact: pat_fleet_healer/  ->  healer.pyz

A zipapp is a standard Python single-file executable: modular source for dev +
GitHub, ONE artifact for the edge fleet (ships via the same single-file mechanism;
the systemd unit runs `python3 healer.pyz`). Deterministic: __pycache__ excluded.

Usage:  python3 build.py            # -> ./healer.pyz
        python3 build.py --check    # build to a temp file + smoke-run it (DRY)
"""
import os
import sys
import shutil
import tempfile
import zipapp
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "pat_fleet_healer")
OUT = os.path.join(HERE, "healer.pyz")
INTERPRETER = "/usr/bin/env python3"


def _filter(path):
    # exclude caches / pyc so the artifact is byte-deterministic
    parts = path.parts
    return "__pycache__" not in parts and not path.name.endswith(".pyc")


def build(out=OUT):
    if not os.path.isdir(SRC):
        print("ERR: source package not found: %s" % SRC)
        return None
    # zipapp from a *staged copy* so the package dir becomes the archive root and
    # `python healer.pyz` runs pat_fleet_healer/__main__.py
    staging = tempfile.mkdtemp()
    try:
        pkg_dst = os.path.join(staging, "pat_fleet_healer")
        shutil.copytree(SRC, pkg_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # a top-level __main__.py that delegates to the package dispatch (tick | collect)
        with open(os.path.join(staging, "__main__.py"), "w") as f:
            f.write("from pat_fleet_healer.__main__ import main\nmain()\n")
        zipapp.create_archive(staging, target=out, interpreter=INTERPRETER, filter=_filter)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    os.chmod(out, 0o755)
    print("built %s (%d bytes)" % (out, os.path.getsize(out)))
    return out


def smoke(out):
    """Run the artifact once in DRY_RUN against a throwaway env -> proves it imports,
    wires, and a full tick completes without touching node state."""
    env = dict(os.environ)
    d = tempfile.mkdtemp()
    envf = os.path.join(d, ".env")
    open(envf, "w").write("DEVICE_ID=PAT-SMOKE-TEST\nRTMP_URL=rtmp://ams.invalid/CCTVApp/x\nMQTT_HOST=127.0.0.1\n")
    env.update({"HEALER_DRY_RUN": "1", "HEALER_ENV_PATH": envf, "HEALER_GRACE_S": "0"})
    r = subprocess.run([sys.executable, out], env=env, capture_output=True, text=True, timeout=90)
    print(r.stdout.strip())
    ok = r.returncode == 0 and "agent.alive" in r.stdout   # heartbeat fires on first tick -> proves wire+run
    print("SMOKE: %s" % ("PASS" if ok else "FAIL rc=%d %s" % (r.returncode, r.stderr[:200])))
    shutil.rmtree(d, ignore_errors=True)
    return ok


if __name__ == "__main__":
    out = build()
    if out and "--check" in sys.argv:
        sys.exit(0 if smoke(out) else 1)
