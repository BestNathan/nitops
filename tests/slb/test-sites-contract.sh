#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-slb-sites-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/conf.d" "$TMP/certs" "$TMP/certs-active/current"

# The served site configuration lives in a ConfigMap; extract its data verbatim
# so the assertions and the syntax check see exactly what the DaemonSet mounts.
python3 -I - "$TMP/conf.d" <<'PY'
import sys, yaml, os
src = 'components/slb/nginx-proxy/configmap-sites.yaml'
out = sys.argv[1]
with open(src) as fh:
    data = yaml.safe_load(fh)['data']
for name, body in data.items():
    with open(os.path.join(out, name), 'w') as fh:
        fh.write(body)
PY

for pair in certs/nhome certs/default certs-active/current/tls; do
  openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=test' \
    -keyout "$TMP/$pair.key" -out "$TMP/$pair.crt" >/dev/null 2>&1
done

IMAGE=docker.io/library/nginx@sha256:2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51
docker run --rm --entrypoint nginx \
  --add-host higress-gateway.higress-system.svc.cluster.local:127.0.0.1 \
  -v "$TMP/conf.d:/etc/nginx/conf.d:ro" \
  -v "$TMP/certs:/etc/nginx/certs:ro" \
  -v "$TMP/certs-active:/etc/nginx/certs-active:ro" \
  "$IMAGE" -t >/dev/null 2>&1
printf 'PASS: nginx-sites configmap syntax valid\n'

CONF="$TMP/conf.d/00-catch-all.conf"
GW='http://higress-gateway.higress-system.svc.cluster.local:80'

# 1. Single TLS terminator: the SLB terminates client TLS and hands Higress
#    plaintext on the trusted cluster network. A TLS hop to the gateway would
#    make the downstream Ingress TLS secret load-bearing for an internal hop
#    (nession#4588 / nitops#2 Option A).
if grep -q 'proxy_pass https://higress-gateway' "$CONF"; then
  echo 'FAIL: SLB must not proxy to the Higress TLS port; the gateway hop stays plaintext' >&2
  exit 1
fi
test "$(grep -c "proxy_pass $GW;" "$CONF")" -eq 4
printf 'PASS: all four vhosts proxy plaintext to Higress :80\n'

# 2. Scheme enforcement lives at the actual TLS edge for the public zone only.
grep -q 'server_name \*\.bestnathan\.top;' "$CONF"
grep -q 'return 308 https://\$host\$request_uri;' "$CONF"
if grep -q 'listen 80;' "$CONF" && grep -A2 'listen 80;$' "$CONF" | grep -q 'bestnathan'; then :; else
  echo 'FAIL: expected a :80 server block for *.bestnathan.top' >&2; exit 1
fi
printf 'PASS: *.bestnathan.top cleartext is redirected at the edge\n'

# 3. nhome.local keeps its preserved HTTP behavior and its own cert: the public
#    redirect must not leak onto internal names.
if grep -B1 -A2 'listen 80;' "$CONF" | grep -q 'nhome'; then
  echo 'FAIL: nhome.local must not be redirected to HTTPS' >&2; exit 1
fi
grep -q 'ssl_certificate     /etc/nginx/certs/nhome.crt;' "$CONF"
grep -q 'ssl_certificate     /etc/nginx/certs-active/current/tls.crt;' "$CONF"
grep -q 'ssl_certificate     /etc/nginx/certs/default.crt;' "$CONF"
printf 'PASS: cert ownership unchanged (nhome / managed wildcard / default)\n'

# 4. Forwarded identity headers stay on every proxy hop.
test "$(grep -c 'proxy_set_header X-Forwarded-Proto' "$CONF")" -eq 4
test "$(grep -c 'proxy_set_header Upgrade \$http_upgrade;' "$CONF")" -eq 4
printf 'PASS: forwarded-proto and WebSocket upgrade headers preserved\n'

# 5. The DaemonSet must roll when the served configuration changes: nginx does
#    not reload a mounted ConfigMap edit, and the cert-reloader only reacts to
#    certificate rotation.
ACTUAL=$(openssl dgst -sha256 -r components/slb/nginx-proxy/configmap-sites.yaml | cut -d' ' -f1)
DECLARED=$(sed -n 's/.*checksum\/nginx-sites: "\([0-9a-f]*\)".*/\1/p' components/slb/nginx-proxy/daemonset.yaml)
if [ -z "$DECLARED" ]; then
  echo 'FAIL: daemonset.yaml is missing checksum/nginx-sites' >&2; exit 1
fi
if [ "$ACTUAL" != "$DECLARED" ]; then
  echo "FAIL: checksum/nginx-sites drift: file=$ACTUAL annotation=$DECLARED" >&2; exit 1
fi
printf 'PASS: DaemonSet checksum matches the served site configuration\n'
