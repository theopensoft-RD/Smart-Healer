#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for healer-selfupdate.sh - the pull-update path that must work on BOTH
fleet generations (OpenSSL 3.5.5 on RPi5, OpenSSL 1.1.1w on the IRIV signage nodes).

The whole suite runs TWICE: once with an openssl that knows `-rawin` and once with
one that does not. Identical results are the point - that split is what silently
stopped every PISN update (434 rejects on pisn004) while the signature was fine.

Run:  python3 selfupdate/test_selfupdate.py            (from the repo root)
"""
import os
import sys
import time
import shutil
import stat
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SCRIPT = os.path.join(HERE, "healer-selfupdate.sh")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

R = []
def check(name, cond):
    R.append((name, bool(cond)))

TMPS = []
def mktmp():
    d = tempfile.mkdtemp(); TMPS.append(d); return d


# --- pick the two openssl binaries we need to exercise both fleet generations -----
def find_openssl(want_rawin):
    cands = ["/opt/homebrew/opt/openssl@3/bin/openssl", "/usr/local/opt/openssl@3/bin/openssl",
             "/usr/bin/openssl", shutil.which("openssl")]
    for c in cands:
        if not c or not os.path.exists(c):
            continue
        h = subprocess.run([c, "pkeyutl", "-help"], capture_output=True, text=True)
        has = "-rawin" in (h.stdout + h.stderr)
        if has == want_rawin:
            return c
    return None

OSSL_NEW = find_openssl(True)      # OpenSSL 3.x  -> the pit/pir path
OSSL_OLD = find_openssl(False)     # no -rawin    -> the pisn (1.1.1w) path


def shim_path(openssl_bin):
    """A PATH dir whose `openssl` is the one we chose for this run."""
    d = mktmp()
    os.symlink(openssl_bin, os.path.join(d, "openssl"))
    for tool in ("curl", "date", "hostname", "mktemp", "install", "mv", "cp", "grep",
                 "cut", "tr", "mkdir", "rm", "printf", "command", "python3"):
        p = shutil.which(tool)
        if p:
            try:
                os.symlink(p, os.path.join(d, tool))
            except OSError:
                pass
    return d


# --- a release the node can pull from (file:// - curl handles it, no network) -----
def make_release(version, artifact_bytes, key, corrupt_sig=False):
    d = mktmp()
    open(os.path.join(d, "version"), "w").write(str(version) + "\n")
    open(os.path.join(d, "healer.pyz"), "wb").write(artifact_bytes)
    sig = key.sign(artifact_bytes)
    if corrupt_sig:
        sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
    open(os.path.join(d, "healer.pyz.sig"), "wb").write(sig)
    return "file://" + d


def make_home(pub_pem, installed_version="521", fake_python=None, device_id="PAT-T-001",
              state_writable=True):
    """A node's HOME: workers dir with the current pyz + baked-in public key."""
    h = mktmp()
    w = os.path.join(h, ".config", "pat-smart", "workers")
    os.makedirs(w)
    os.makedirs(os.path.join(h, ".config", "pat-smart"), exist_ok=True)
    st = os.path.join(h, ".local", "state", "pat-smart")
    os.makedirs(st)
    if not state_writable:
        os.chmod(st, 0o500)
    env = os.path.join(h, ".config", "pat-smart", ".env")
    open(env, "w").write(("DEVICE_ID=%s\n" % device_id) if device_id else "MQTT_HOST=x\n")
    open(os.path.join(w, "healer.pyz"), "wb").write(b"INSTALLED-ARTIFACT")
    open(os.path.join(w, "healer-release.pub"), "wb").write(pub_pem)
    if fake_python:
        vb = os.path.join(h, ".local", "share", "pipx", "venvs", "pat-smart", "bin")
        os.makedirs(vb)
        p = os.path.join(vb, "python")
        open(p, "w").write(fake_python)
        os.chmod(p, 0o755)
    else:
        # no venv -> the script must fall back to the system python (the IRIV case)
        pass
    return h, w, st


