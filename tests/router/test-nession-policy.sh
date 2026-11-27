#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-nession-policy.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/conf.d" "$TMP/certs/le/current"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=*.bestnathan.top' \
  -keyout "$TMP/certs/le/current/tls.key" -out "$TMP/certs/le/current/chain.pem" >/dev/null 2>&1
printf 'events {}\nhttp {\n  include /etc/nginx/conf.d/*.conf;\n}\n' > "$TMP/nginx.conf"
cp router/nginx/cloudflare-ips.inc "$TMP/conf.d/"

IMAGE=docker.io/library/nginx@sha256:2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51
for CFG in nession.conf nession-deny-only.conf; do
  cp "router/nginx/$CFG" "$TMP/conf.d/nession.conf"
  docker run --rm --entrypoint nginx \
    -v "$TMP/nginx.conf:/etc/nginx/nginx.conf:ro" \
    -v "$TMP/conf.d:/etc/nginx/conf.d:ro" \
    -v "$TMP/certs:/etc/nginx/certs:ro" \
    "$IMAGE" -t >/dev/null 2>&1
  printf 'PASS: %s nginx syntax valid\n' "$CFG"
  rm -f "$TMP/conf.d/nession.conf"
done

# Policy assertions on the active MCP-only config.
grep -q 'server_name mcp-nession.bestnathan.top staging-mcp-nession.bestnathan.top;' router/nginx/nession.conf
grep -q 'location = /mcp' router/nginx/nession.conf
grep -q 'proxy_ssl_verify on;' router/nginx/nession.conf
grep -q 'proxy_pass https://192.168.2.86:443;' router/nginx/nession.conf
# shellcheck disable=SC2016
grep -q 'if ($ssl_server_name != $host) { return 444; }' router/nginx/nession.conf
grep -q 'location / { return 404; }' router/nginx/nession.conf
grep -q 'deny all;' router/nginx/cloudflare-ips.inc
# Private/unknown Nession hosts must be denied, not proxied.
grep -q 'return 444;' router/nginx/nession.conf
if grep -q 'proxy_pass' router/nginx/nession-deny-only.conf; then
  echo 'deny-only config must not proxy' >&2; exit 1
fi
printf 'PASS: MCP-only allowlist, exact /mcp, verified HTTPS upstream, private denial\n'
