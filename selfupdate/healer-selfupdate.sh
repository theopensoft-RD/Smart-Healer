#!/bin/bash
# fleet-healer pull self-update (runs on each node via a systemd timer).
# OUTBOUND HTTPS ONLY -> no netbird, no SSH, no SSO. Verifies an ed25519 signature
# against a baked-in publisher public key, self-tests the new artifact, then installs
# atomically. ANY failure -> keep the current healer (fail-safe). Every outcome is an
# event in the structured stream.
#
# The fleet spans TWO OS generations and this script must work on both:
#   pit/pir (RPi5)  OpenSSL 3.5.5   python 3.13  cryptography 43   pipx venv present
#   pisn    (IRIV)  OpenSSL 1.1.1w  python 3.9   cryptography 3.3  NO pipx venv
# `openssl pkeyutl -rawin` only exists in OpenSSL 3.x, and the pipx venv path only
# exists on RPi5 - the original script assumed both, so every PISN tick rejected the
# update as "bad-signature" (434 rejects on pisn004) even though the signature was
# perfectly valid. Verified 2026-08-04: the published v520 signature checks out.
set -u
BASE="${HEALER_RELEASE_BASE:-https://raw.githubusercontent.com/theopensoft-RD/Smart-Healer/main/dist}"
W="$HOME/.config/pat-smart/workers"
PYZ="$W/healer.pyz"
PUB="$W/healer-release.pub"                 # baked-in publisher key (ed25519)
STATE="$HOME/.local/state/pat-smart"
ENVF="$HOME/.config/pat-smart/.env"

# interpreter: the pipx venv when it exists (RPi5), else the system python (IRIV).
# Getting this wrong makes LV=0 -> every release looks newer -> pointless re-downloads.
PY="$HOME/.local/share/pipx/venvs/pat-smart/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || echo /usr/bin/python3)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
NID="$(grep -m1 '^DEVICE_ID' "$ENVF" 2>/dev/null | cut -d= -f2)"
[ -n "$NID" ] || NID="$(hostname)"          # signage/infra nodes have no DEVICE_ID
# stdout goes to journald: an event still reaches a human when the state dir is
# unwritable (that failure mode hid 649 aborted remediations on 4 nodes for months)
ev(){ printf '%s [selfupdate] %s %s\n' "$(date +%FT%T%z)" "$1" "${2:-{}}" >&2
      { mkdir -p "$STATE" && printf '{"t":%s,"n":"%s","e":"%s","d":%s}\n' \
        "$(date +%s)" "$NID" "$1" "${2:-{}}" >> "$STATE/events.jsonl"; } 2>/dev/null || true; }

# ed25519 verify. 0 = good, 1 = BAD signature, 2 = no verifier on this node.
# openssl first (unchanged fast path for the 58 RPi5 nodes); python-cryptography is
# the fallback AND the authority when openssl is too old to know -rawin.
verify_sig(){
  if openssl pkeyutl -verify -pubin -inkey "$PUB" -rawin -in "$1" -sigfile "$2" >/dev/null 2>&1; then
    return 0
  fi
  if "$PY" -c 'import cryptography' >/dev/null 2>&1; then
    "$PY" - "$PUB" "$1" "$2" <<'PYV' >/dev/null 2>&1
import sys
from cryptography.hazmat.primitives.serialization import load_pem_public_key
load_pem_public_key(open(sys.argv[1], 'rb').read()).verify(
    open(sys.argv[3], 'rb').read(), open(sys.argv[2], 'rb').read())
PYV
    return $?
  fi
  return 2                                  # fail CLOSED: never install unverified
}

[ -f "$PUB" ] || { ev "healer.selfupdate.reject" '{"why":"no-pubkey"}'; exit 0; }

# 1. compare versions (cheap)
RV="$(curl -fsSL --max-time 20 "$BASE/version" 2>/dev/null | tr -dc '0-9')"
[ -z "$RV" ] && exit 0
LV="$("$PY" "$PYZ" --version 2>/dev/null | tr -dc '0-9')"; [ -z "$LV" ] && LV=0
[ "$RV" -le "$LV" ] 2>/dev/null && exit 0    # already current

# 2. fetch artifact + detached signature
curl -fsSL --max-time 60 "$BASE/healer.pyz"     -o "$TMP/healer.pyz"     || { ev "healer.selfupdate.fail" '{"stage":"download"}'; exit 0; }
curl -fsSL --max-time 20 "$BASE/healer.pyz.sig" -o "$TMP/healer.pyz.sig" || { ev "healer.selfupdate.fail" '{"stage":"sig"}'; exit 0; }

# 3. verify -> reject a tampered/unsigned artifact (repo-compromise defense)
verify_sig "$TMP/healer.pyz" "$TMP/healer.pyz.sig"; VRC=$?
if [ "$VRC" = 2 ]; then
  # distinct from bad-signature on purpose: this node CANNOT check anything, which is
  # an operator problem (missing python-cryptography), not evidence of tampering.
  ev "healer.selfupdate.reject" "{\"why\":\"no-verifier\",\"rv\":$RV}"; exit 0; fi
if [ "$VRC" != 0 ]; then
  ev "healer.selfupdate.reject" "{\"why\":\"bad-signature\",\"rv\":$RV}"; exit 0; fi

# 4. self-test the NEW artifact BEFORE it touches the running one
if ! "$PY" "$TMP/healer.pyz" selftest >/dev/null 2>&1; then
  ev "healer.selfupdate.reject" "{\"why\":\"selftest-failed\",\"rv\":$RV}"; exit 0; fi

# 5. atomic install, keep previous for rollback
cp -f "$PYZ" "$PYZ.prev" 2>/dev/null
if install -m0644 "$TMP/healer.pyz" "$PYZ.new" && mv -f "$PYZ.new" "$PYZ"; then
  ev "healer.selfupdate.ok" "{\"from\":$LV,\"to\":$RV}"
else
  ev "healer.selfupdate.fail" '{"stage":"install"}'
fi
