#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-reloader-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/certs" "$TMP/run"
REAL_OPENSSL=$(command -v openssl)
export REAL_OPENSSL
"$REAL_OPENSSL" req -x509 -newkey rsa:2048 -sha256 -nodes -days 2 \
  -subj /CN=argocd.bestnathan.top -keyout "$TMP/certs/good.key" \
  -out "$TMP/certs/leaf.pem" >/dev/null 2>&1
cat "$TMP/certs/leaf.pem" "$TMP/certs/leaf.pem" > "$TMP/certs/tls.crt"
"$REAL_OPENSSL" req -x509 -newkey rsa:2048 -sha256 -nodes -days 2 \
  -subj /CN=argocd.bestnathan.top -keyout "$TMP/certs/other.key" \
  -out "$TMP/certs/other-leaf.pem" >/dev/null 2>&1
cat "$TMP/certs/other-leaf.pem" "$TMP/certs/other-leaf.pem" > "$TMP/certs/other-chain.pem"
cp "$TMP/certs/good.key" "$TMP/certs/tls.key"
cat > "$TMP/bin/nginx" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
if [ "$1" = "-t" ] && [ "${NGINX_TEST_FAIL:-0}" = "1" ]; then exit 1; fi
SH
cat > "$TMP/bin/openssl" <<'SH'
#!/bin/sh
if [ "$1" = "s_client" ]; then cat "$PEER_CERT"; exit "${PEER_FAIL:-0}"; fi
if [ "$1" = "verify" ]; then exit "${CERT_CHAIN_FAIL:-0}"; fi
exec "$REAL_OPENSSL" "$@"
SH
chmod +x "$TMP/bin/nginx" "$TMP/bin/openssl"
python3 -I - "$TMP/reloader.sh" <<'PY'
import sys
import yaml
source = yaml.safe_load(open("components/slb/nginx-proxy/configmap-reloader.yaml"))
open(sys.argv[1], "w").write(source["data"]["cert-reloader.sh"])
PY
chmod +x "$TMP/reloader.sh"
export PATH="$TMP/bin:$PATH" CALL_LOG="$TMP/calls" CERT_DIR="$TMP/certs"
export RUN_DIR="$TMP/run" PROBE_HOST=argocd.bestnathan.top
export CERT_RELOADER_SOURCE_ONLY=1 PEER_CERT="$TMP/certs/tls.crt"
export RELOADER_SETTLE_SECONDS=0 RELOAD_MIN_INTERVAL=0
export VERIFY_ATTEMPTS=1 VERIFY_DELAY=0
# shellcheck source=/dev/null
source "$TMP/reloader.sh"

cp "$TMP/certs/other.key" "$TMP/certs/tls.key"
LAST_FINGERPRINT=previous
export LAST_RELOAD=0
: > "$CALL_LOG"
if run_once; then echo 'mismatched key unexpectedly succeeded' >&2; exit 1; fi
if grep -q -- '-t' "$CALL_LOG"; then
  echo 'candidate failure reached nginx test' >&2
  exit 1
fi
[ "$LAST_FINGERPRINT" = previous ]
printf 'PASS: mismatched key is rejected before nginx test/reload\n'

cp "$TMP/certs/good.key" "$TMP/certs/tls.key"
export CERT_CHAIN_FAIL=1
LAST_FINGERPRINT=previous
: > "$CALL_LOG"
if run_once; then echo 'untrusted chain unexpectedly succeeded' >&2; exit 1; fi
if grep -q -- '-t' "$CALL_LOG"; then
  echo 'candidate failure reached nginx test' >&2
  exit 1
fi
[ "$LAST_FINGERPRINT" = previous ]
export CERT_CHAIN_FAIL=0
printf 'PASS: untrusted chain is rejected before nginx test/reload\n'

export NGINX_TEST_FAIL=1
LAST_FINGERPRINT=previous
: > "$CALL_LOG"
if run_once; then echo 'invalid nginx config unexpectedly succeeded' >&2; exit 1; fi
if grep -q 'reload' "$CALL_LOG"; then
  echo 'invalid config unexpectedly reloaded nginx' >&2
  exit 1
fi
[ "$LAST_FINGERPRINT" = previous ]
export NGINX_TEST_FAIL=0
printf 'PASS: invalid update retains active configuration\n'

: > "$CALL_LOG"
LAST_FINGERPRINT=previous
run_once
grep -q -- '-s reload' "$CALL_LOG"
[ "$LAST_FINGERPRINT" != previous ]
printf 'PASS: valid update reloads and verifies the actual served serial\n'
