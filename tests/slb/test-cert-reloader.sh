#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-reloader-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/certs" "$TMP/state" "$TMP/ca"
REAL_OPENSSL=$(command -v openssl)
export REAL_OPENSSL

# Build a real trust chain: root CA -> intermediate CA -> wildcard leaf.
"$REAL_OPENSSL" req -x509 -newkey rsa:2048 -sha256 -nodes -days 2 \
  -subj /CN=nitops-test-root -keyout "$TMP/ca/root.key" -out "$TMP/ca/root.pem" \
  -addext basicConstraints=critical,CA:TRUE \
  -addext keyUsage=critical,keyCertSign,cRLSign >/dev/null 2>&1
"$REAL_OPENSSL" req -newkey rsa:2048 -nodes -subj /CN=nitops-test-int \
  -keyout "$TMP/ca/int.key" -out "$TMP/ca/int.csr" >/dev/null 2>&1
printf 'basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n' > "$TMP/ca/int.ext"
"$REAL_OPENSSL" x509 -req -in "$TMP/ca/int.csr" -CA "$TMP/ca/root.pem" \
  -CAkey "$TMP/ca/root.key" -CAcreateserial -days 2 -sha256 \
  -extfile "$TMP/ca/int.ext" -out "$TMP/ca/int.pem" >/dev/null 2>&1

make_leaf() {
  name=$1 san=$2
  "$REAL_OPENSSL" req -newkey rsa:2048 -nodes -subj "/CN=$san" \
    -keyout "$TMP/ca/$name.key" -out "$TMP/ca/$name.csr" >/dev/null 2>&1
  printf 'subjectAltName=DNS:%s\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$san" > "$TMP/ca/$name.ext"
  "$REAL_OPENSSL" x509 -req -in "$TMP/ca/$name.csr" -CA "$TMP/ca/int.pem" \
    -CAkey "$TMP/ca/int.key" -CAcreateserial -days 2 -sha256 \
    -extfile "$TMP/ca/$name.ext" -out "$TMP/ca/$name.pem" >/dev/null 2>&1
  cat "$TMP/ca/$name.pem" "$TMP/ca/int.pem" > "$TMP/ca/$name.chain"
}
make_leaf leafA '*.bestnathan.top'
make_leaf leafB '*.bestnathan.top'
make_leaf leafWrong 'argocd.bestnathan.top'

# A second, untrusted root/intermediate/leaf for the trust-failure case.
"$REAL_OPENSSL" req -x509 -newkey rsa:2048 -sha256 -nodes -days 2 \
  -subj /CN=nitops-untrusted-root -keyout "$TMP/ca/root2.key" -out "$TMP/ca/root2.pem" \
  -addext basicConstraints=critical,CA:TRUE >/dev/null 2>&1
"$REAL_OPENSSL" req -newkey rsa:2048 -nodes -subj /CN=nitops-untrusted-int \
  -keyout "$TMP/ca/int2.key" -out "$TMP/ca/int2.csr" >/dev/null 2>&1
"$REAL_OPENSSL" x509 -req -in "$TMP/ca/int2.csr" -CA "$TMP/ca/root2.pem" \
  -CAkey "$TMP/ca/root2.key" -CAcreateserial -days 2 -sha256 \
  -extfile "$TMP/ca/int.ext" -out "$TMP/ca/int2.pem" >/dev/null 2>&1
"$REAL_OPENSSL" req -newkey rsa:2048 -nodes -subj '/CN=*.bestnathan.top' \
  -keyout "$TMP/ca/leaf2.key" -out "$TMP/ca/leaf2.csr" >/dev/null 2>&1
printf 'subjectAltName=DNS:*.bestnathan.top\nbasicConstraints=CA:FALSE\n' > "$TMP/ca/leaf2.ext"
"$REAL_OPENSSL" x509 -req -in "$TMP/ca/leaf2.csr" -CA "$TMP/ca/int2.pem" \
  -CAkey "$TMP/ca/int2.key" -CAcreateserial -days 2 -sha256 \
  -extfile "$TMP/ca/leaf2.ext" -out "$TMP/ca/leaf2.pem" >/dev/null 2>&1
cat "$TMP/ca/leaf2.pem" "$TMP/ca/int2.pem" > "$TMP/ca/untrusted.chain"

cat > "$TMP/bin/nginx" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
if [ "$1" = "-t" ] && [ "${NGINX_TEST_FAIL:-0}" = "1" ]; then exit 1; fi
SH
cat > "$TMP/bin/openssl" <<'SH'
#!/bin/sh
if [ "$1" = "s_client" ]; then cat "$PEER_CERT"; exit 0; fi
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