# On the nodes cryptography is a system dist-package, so it is importable no matter
# what HOME says. On macOS it lives under ~/Library/... - and this harness rewrites
# HOME, which would hide it and make every fallback verification look like
# "no-verifier". Pin it explicitly so the test measures the script, not the laptop.
import cryptography as _c
CRYPTO_PATH = os.path.dirname(os.path.dirname(os.path.abspath(_c.__file__)))


def run_update(home, base, shim, extra_env=None):
    env = dict(os.environ)
    env.update({"HOME": home, "HEALER_RELEASE_BASE": base,
                "PATH": shim + os.pathsep + env.get("PATH", ""),
                "PYTHONPATH": CRYPTO_PATH})
    env.update(extra_env or {})
    p = subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env, timeout=120)
    return p


def events_of(state_dir):
    f = os.path.join(state_dir, "events.jsonl")
    try:
        return open(f).read()
    except Exception:
        return ""


# A stand-in interpreter: answers --version / selftest / the cryptography probe the way
# a node would, and hands EVERYTHING else (crucially the signature check itself) to a
# real python. A stub that answered the verify call would quietly approve a forged
# artifact - which is exactly what the first version of this harness did, and what U2/U3
# caught.
def fake_py(version="999", selftest_rc=0, has_crypto=True):
    # When crypto IS available the probe is NOT stubbed - it delegates like every other
    # call, so the capability probe and the verification always speak for the same
    # interpreter. Stubbing "yes I have cryptography" while delegating to a python that
    # doesn't is a lie the real script can never tell, and it made the first version of
    # this harness reject valid signatures.
    probe = '' if has_crypto else '  *"import cryptography"*) exit 1 ;;\n'
    return """#!/bin/bash
case "$*" in
  *--version*) echo "%s" ;;
  *selftest*)  exit %d ;;
%s  *) exec %s "$@" ;;
esac
""" % (version, selftest_rc, probe, sys.executable)




def fake_py_installed_fail(version="999"):
    """selftest PASSES from a temp path but FAILS from the installed workers path.

    This is the shape the immediate rollback gate exists for: an artifact that clears
    the pre-install check and is broken only once it is in place. A single fixed
    selftest_rc (as fake_py uses) cannot express it.
    """
    return """#!/bin/bash
case "$*" in
  *--version*) echo "%s" ;;
  *selftest*)  case "$*" in *workers/healer.pyz*) exit 1 ;; *) exit 0 ;; esac ;;
  *) exec %s "$@" ;;
esac
""" % (version, sys.executable)

