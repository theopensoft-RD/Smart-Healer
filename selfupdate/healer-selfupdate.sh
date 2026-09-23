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

TMP="$(mktemp -d)"
# Worker OTA (Dashboard-Hardware ota/workers-selfupdate.sh) piggybacks on this timer: one file copy,
# no new unit, no root. It runs on EVERY exit path of this script (this script exits early in the
# common "nothing new" case, so a line at the end would never run), and AFTER this script settled,
# so the healer and the workers never update in the same tick (the workers updater defers while
# update.pending exists). Nothing happens when the file is absent.
trap 'rm -rf "$TMP"; [ -x "$W/workers-selfupdate.sh" ] && "$W/workers-selfupdate.sh" || true' EXIT
NID="$(grep -m1 '^DEVICE_ID' "$ENVF" 2>/dev/null | cut -d= -f2)"
[ -n "$NID" ] || NID="$(hostname)"          # signage/infra nodes have no DEVICE_ID
# stdout goes to journald: an event still reaches a human when the state dir is
# unwritable (that failure mode hid 649 aborted remediations on 4 nodes for months)
ev(){ local d="${2:-}"; [ -n "$d" ] || d='{}'   # NOT "${2:-{}}": bash closes that at the first '}' and appended a
      # stray brace to every event that carried data, so those lines were not JSON (10 of 4061 on PIT003, 2026-09-23)
      printf '%s [selfupdate] %s %s\n' "$(date +%FT%T%z)" "$1" "$d" >&2
      { mkdir -p "$STATE" && printf '{"t":%s,"n":"%s","e":"%s","d":%s}\n' \
        "$(date +%s)" "$NID" "$1" "$d" >> "$STATE/events.jsonl"; } 2>/dev/null || true; }

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


# ---------------------------------------------------------------------------
# 5b. PROMOTION-BASED ROLLBACK, GIT-PROVENANCE ONLY  (Carey's design, v534)
#
# The fallback is the last artifact that BOTH (a) arrived from the signed git release
# channel and (b) proved itself with a real tick on THIS node. Two rules, both needed:
#
#   * "proved itself" - not merely "the previous one". The naive cp-at-install promotes
#     a bad build to fallback one release later: v531 good -> v532 bad leaves .prev=531,
#     but when v533 lands .prev becomes 532 and a rollback goes TO the broken build.
#
#   * "from git" - a node that was hand-fixed on site is running an artifact nobody
#     signed. Seeding .good from whatever happens to be installed would launder that
#     local edit into the trusted rollback target and defeat the signature chain
#     entirely. A hand-fix is left running (it is presumably there for a reason) but it
#     is NEVER promoted, and the node says so via healer.selfupdate.foreign.
#
# Provenance is tracked by recording the sha256 of each artifact the updater installs.
# Running artifact == recorded hash  -> it came from git, may be promoted.
# Running artifact != recorded hash  -> hand-placed, never promoted.
# ---------------------------------------------------------------------------
GOOD="$PYZ.good"
PROV="$STATE/installed.sha256"          # sha256 of the last GIT-VERIFIED install
sha(){ sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || openssl dgst -sha256 "$1" 2>/dev/null | awk '{print $NF}'; }
# NOTE: .good is deliberately NOT seeded from the running artifact. Until a git-verified
# build proves itself there is no trustworthy rollback target, and saying so honestly is
# better than inventing one.

restore_good(){   # $1 = reason, $2 = extra json (no braces)
  if [ ! -s "$GOOD" ]; then
    ev "healer.selfupdate.rollback-impossible" "{\"why\":\"$1\",\"note\":\"no verified .good yet\"}"; return 1; fi
  if install -m0644 "$GOOD" "$PYZ.restore" && mv -f "$PYZ.restore" "$PYZ"; then
    rm -f "$STATE/update.pending"; sha "$PYZ" > "$PROV" 2>/dev/null
    GV="$("$PY" "$PYZ" --version 2>/dev/null | tr -dc '0-9')"
    ev "healer.selfupdate.rollback" "{\"why\":\"$1\",\"restored\":${GV:-0}${2:+,$2}}"; return 0; fi
  ev "healer.selfupdate.rollback-failed" "{\"why\":\"$1\"}"; return 1
}

# Settle the PREVIOUS cycle before considering a new release.
if [ -f "$STATE/update.pending" ]; then
  PEND_AGE=$(( $(date +%s) - $(stat -c %Y "$STATE/update.pending" 2>/dev/null || date +%s) ))
  PEND_MAX="${HEALER_UPDATE_PROVE_S:-900}"      # 15 min ~= 15 healer ticks at 60s
  if [ "$PEND_AGE" -ge "$PEND_MAX" ]; then
    restore_good "no-healthy-tick" "\"age_s\":$PEND_AGE"
  fi
  exit 0                                        # proving, or just restored: settle first
fi
if ! cmp -s "$PYZ" "$GOOD" 2>/dev/null; then
  RUN_SHA="$(sha "$PYZ")"; WANT_SHA="$(cat "$PROV" 2>/dev/null)"
  if [ -n "$WANT_SHA" ] && [ -n "$RUN_SHA" ] && [ "$RUN_SHA" = "$WANT_SHA" ]; then
    CV="$("$PY" "$PYZ" --version 2>/dev/null | tr -dc '0-9')"
    if install -m0644 "$PYZ" "$GOOD.new" && mv -f "$GOOD.new" "$GOOD"; then
      ev "healer.selfupdate.promote" "{\"good\":${CV:-0}}"
      rm -f "$PYZ.prev"          # orphaned by pre-v533 scripts; .good replaces it
    fi
  elif [ -n "$WANT_SHA" ]; then
    # We DO have a record of what we installed, and this is not it: a site hand-fix or
    # tampering. Leave it running (it is presumably there for a reason), refuse to trust
    # it as a fallback, and make sure a human can see it.
    ev "healer.selfupdate.foreign" "{\"note\":\"running artifact not from the signed channel; not promoted\"}"
  fi
  # No provenance record at all (a node this updater has never installed on, e.g. a
  # factory image): unknown, not suspicious. Do not promote, and stay SILENT - a
  # "nothing to do" run must emit nothing, or the event stream becomes unreadable.
fi

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

# 5. atomic install. NOTE: nothing is copied to .prev here any more - the fallback
#    (.good) is written only by the promotion gate above, and only for an artifact
#    that both proved itself AND came from the signed git channel.
if install -m0644 "$TMP/healer.pyz" "$PYZ.new" && mv -f "$PYZ.new" "$PYZ"; then
  # Immediate gate: prove the INSTALLED artifact runs (catches a bad copy/mv and
  # anything that only breaks once in place). .good is NOT touched here - promotion
  # happens only after a healthy tick, on the next run.
  if ! "$PY" "$PYZ" selftest >/dev/null 2>&1; then
    restore_good "installed-selftest-failed" "\"rv\":$RV"
    exit 0
  fi
  sha "$PYZ" > "$PROV" 2>/dev/null          # provenance: this one came from git
  : > "$STATE/update.pending" 2>/dev/null || true
  ev "healer.selfupdate.ok" "{\"from\":$LV,\"to\":$RV}"
else
  ev "healer.selfupdate.fail" '{"stage":"install"}'
fi