export PATH="$TMP/bin:$PATH"
export CALL_LOG="$TMP/calls"
export CERT_DIR="$TMP/certs"
export STATE_DIR="$TMP/state"
export CA_BUNDLE="$TMP/ca/root.pem"
export PROBE_HOST=argocd.bestnathan.top
export CERT_RELOADER_SOURCE_ONLY=1
export RELOADER_SETTLE_SECONDS=0 RELOAD_MIN_INTERVAL=0 VERIFY_ATTEMPTS=1 VERIFY_DELAY=0
export NGINX_TEST_FAIL=0
# shellcheck source=/dev/null
source "$TMP/reloader.sh"

set_candidate() {
  cp "$1" "$CERT_DIR/tls.crt"
  cp "$2" "$CERT_DIR/tls.key"
  cat "$CERT_DIR/tls.crt" "$CERT_DIR/tls.key" | sha256sum | cut -d' ' -f1
}
current_fp() {
  cat "$STATE_DIR/current/tls.crt" "$STATE_DIR/current/tls.key" | sha256sum | cut -d' ' -f1
}
assert_current() {
  actual=$(current_fp)
  [ "$actual" = "$1" ] || { echo "expected current=$1 got=$actual" >&2; exit 1; }
}
reset_calls() { : > "$CALL_LOG"; }
no_nginx_test() {
  if grep -q -- '-t' "$CALL_LOG"; then echo 'candidate failure reached nginx test' >&2; exit 1; fi
}

# 1. Valid update from a cold state establishes current and verifies served serial.
export PEER_CERT="$TMP/ca/leafA.chain"
FP_A=$(set_candidate "$TMP/ca/leafA.chain" "$TMP/ca/leafA.key")
reset_calls
run_once
assert_current "$FP_A"
grep -q -- '-s reload' "$CALL_LOG"
[ "$(cat "$STATE_DIR/verified-fingerprint")" = "$FP_A" ]
printf 'PASS: valid wildcard update reloads and verifies actual served serial\n'

# 2. No-op when the active generation is already verified.
reset_calls
run_once
if grep -q -- '-s reload' "$CALL_LOG"; then echo 'no-op unexpectedly reloaded' >&2; exit 1; fi
printf 'PASS: verified current generation is a no-op\n'

# 3. Mismatched private key is rejected before any nginx test/reload.
cp "$TMP/ca/leafB.key" "$CERT_DIR/tls.key"
reset_calls
if run_once; then echo 'mismatched key unexpectedly succeeded' >&2; exit 1; fi
no_nginx_test
assert_current "$FP_A"
printf 'PASS: mismatched key rejected before nginx\n'

# 4. Untrusted fullchain is rejected by real openssl verify before nginx.
set_candidate "$TMP/ca/untrusted.chain" "$TMP/ca/leaf2.key" >/dev/null
reset_calls
if run_once; then echo 'untrusted chain unexpectedly succeeded' >&2; exit 1; fi
no_nginx_test
assert_current "$FP_A"
printf 'PASS: untrusted chain rejected by real trust verification\n'

# 5. Wrong SAN (single host, not the wildcard) is rejected before nginx.
set_candidate "$TMP/ca/leafWrong.chain" "$TMP/ca/leafWrong.key" >/dev/null
reset_calls
if run_once; then echo 'wrong SAN unexpectedly succeeded' >&2; exit 1; fi
no_nginx_test
assert_current "$FP_A"
printf 'PASS: non-wildcard SAN rejected before nginx\n'

# 6. nginx -t failure rolls back to the previous generation.
FP_B=$(set_candidate "$TMP/ca/leafB.chain" "$TMP/ca/leafB.key")
export NGINX_TEST_FAIL=1
reset_calls
if run_once; then echo 'nginx test failure unexpectedly succeeded' >&2; exit 1; fi
export NGINX_TEST_FAIL=0
assert_current "$FP_A"
[ "$(cat "$STATE_DIR/verified-fingerprint")" = "$FP_A" ]
printf 'PASS: nginx test failure rolls back to last-known-good\n'

# 7. Served-serial mismatch rolls back even after a successful reload.
export PEER_CERT="$TMP/ca/leafA.chain"
reset_calls
if run_once; then echo 'served mismatch unexpectedly succeeded' >&2; exit 1; fi
assert_current "$FP_A"
[ "$(cat "$STATE_DIR/verified-fingerprint")" = "$FP_A" ]
printf 'PASS: served serial mismatch rolls back to last-known-good\n'

# 8. Sidecar restart reconciles a pending rotation instead of stranding it.
export PEER_CERT="$TMP/ca/leafB.chain"
FP_B=$(set_candidate "$TMP/ca/leafB.chain" "$TMP/ca/leafB.key")
export LAST_RELOAD=0
reset_calls
run_once
assert_current "$FP_B"
[ "$(cat "$STATE_DIR/verified-fingerprint")" = "$FP_B" ]
printf 'PASS: sidecar restart reconciles pending rotation\n'
