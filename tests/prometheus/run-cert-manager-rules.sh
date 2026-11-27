#!/usr/bin/env bash
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-cert-manager-rules.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/rules"
python3 -I - "$ROOT" "$TMP" <<'PY'
import sys
from pathlib import Path
import yaml
root = Path(sys.argv[1])
target = Path(sys.argv[2])
source = root / "components/observability/prometheus"
config = yaml.safe_load((source / "configmap.yaml").read_text())
rules = yaml.safe_load((source / "configmap-rules.yaml").read_text())
(target / "prometheus.yml").write_text(config["data"]["prometheus.yml"])
(target / "rules/cert-manager.yml").write_text(rules["data"]["cert-manager.yml"])
PY
docker run --rm --entrypoint promtool \
  --tmpfs /tmp:rw,size=64m \
  -v "$TMP/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$TMP/rules:/etc/prometheus/rules:ro" \
  prom/prometheus:v3.11.3 check config /etc/prometheus/prometheus.yml
docker run --rm --entrypoint promtool \
  --tmpfs /tmp:rw,size=64m \
  -v "$ROOT/tests/prometheus/cert-manager-rules.test.yml:/validation/test.yml:ro" \
  -v "$TMP/rules:/etc/prometheus/rules:ro" \
  prom/prometheus:v3.11.3 test rules /validation/test.yml