def suite(label, openssl_bin):
    """The whole contract, run against ONE openssl generation."""
    shim = shim_path(openssl_bin)
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo)
    NEW = b"NEW-ARTIFACT-CONTENT"

    # U1 valid signature + newer release -> installs
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    run_update(home, make_release(999, NEW, key), shim)
    check("[%s] U1 valid sig + newer -> installed" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == NEW)
    check("[%s] U1 -> emits selfupdate.ok" % label, "healer.selfupdate.ok" in events_of(st))
    check("[%s] U1 -> keeps a rollback copy (.good)" % label,
          os.path.exists(os.path.join(w, "healer.pyz.good")))
    check("[%s] U1 -> .good holds the PROVEN build, not the one just installed" % label,
          open(os.path.join(w, "healer.pyz.good"), "rb").read() == b"INSTALLED-ARTIFACT")

    # U2 tampered artifact -> rejected, current healer untouched  (the security promise)
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    run_update(home, make_release(999, NEW, key, corrupt_sig=True), shim)
    check("[%s] U2 bad sig -> NOT installed" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U2 bad sig -> reason is bad-signature" % label, "bad-signature" in events_of(st))

    # U3 artifact signed by the WRONG key -> rejected (a repo compromise, not a typo)
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    run_update(home, make_release(999, NEW, Ed25519PrivateKey.generate()), shim)
    check("[%s] U3 wrong signer -> NOT installed" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U3 wrong signer -> bad-signature" % label, "bad-signature" in events_of(st))

    # U4 already current -> silent no-op
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    run_update(home, make_release(521, NEW, key), shim)
    check("[%s] U4 same version -> untouched" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U4 same version -> no event noise" % label, events_of(st).strip() == "")

    # U5 THE DOWNGRADE GUARD: an older release must never overwrite a newer node.
    # This only holds if the local version was read with a python that EXISTS - the
    # old script's hardcoded pipx path made LV=0 on IRIV, so 520 looked newer than 521.
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    run_update(home, make_release(100, NEW, key), shim)
    check("[%s] U5 older release -> NO downgrade" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    # ...and it must stop AT the version comparison. "Unchanged" alone is a trap: if the
    # local version reads as 0 the script downloads and only a later guard (selftest)
    # happens to block it - passing for the wrong reason, which is how the original
    # $VENV bug hid.
    check("[%s] U5 older release -> exits at the version check (silent)" % label,
          events_of(st).strip() == "")

    # U6 no pipx venv at all (the IRIV shape) -> must still read the local version
    home, w, st = make_home(pub, fake_python=None)
    if not os.path.exists(os.path.join(REPO, "healer.pyz")):
        subprocess.run([sys.executable, "build.py"], cwd=REPO, capture_output=True)
    real = open(os.path.join(REPO, "healer.pyz"), "rb").read() if os.path.exists(os.path.join(REPO, "healer.pyz")) else None
    if real:
        open(os.path.join(w, "healer.pyz"), "wb").write(real)     # real pyz, reports 521
        run_update(home, make_release(100, NEW, key), shim)
        check("[%s] U6 no venv -> still reads local version (no downgrade)" % label,
              open(os.path.join(w, "healer.pyz"), "rb").read() == real)
        check("[%s] U6 no venv -> exits at the version check (LV was read, not 0)" % label,
              events_of(st).strip() == "")
    else:
        check("[%s] U6 needs a built healer.pyz at the repo root" % label, False)

    # U7 selftest fails -> rejected before it can replace a working healer
    home, w, st = make_home(pub, fake_python=fake_py("521", selftest_rc=1))
    run_update(home, make_release(999, NEW, key), shim)
    check("[%s] U7 selftest fail -> NOT installed" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U7 selftest fail -> reason says so" % label, "selftest-failed" in events_of(st))

    # U8 no verifier on the node -> fail CLOSED, and say WHY (operator problem, not tampering)
    home, w, st = make_home(pub, fake_python=fake_py("521", has_crypto=False))
    p = run_update(home, make_release(999, NEW, key), shim)
    if openssl_bin == OSSL_OLD:
        check("[%s] U8 no verifier -> NOT installed" % label,
              open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
        check("[%s] U8 no verifier -> distinct 'no-verifier' reason" % label,
              "no-verifier" in events_of(st))
    else:
        check("[%s] U8 openssl can verify alone (python not needed)" % label,
              open(os.path.join(w, "healer.pyz"), "rb").read() == NEW)

    # U9 unwritable state dir must not silence the outcome - journald still gets it
    home, w, st = make_home(pub, fake_python=fake_py("521"), state_writable=False)
    p = run_update(home, make_release(999, NEW, key), shim)
    check("[%s] U9 unwritable state -> update STILL happens" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == NEW)
    check("[%s] U9 unwritable state -> outcome reaches journald" % label,
          "healer.selfupdate.ok" in p.stderr)
    os.chmod(st, 0o700)

    # U10 a node with no DEVICE_ID (signage) must still be identifiable in the event
    home, w, st = make_home(pub, fake_python=fake_py("521"), device_id=None)
    run_update(home, make_release(999, NEW, key), shim)
    ev = events_of(st)
    check("[%s] U10 no DEVICE_ID -> event still names the node" % label,
          '"n":""' not in ev and "healer.selfupdate.ok" in ev)




    # ---- U11-U14: PROMOTION-BASED ROLLBACK (v533). The fallback is the last artifact
    # that PROVED itself on this node, never merely "the previous one".
    # U11 immediate gate: passes selftest from /tmp, fails once installed -> restore .good
    home, w, st = make_home(pub, fake_python=fake_py_installed_fail("521"))
    run_update(home, make_release(999, NEW, key), shim)
    # The artifact that was running had no pending marker, so the gate promoted it to
    # .good before fetching. The failed update therefore lands back on exactly the
    # build that had proved itself - which is the whole point of the design.
    check("[%s] U11 broken-once-installed -> restored to the PROVEN build" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U11 -> emits selfupdate.rollback" % label,
          "healer.selfupdate.rollback" in events_of(st))

    # U12 deferred gate: stale update.pending = the build never completed a real tick
    home, w, st = make_home(pub, fake_python=fake_py("777"))
    open(os.path.join(w, "healer.pyz.good"), "wb").write(b"GOOD-ARTIFACT")
    pend = os.path.join(st, "update.pending")
    open(pend, "w").close(); os.utime(pend, (time.time() - 3600,) * 2)
    run_update(home, make_release(999, NEW, key), shim)
    check("[%s] U12 stale marker -> restored from .good" % label,
          open(os.path.join(w, "healer.pyz"), "rb").read() == b"GOOD-ARTIFACT")
    check("[%s] U12 -> marker cleared so it cannot loop" % label, not os.path.exists(pend))

    # U13 THE FLAW THIS DESIGN FIXES: a bad build must never BECOME the fallback.
    # Marker still present and fresh => unproven => no promotion, and no new fetch.
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    open(os.path.join(w, "healer.pyz.good"), "wb").write(b"GOOD-ARTIFACT")
    open(os.path.join(st, "update.pending"), "w").close()      # fresh: still proving
    run_update(home, make_release(999, NEW, key), shim)
    check("[%s] U13 unproven build is NOT promoted to .good" % label,
          open(os.path.join(w, "healer.pyz.good"), "rb").read() == b"GOOD-ARTIFACT")

    # U14 a build that DID prove itself (no marker, differs from .good) is promoted
    home, w, st = make_home(pub, fake_python=fake_py("521"))
    open(os.path.join(w, "healer.pyz.good"), "wb").write(b"OLDER-GOOD")
    run_update(home, make_release(999, NEW, key), shim)        # no marker present
    check("[%s] U14 proven build promoted to .good" % label,
          open(os.path.join(w, "healer.pyz.good"), "rb").read() == b"INSTALLED-ARTIFACT")
    check("[%s] U14 -> emits selfupdate.promote" % label,
          "healer.selfupdate.promote" in events_of(st))

print("=== selfupdate: both fleet generations ===")
print("  openssl WITH -rawin (pit/pir): %s" % (OSSL_NEW or "ไม่พบ"))
print("  openssl WITHOUT  -rawin (pisn): %s" % (OSSL_OLD or "ไม่พบ"))
check("H0 harness has an OpenSSL 3.x (the pit/pir generation)", bool(OSSL_NEW))
check("H0 harness has a pre-3.x openssl (the pisn generation)", bool(OSSL_OLD))
# H1: without this the no-rawin suite would "pass" by rejecting everything, i.e. it
# would prove nothing about the fallback it exists to test.
_h = mktmp()
_probe = subprocess.run([sys.executable, "-c", "import cryptography"], capture_output=True,
                        env=dict(os.environ, HOME=_h, PYTHONPATH=CRYPTO_PATH))
check("H1 the delegated interpreter can import cryptography under the test env",
      _probe.returncode == 0)

if OSSL_NEW:
    suite("openssl3", OSSL_NEW)
if OSSL_OLD:
    suite("no-rawin", OSSL_OLD)

for d in TMPS:
    shutil.rmtree(d, ignore_errors=True)
print("\n=== RESULTS ===")
for n, ok in R:
    print("%-64s %s" % (n, "PASS" if ok else "FAIL"))
print("TOTAL: %d/%d PASS" % (sum(1 for _, ok in R if ok), len(R)))
sys.exit(0 if all(ok for _, ok in R) else 1)
