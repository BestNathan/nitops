# cert-manager GitOps and Edge TLS Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install one pinned cert-manager instance, issue a Let's Encrypt `*.bestnathan.top` certificate through Cloudflare DNS-01, consume it automatically at SLB and Router, enforce MCP-only public Nession access, and prove controlled renewal with real TLS readback.

**Architecture:** An offline PR-0 bootstrap seals the existing GitHub Cloudflare credential, followed by six independently reviewed rollout stages that keep consumers disabled until certificates are Ready. cert-manager exclusively owns `slb/bestnathan-top-tls`; SLB mounts it read-only and validates reloads, while Router polls it through name-restricted RBAC and atomically promotes validated versions. Higress remains HTTP, `nhome.local` and unrelated public services remain unchanged.

**Tech Stack:** Kubernetes 1.35, ArgoCD, cert-manager v1.21.3, Cloudflare DNS-01, Let's Encrypt ACME, SealedSecrets, nginx 1.29.3, Python 3.10+ standard library plus PyYAML for manifest tooling, Prometheus 3.11.3, Blackbox Exporter 0.27.0, systemd, ShellCheck, git/GitHub CLI.

---

## Execution model and hard gates

This is not one unattended deployment. Use a new isolated worktree through `superpowers:using-git-worktrees` for each PR. Do not carry or stage the pre-existing modification to `components/new-api/new-api/deployment.yaml`.

| Stage | Merge/deploy gate | Required live evidence before continuing |
|---|---|---|
| PR-0 | Offline credential bootstrap amendment below | Main-only workflow using the repository-level Secret succeeds; artifact contains only SealedSecret ciphertext and sanitized DNS name/type inventory |
| PR-1 | Tasks 1–3 | CRDs Established, controllers/webhook Ready, Staging Certificate Ready, legacy Issuers safely handled |
| PR-2 | Task 4 | Production Certificate Ready, SAN/chain/key verified, controller metrics and alerts loaded |
| PR-3A | Task 5 | Every SLB node serves the new serial; `nhome.local` and `default` unchanged |
| PR-3B | Task 6 | LAN Blackbox probe and actual-served-certificate alerts are healthy |
| PR-4 | Tasks 7–8 | Router observe-only sync passes; Nginx config and active cert unchanged |
| PR-5 | Task 9 | Router active sync, Nession Host/SNI/path/source policy, LAN/Public probes |
| PR-6 | Tasks 10–11 | Controlled renewal converges everywhere and issue #2 has evidence-backed acceptance |

Any failed gate stops the rollout. Never compensate with `curl -k`, `insecure_skip_verify`, deleting CRDs, deleting the production Secret, or manually copying a renewed private key.

## Approved PR-0 amendment: offline credential sealing

The owner confirmed that the reusable credential is the GitHub Actions Secret `BestNathan/nitops/CLOUDFLARE_API_TOKEN`, not a K8s Secret. GitHub Secrets cannot be read locally. The local plaintext-token steps in Task 0 Step 4, Task 2 Step 5, and Task 3 Step 5 are therefore replaced by this offline bootstrap.

### PR-0 source and safety gate

**Files:**
- Create: `.github/workflows/seal-cert-manager-cloudflare.yml`
- Create: `components/sealed-secrets/public-cert.pem`
- Create: `scripts/cert_manager/test_bootstrap_workflow.py`

- [ ] Write `test_bootstrap_workflow.py` first and verify it fails because the workflow is absent. Tests must require `workflow_dispatch`, a main-only job condition, no GitHub Environment, direct use of the repository-level Secret, `contents: read`, full-SHA action pins, `persist-credentials: false`, a five-minute timeout, one-day artifact retention, and secret env only on the Seal and DNS inventory/validation steps.
- [ ] Copy the already inspected public sealing certificate from `/tmp/nitops-sealed-secrets-public-cert.pem`; assert its SHA-256 fingerprint is `25:01:16:0D:24:A7:A6:21:A8:C1:CA:16:C5:B5:DD:6A:0D:F3:AF:6B:14:C2:6A:1F:0D:D4:F5:5D:F8:65:B3:1A`. It is a public key, not a private sealing key.
- [ ] Implement the workflow with no checkout credentials, no shell xtrace, and no Token in job-level env. Before any secret-bearing step, download `kubeseal-0.27.1-linux-amd64.tar.gz` into a new runner-temp download directory, require SHA-256 `01069a5e79ed7702ba10bed42cd8132d94746ca288add06836d082b4202c2789`, and extract the binary into a separate runner-temp bin directory.
- [ ] In the Seal step, use Python standard library to turn the env Token directly into a namespaced `Secret/cert-manager/cloudflare-api-token` JSON stream and pipe it to offline `kubeseal --cert components/sealed-secrets/public-cert.pem --format yaml --scope strict`. Never write the raw Secret to disk. Parse the generated YAML in the same step and reject any unexpected artifact type, top-level/template Secret name or Namespace, missing or additional encrypted data keys, unencrypted `data`, or raw/Base64 Token occurrence.
- [ ] In the DNS inventory step, follow Nession's REST preflight: do not call `/user/tokens/verify` or any standalone Token verify endpoint. Retrieve exactly one active `bestnathan.top` Zone through the bearer-authenticated Zone GET, then write paginated, sorted `{name,type}` records to `dist/cloudflare-records.json`. Do not write record content, IPs, Account IDs, Zone IDs, Token IDs, or credentials. Add a mocked-HTTP behavior test that executes the inline inventory Python and asserts the Zone/records-only call sequence and sanitized output. Zone reads prove actual read authorization; Staging DNS-01 separately proves DNS write authorization.
- [ ] In the same secret-bearing step, assert neither the raw nor base64 Token occurs in either dist file. Upload only `dist/sealedsecret-cloudflare-api-token.yaml` and `dist/cloudflare-records.json` with `retention-days: 1`.
- [ ] Add executable behavior tests: prove a wrong-identity SealedSecret is rejected, prove alternate Secret-expression spellings cannot bypass step scoping, and execute the real Seal shell with a dummy Token through verified kubeseal v0.27.1. Capture its argv to require the exact certificate/format/strict-scope arguments and parse the result to verify the top-level/template identity, encrypted-only token, and absence of raw/Base64 Token. Put verified kubeseal v0.27.1 on `PATH`, then run `KUBESEAL_BIN="$(command -v kubeseal)" python3 scripts/cert_manager/test_bootstrap_workflow.py -v`. Do not use the real GitHub Secret locally.
- [ ] Commit PR-0 after all tests pass. Before dispatch, the owner must confirm the repository Actions Secret `CLOUDFLARE_API_TOKEN` exists and manually dispatch the workflow from `main`; no GitHub Environment or deployment approval is used. Merge and dispatch remain separate operator gates.

### Consume the PR-0 artifact

After PR-0 merges, dispatch only from `main`:

```bash
set -euo pipefail
BOOTSTRAP_ID=$(python3 -I -c 'import uuid; print(uuid.uuid4())')
EXPECTED_TITLE="Seal cert-manager Cloudflare bootstrap $BOOTSTRAP_ID"
gh workflow run seal-cert-manager-cloudflare.yml --repo BestNathan/nitops --ref main \
  -f bootstrap_id="$BOOTSTRAP_ID"
RUNS_JSON=$(mktemp /tmp/nitops-seal-runs.XXXXXX)
for ATTEMPT in {1..30}; do
  gh run list --repo BestNathan/nitops --workflow seal-cert-manager-cloudflare.yml \
    --event workflow_dispatch --branch main --limit 100 \
    --json databaseId,displayTitle > "$RUNS_JSON"
  RUN_ID=$(python3 -I - "$RUNS_JSON" "$EXPECTED_TITLE" <<'PY'
import json,sys
from pathlib import Path
runs=json.loads(Path(sys.argv[1]).read_text())
matches=[run['databaseId'] for run in runs if run['displayTitle']==sys.argv[2]]
if len(matches)>1:
    raise RuntimeError('Duplicate bootstrap dispatch identifier')
print(matches[0] if matches else '')
PY
)
  if test -n "$RUN_ID"; then break; fi
  sleep 2
done
test -n "$RUN_ID"
rm -f "$RUNS_JSON"
gh run watch "$RUN_ID" --repo BestNathan/nitops --exit-status
ARTIFACT_DIR=$(mktemp -d /tmp/nitops-sealed-cloudflare.XXXXXX)
ARTIFACT_NAME="cert-manager-cloudflare-$RUN_ID"
gh run download "$RUN_ID" --repo BestNathan/nitops --name "$ARTIFACT_NAME" --dir "$ARTIFACT_DIR"
python3 -I - "$ARTIFACT_DIR" <<'PY'
import json,sys,yaml
from pathlib import Path
root=Path(sys.argv[1])
sealed=yaml.safe_load((root/'sealedsecret-cloudflare-api-token.yaml').read_text())
assert sealed['kind']=='SealedSecret'
assert sealed['metadata']['name']=='cloudflare-api-token'
assert sealed['metadata']['namespace']=='cert-manager'
assert 'token' not in sealed.get('data',{})
records=json.loads((root/'cloudflare-records.json').read_text())
assert records and all(set(item)=={'name','type'} for item in records)
print('validated encrypted artifact and sanitized records:',len(records))
PY
```

Use the SealedSecret artifact for Task 2 Step 5 and copy `cloudflare-records.json` to the validated Task 0 backup as `cloudflare-records-before.json`. Add only the ArgoCD sync-wave annotation to the SealedSecret metadata; do not alter its encrypted data, name, or Namespace.

For Task 3 Step 5, rerun the same main-only workflow after Staging issuance, download its new artifact into a different new empty directory, and compare the new `cloudflare-records.json` with `cloudflare-records-before.json`. Do not replace the committed SealedSecret with the second randomized ciphertext merely because the workflow was rerun.

## File map

| File | Responsibility |
|---|---|
| `.github/workflows/seal-cert-manager-cloudflare.yml` | Main-only PR-0 offline sealing and sanitized DNS inventory using the existing repository-level GitHub Secret |
| `components/sealed-secrets/public-cert.pem` | Public offline SealedSecrets encryption certificate |
| `scripts/cert_manager/test_bootstrap_workflow.py` | PR-0 workflow security contract tests |
| `scripts/cert_manager/render_upstream.py` | Verify the expected upstream document count, split 47 resources into one-resource files, and apply reviewed version/digest/scheduling/proxy/DNS transforms |
| `scripts/cert_manager/test_render_upstream.py` | Deterministic tests for transforms and output layout |
| `scripts/cert_manager/test_manifests.py` | Contract tests for ArgoCD, Issuer, Certificate, Secret-name uniqueness, and prohibited changes |
| `apps/cert-manager.yaml` | Single recursive ArgoCD child Application with SSA |
| `components/cert-manager/crds/*` | Six protected upstream CRDs |
| `components/cert-manager/controller/*` | Upstream controller, webhook, cainjector, Service, RBAC resources |
| `components/cert-manager/issuers/*` | Staging and Production Cloudflare DNS-01 ClusterIssuers |
| `components/cert-manager/certificates/*` | Temporary Staging test Certificate, then one production wildcard Certificate |
| `components/cert-manager/secrets/sealedsecret-cloudflare-api-token.yaml` | Encrypted Cloudflare token only |
| `components/slb/nginx-proxy/configmap-reloader.yaml` | Bounded nginx test/reload and real-SNI serial readback script |
| `components/slb/nginx-proxy/daemonset.yaml` | Pinned multi-arch nginx image, Secret volume, shared PID namespace/run directory, reload container |
| `components/slb/nginx-proxy/configmap-sites.yaml` | Only `bestnathan` cert paths change |
| `components/observability/prometheus/*` | cert-manager metrics, Blackbox probes, and alert rules |
| `components/cert-manager/router-rbac/*` | ServiceAccount and `get`-only access to one named Secret |
| `router/cert-sync/cert_sync.py` | Router fetch, OpenSSL validation, atomic version promotion, rollback, and local served-serial readback |
| `router/cert-sync/test_cert_sync.py` | Router failure/rollback unit tests |
| `router/cert-sync/nitops-cert-sync.service` | Observe-only first, active sync only after PR-5 cutover |
| `router/cert-sync/nitops-cert-sync.timer` | Five-minute polling |
| `router/nginx/nession.conf` | Exact MCP allowlist, default Nession denial, Cloudflare sources, verified HTTPS upstream |
| `docs/runbooks/cert-manager.md` | Upgrade, recovery, Router token rotation, rollback, and sanitized evidence commands |

Pinned artifacts already resolved for this plan:

```text
kubeseal-0.27.1-linux-amd64.tar.gz sha256=01069a5e79ed7702ba10bed42cd8132d94746ca288add06836d082b4202c2789
cert-manager.yaml sha256=c04a51295adda2b1a775146751f973e787190778c7f35180861d9678876e5c74
cert-manager-controller sha256=9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc
cert-manager-cainjector sha256=083d0e906f8eb44e255fe4ff817ae54e49f9ed82e22f1011bb4e9e4f4b8a641f
cert-manager-webhook sha256=055ac8e9de6a0ba6763794838fe38944acc650f68ebe1b01dd8be079e580f458
cert-manager-acmesolver sha256=c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7
nginx:1.29.3 sha256=2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51
prom/blackbox-exporter:v0.27.0 sha256=a50c4c0eda297baa1678cd4dc4712a67fdea713b832d43ce7fcc5f9bea05094d
cmctl v2.6.2 darwin_arm64 sha256=c05aeddc8ba789942529ca99c284f8de655d8bc7de3680d87bea7452e86f30f2
```

### Task 0: Read-only preflight and isolated branch

**Files:**
- Create outside Git: a mode-0700 temporary backup directory
- No repository changes

- [ ] **Step 1: Verify local tools and upstream artifact availability**

Run:

```bash
python3 -I -c 'import yaml; assert yaml.__version__ == "6.0.3"'
command -v kubeseal kubectl gh crane shellcheck openssl jq
kubectl version -o json | jq -r '.serverVersion.gitVersion'
```

Expected: all commands exist; Server reports `v1.35.6`. If PyYAML differs, use a clean virtual environment with PyYAML 6.0.3 rather than changing the renderer to depend on an unreviewed version.

- [ ] **Step 2: Confirm there is no competing live controller and save non-secret recovery data**

Run:

```bash
BACKUP_DIR=$(mktemp -d /tmp/nitops-cert-manager-backup.XXXXXX)
chmod 700 "$BACKUP_DIR"
printf '%s\n' "$BACKUP_DIR"
CRDS=$(kubectl get crd -o name | rg 'cert-manager\.io')
kubectl get $CRDS -o yaml > "$BACKUP_DIR/crds.yaml"
kubectl get issuers,certificates -A -o yaml > "$BACKUP_DIR/issuers-certificates.yaml"
kubectl get deployments -A -o wide | rg 'cert-manager|certmanager' || true
kubectl get certificates -A --no-headers | wc -l
```

Expected: six CRDs backed up, no controller Deployment, zero Certificates. Do not export TLS or ACME account Secrets into this directory.

- [ ] **Step 3: Record the current certificate fingerprints and unrelated route baseline**

Run once per SLB Pod without printing private keys:

```bash
for POD in $(kubectl -n slb get pods -l app=nginx-proxy -o name); do
  kubectl -n slb exec "$POD" -- sh -c 'for C in nhome bestnathan default; do sha256sum "/etc/nginx/certs/$C.crt"; done'
done
ssh root@192.168.2.106 'docker exec nginx-proxy nginx -t; sha256sum /root/docker-nginx/certs/argocd.crt /root/docker-nginx/certs/nession.crt'
```

Expected: all nginx syntax checks pass. Store only these public hashes and HTTP status results in the stage evidence, not certificate private keys.

- [ ] **Step 4: Save a sanitized pre-issuance Cloudflare record inventory**

Use only the validated artifact from the PR-0 `Consume the PR-0 artifact` procedure. Do not read, re-enter, or write the Cloudflare Token locally. In the same shell where `ARTIFACT_DIR` names the downloaded PR-0 artifact, run:

```bash
test -d "$BACKUP_DIR" && test ! -L "$BACKUP_DIR"
cp "$ARTIFACT_DIR/cloudflare-records.json" "$BACKUP_DIR/cloudflare-records-before.json"
python3 -I - "$BACKUP_DIR/cloudflare-records-before.json" <<'PY'
import json,sys
from pathlib import Path
records=json.loads(Path(sys.argv[1]).read_text())
assert records and all(set(item)=={'name','type'} for item in records)
print('validated sanitized pre-issuance records:',len(records))
PY
```

Expected: a non-empty name/type inventory with no record content, credentials, or private addresses. The mode-0700 backup directory remains local.

- [ ] **Step 5: Verify amd64 egress and public recursive DNS before installing**

```bash
ssh root@192.168.2.86 'curl -fsS --max-time 20 --proxy http://192.168.2.98:7895 https://acme-staging-v02.api.letsencrypt.org/directory >/dev/null; curl -fsS --max-time 20 --proxy http://192.168.2.98:7895 https://www.cloudflare.com/ips-v4 >/dev/null; dig @1.1.1.1 bestnathan.top NS +time=5 +tries=1 +noall +answer +comments; dig @8.8.8.8 bestnathan.top NS +time=5 +tries=1 +noall +answer +comments'
```

Expected: both HTTPS requests succeed and both public resolvers return `NOERROR` with the Cloudflare nameservers. Any failure stops PR-1 before ArgoCD sync.

### Task 1: Reproducible upstream renderer

**Files:**
- Create: `scripts/cert_manager/render_upstream.py`
- Create: `scripts/cert_manager/test_render_upstream.py`
- Generate: `components/cert-manager/{namespace,crds,controller}/*.yaml`

- [ ] **Step 1: Write the failing renderer tests**

```python
# scripts/cert_manager/test_render_upstream.py
import tempfile
import unittest
from pathlib import Path

import yaml

from render_upstream import render_documents, transform


class RenderTests(unittest.TestCase):
    def test_crd_is_protected_and_images_are_pinned(self):
        crd = {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "certificates.cert-manager.io"},
            "spec": {},
        }
        transformed = transform(crd)
        annotations = transformed["metadata"]["annotations"]
        self.assertEqual(annotations["argocd.argoproj.io/sync-wave"], "-20")
        self.assertEqual(
            annotations["argocd.argoproj.io/sync-options"],
            "Prune=false,Delete=false",
        )

        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "cert-manager"},
            "spec": {"template": {"spec": {"containers": [{
                "name": "cert-manager-controller",
                "image": "quay.io/jetstack/cert-manager-controller:v1.21.3",
                "args": ["--acme-http01-solver-image=quay.io/jetstack/cert-manager-acmesolver:v1.21.3"],
                "env": [{"name": "POD_NAMESPACE"}],
            }]}}},
        }
        result = transform(deployment)
        pod = result["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"]["kubernetes.io/arch"], "amd64")
        container = pod["containers"][0]
        self.assertTrue(container["image"].endswith(
            "@sha256:9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc"
        ))
        self.assertIn("--dns01-recursive-nameservers-only=true", container["args"])
        self.assertIn(
            "--acme-http01-solver-image=quay.io/jetstack/cert-manager-acmesolver@sha256:c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7",
            container["args"],
        )
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["HTTPS_PROXY"], "http://192.168.2.98:7895")
        self.assertIn(".svc", env["NO_PROXY"])

    def test_one_resource_per_file(self):
        docs = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "cert-manager"}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
                "name": "cert-manager", "namespace": "cert-manager"}},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            paths = render_documents(docs, Path(tmp))
            self.assertEqual(len(paths), 2)
            for path in paths:
                loaded = list(yaml.safe_load_all(path.read_text()))
                self.assertEqual(len(loaded), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify the expected failure**

Run: `python3 -m unittest discover -s scripts/cert_manager -p 'test_render_upstream.py' -v`

Expected: FAIL because `render_upstream.py` does not exist.

- [ ] **Step 3: Implement the complete renderer**

```python
#!/usr/bin/env python3
# scripts/cert_manager/render_upstream.py
import argparse
import re
from pathlib import Path

import yaml

IMAGES = {
    "quay.io/jetstack/cert-manager-controller:v1.21.3":
        "quay.io/jetstack/cert-manager-controller@sha256:9f4f96a33c7774cef24466fcd85031997531a03a8d117f151126baa8d1290dcc",
    "quay.io/jetstack/cert-manager-cainjector:v1.21.3":
        "quay.io/jetstack/cert-manager-cainjector@sha256:083d0e906f8eb44e255fe4ff817ae54e49f9ed82e22f1011bb4e9e4f4b8a641f",
    "quay.io/jetstack/cert-manager-webhook:v1.21.3":
        "quay.io/jetstack/cert-manager-webhook@sha256:055ac8e9de6a0ba6763794838fe38944acc650f68ebe1b01dd8be079e580f458",
    "quay.io/jetstack/cert-manager-acmesolver:v1.21.3":
        "quay.io/jetstack/cert-manager-acmesolver@sha256:c8c1225bb2fe8ab37a330b77f0d8c2ee6a41210fcb54fc07ca606ab9a022b8b7",
}
NO_PROXY = "localhost,127.0.0.1,.svc,.cluster.local,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"


def transform(document):
    metadata = document["metadata"]
    annotations = metadata.setdefault("annotations", {})
    annotations["argocd.argoproj.io/sync-wave"] = "0"
    if document["kind"] == "CustomResourceDefinition":
        annotations["argocd.argoproj.io/sync-wave"] = "-20"
        annotations["argocd.argoproj.io/sync-options"] = "Prune=false,Delete=false"
    elif document["kind"] == "Namespace":
        annotations["argocd.argoproj.io/sync-wave"] = "-10"

    if document["kind"] != "Deployment":
        return document

    pod = document["spec"]["template"]["spec"]
    pod["nodeSelector"] = {"kubernetes.io/arch": "amd64"}
    for container in pod["containers"]:
        container["image"] = IMAGES[container["image"]]
        if metadata["name"] != "cert-manager":
            continue
        args = container.setdefault("args", [])
        solver_flag = "--acme-http01-solver-image="
        args[:] = [
            solver_flag + IMAGES[arg[len(solver_flag):]] if arg.startswith(solver_flag) else arg
            for arg in args
        ]
        args.extend([
            "--dns01-recursive-nameservers=1.1.1.1:53,8.8.8.8:53",
            "--dns01-recursive-nameservers-only=true",
        ])
        env = container.setdefault("env", [])
        env.extend([
            {"name": "HTTP_PROXY", "value": "http://192.168.2.98:7895"},
            {"name": "HTTPS_PROXY", "value": "http://192.168.2.98:7895"},
            {"name": "NO_PROXY", "value": NO_PROXY},
        ])
    return document


def output_path(document, root):
    kind = document["kind"]
    name = document["metadata"]["name"]
    if kind == "CustomResourceDefinition":
        return root / "crds" / f"{name}.yaml"
    if kind == "Namespace":
        return root / "namespace" / "namespace.yaml"
    safe_name = re.sub(r"[^a-zA-Z0-9.-]+", "-", name)
    return root / "controller" / f"{kind.lower()}-{safe_name}.yaml"


def render_documents(documents, root):
    paths = []
    for original in documents:
        document = transform(original)
        path = output_path(document, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(
            document, sort_keys=False, explicit_start=True, width=120))
        paths.append(path)
    return paths


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("upstream", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    with args.upstream.open(encoding="utf-8") as stream:
        documents = [doc for doc in yaml.safe_load_all(stream) if doc]
    if len(documents) != 47:
        raise SystemExit(f"expected 47 upstream resources, got {len(documents)}")
    paths = render_documents(documents, args.output)
    print(f"rendered {len(paths)} one-resource files")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and verify PASS**

Run: `python3 -m unittest discover -s scripts/cert_manager -p 'test_render_upstream.py' -v`

Expected: `Ran 2 tests ... OK`.

- [ ] **Step 5: Download to a new empty directory, verify, and render**

Run:

```bash
UPSTREAM_DIR=$(mktemp -d /tmp/nitops-cert-manager-v1.21.3.XXXXXX)
curl -fsSL https://github.com/cert-manager/cert-manager/releases/download/v1.21.3/cert-manager.yaml -o "$UPSTREAM_DIR/cert-manager.yaml"
printf '%s  %s\n' 'c04a51295adda2b1a775146751f973e787190778c7f35180861d9678876e5c74' "$UPSTREAM_DIR/cert-manager.yaml" | shasum -a 256 -c -
python3 -I scripts/cert_manager/render_upstream.py "$UPSTREAM_DIR/cert-manager.yaml" components/cert-manager
find components/cert-manager -type f -name '*.yaml' | wc -l
```

Expected: checksum `OK`, `rendered 47 one-resource files`, count `47`. The downloaded YAML is data only; the reviewed script remains outside the download directory and receives its path as an argument.

- [ ] **Step 6: Validate generated resources and commit only this tooling slice**

Run:

```bash
kubectl apply --dry-run=client -R -f components/cert-manager >/dev/null
python3 -m unittest discover -s scripts/cert_manager -p 'test_*.py' -v
git add scripts/cert_manager components/cert-manager/namespace components/cert-manager/crds components/cert-manager/controller
git commit -m 'feat(cert-manager): add pinned v1.21.3 upstream renderer' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

Expected: client dry-run accepts all 47 resources; tests pass; no unrelated file is staged.

### Task 2: PR-1 ArgoCD, DNS-01 Issuers, Staging Certificate

**Files:**
- Create: `apps/cert-manager.yaml`
- Create: `components/cert-manager/issuers/clusterissuer-letsencrypt-staging.yaml`
- Create: `components/cert-manager/issuers/clusterissuer-letsencrypt-production.yaml`
- Create: `components/cert-manager/certificates/certificate-bestnathan-staging-test.yaml`
- Create: `components/cert-manager/secrets/sealedsecret-cloudflare-api-token.yaml`
- Create: `scripts/cert_manager/test_manifests.py`

- [ ] **Step 1: Write the failing manifest contract tests**

```python
# scripts/cert_manager/test_manifests.py
import unittest
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]


def docs(pattern):
    result = []
    for path in ROOT.glob(pattern):
        result.extend(doc for doc in yaml.safe_load_all(path.read_text()) if doc)
    return result


class ManifestContracts(unittest.TestCase):
    def test_single_recursive_argocd_application(self):
        app = yaml.safe_load((ROOT / "apps/cert-manager.yaml").read_text())
        self.assertTrue(app["spec"]["source"]["directory"]["recurse"])
        self.assertIn("ServerSideApply=true", app["spec"]["syncPolicy"]["syncOptions"])

    def test_issuers_and_certificate(self):
        issuers = docs("components/cert-manager/issuers/*.yaml")
        self.assertEqual({d["metadata"]["name"] for d in issuers},
                         {"letsencrypt-staging", "letsencrypt-production"})
        for issuer in issuers:
            acme = issuer["spec"]["acme"]
            self.assertNotIn("skipTLSVerify", acme)
            self.assertNotIn("dns01RecursiveNameservers", acme)
            solver = acme["solvers"][0]
            self.assertEqual(solver["selector"]["dnsZones"], ["bestnathan.top"])
            self.assertEqual(solver["dns01"]["cloudflare"]["apiTokenSecretRef"], {
                "name": "cloudflare-api-token", "key": "token"})
        certificates = docs("components/cert-manager/certificates/*.yaml")
        self.assertEqual(len(certificates), 1)
        staging = certificates[0]
        self.assertEqual(staging["metadata"]["name"], "bestnathan-staging-test")
        self.assertEqual(staging["spec"]["issuerRef"]["name"], "letsencrypt-staging")
        self.assertEqual(staging["spec"]["dnsNames"], ["*.bestnathan.top"])
        self.assertEqual(staging["spec"]["privateKey"]["rotationPolicy"], "Always")

    def test_no_competing_secret_writer_or_private_ca(self):
        sealed = docs("components/cert-manager/secrets/*.yaml")
        for item in sealed:
            self.assertNotEqual(item["metadata"]["name"], "bestnathan-top-tls")
        all_text = "\n".join(path.read_text() for path in (ROOT / "components/cert-manager").rglob("*.yaml"))
        self.assertNotIn("BEGIN PRIVATE KEY", all_text)
        self.assertNotIn("apiToken:", all_text)
        self.assertNotIn("nhome-root-ca", all_text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify FAIL**

Run: `python3 scripts/cert_manager/test_manifests.py -v`

Expected: FAIL because `apps/cert-manager.yaml` is absent.

- [ ] **Step 3: Add the ArgoCD Application**

```yaml
# apps/cert-manager.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: cert-manager
  namespace: argocd
  labels:
    app: cert-manager
spec:
  project: default
  source:
    repoURL: https://github.com/BestNathan/nitops.git
    targetRevision: main
    path: components/cert-manager
    directory:
      recurse: true
  destination:
    server: https://kubernetes.default.svc
    namespace: cert-manager
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - ServerSideApply=true
```

- [ ] **Step 4: Generate both Issuers from the reviewed GitHub ACME_EMAIL variable**

Read the repository Actions Variable `ACME_EMAIL` without printing its value:

```bash
python3 -I - <<'PY'
import subprocess
from pathlib import Path
import yaml

email = subprocess.run(
    ["gh", "api", "repos/BestNathan/nitops/actions/variables/ACME_EMAIL", "--jq", ".value"],
    check=True, capture_output=True, text=True,
).stdout.strip()
if "@" not in email or "\n" in email:
    raise SystemExit("invalid ACME email")
root = Path("components/cert-manager/issuers")
root.mkdir(parents=True, exist_ok=True)
for name, server in [
    ("letsencrypt-staging", "https://acme-staging-v02.api.letsencrypt.org/directory"),
    ("letsencrypt-production", "https://acme-v02.api.letsencrypt.org/directory"),
]:
    document = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {
            "name": name,
            "annotations": {"argocd.argoproj.io/sync-wave": "10"},
        },
        "spec": {"acme": {
            "email": email,
            "server": server,
            "privateKeySecretRef": {"name": f"{name}-account-key"},
            "solvers": [{
                "selector": {"dnsZones": ["bestnathan.top"]},
                "dns01": {"cloudflare": {"apiTokenSecretRef": {
                    "name": "cloudflare-api-token", "key": "token"}}},
            }],
        }},
    }
    (root / f"clusterissuer-{name}.yaml").write_text(
        yaml.safe_dump(document, sort_keys=False, explicit_start=True))
print("rendered 2 ClusterIssuers from repository ACME_EMAIL")
PY
```

Expected: two files with the same reviewed email, distinct ACME servers/account keys, and no per-Issuer recursive-DNS field. The controller flags from Task 1 own public recursive DNS.

- [ ] **Step 5: Import the PR-0 SealedSecret without reading the Token locally**

Use the original validated PR-0 artifact directory. Do not create a local Token file, invoke online sealing against the controller, or replace the credential through Kubernetes. Copy and annotate only the encrypted document:

```bash
mkdir -p components/cert-manager/secrets
python3 -I - "$ARTIFACT_DIR/sealedsecret-cloudflare-api-token.yaml" <<'PY'
import sys,yaml
from pathlib import Path
source=Path(sys.argv[1])
document=yaml.safe_load(source.read_text())
assert document['kind']=='SealedSecret'
assert document['metadata']['name']=='cloudflare-api-token'
assert document['metadata']['namespace']=='cert-manager'
assert set(document['spec']['encryptedData'])=={'token'}
assert 'data' not in document and 'data' not in document['spec'].get('template',{})
document['metadata'].setdefault('annotations',{})['argocd.argoproj.io/sync-wave']='10'
target=Path('components/cert-manager/secrets/sealedsecret-cloudflare-api-token.yaml')
target.write_text(yaml.safe_dump(document,sort_keys=False,explicit_start=True))
PY
```

Expected: only encrypted `SealedSecret/cloudflare-api-token` is written, with sync wave `"10"`; no plaintext credential is read or written locally.

- [ ] **Step 6: Add only the temporary Staging Certificate**

```yaml
# components/cert-manager/certificates/certificate-bestnathan-staging-test.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: bestnathan-staging-test
  namespace: cert-manager
  annotations:
    argocd.argoproj.io/sync-wave: "20"
spec:
  secretName: bestnathan-staging-test-tls
  issuerRef:
    name: letsencrypt-staging
    kind: ClusterIssuer
    group: cert-manager.io
  dnsNames:
    - "*.bestnathan.top"
  privateKey:
    rotationPolicy: Always
```

- [ ] **Step 7: Run all deterministic checks**

Run:

```bash
python3 -m unittest discover -s scripts/cert_manager -p 'test_*.py' -v
kubectl apply --dry-run=client -R -f components/cert-manager >/dev/null
kubectl apply --dry-run=client -f apps/cert-manager.yaml >/dev/null
```

Expected: all tests PASS, including Staging-only behavior; client dry-run accepts every resource.

- [ ] **Step 8: Commit PR-1**

```bash
git add apps/cert-manager.yaml components/cert-manager scripts/cert_manager
git diff --cached --check
git commit -m 'feat(cert-manager): add Cloudflare DNS-01 and staging issuance' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

Expected: only cert-manager files and tests are staged. Obtain approval before pushing/opening PR-1.

### Task 3: PR-1 live readiness gate

**Files:**
- No source changes
- Evidence: sanitized command output attached to issue #2 only after operator approval

- [ ] **Step 1: Merge PR-1 and read ArgoCD/Kubernetes state independently**

After explicit merge approval:

```bash
kubectl -n argocd get application apps cert-manager \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
kubectl wait --for=condition=Established crd/certificates.cert-manager.io --timeout=5m
kubectl -n cert-manager rollout status deployment/cert-manager --timeout=5m
kubectl -n cert-manager rollout status deployment/cert-manager-cainjector --timeout=5m
kubectl -n cert-manager rollout status deployment/cert-manager-webhook --timeout=5m
```

Expected: Applications Synced/Healthy and all Deployments fully available. If ArgoCD is Healthy but a Deployment is not, the gate remains failed.

- [ ] **Step 2: Remove only verified-unused legacy Issuers**

Run readback first:

```bash
kubectl get certificates -A
kubectl get issuer gitlab-issuer -n default -o name
kubectl get issuer gitlab-issuer -n gitlab -o name
```

Expected: still no legacy Certificate. With operator approval, remove exactly the two historical Issuers:

```bash
kubectl -n default delete issuer gitlab-issuer
kubectl -n gitlab delete issuer gitlab-issuer
```

Do not delete any CRD or Secret.

- [ ] **Step 3: Verify Staging DNS-01 issuance**

```bash
kubectl wait --for=condition=Ready clusterissuer/letsencrypt-staging --timeout=10m
kubectl -n cert-manager wait --for=condition=Ready certificate/bestnathan-staging-test --timeout=15m
kubectl -n cert-manager get challenges,orders,certificaterequests
kubectl -n cert-manager logs deployment/cert-manager --since=30m | rg -i 'error|failed|challenge'
```

Expected: Issuer and Certificate Ready, no remaining Challenge, no private business record creation. Logs may contain historical transient retries; unresolved current failures block the gate.

- [ ] **Step 4: Verify public certificate metadata without exposing the key**

```bash
CERT_DIR=$(mktemp -d /tmp/nitops-staging-cert.XXXXXX)
kubectl -n cert-manager get secret bestnathan-staging-test-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > "$CERT_DIR/tls.crt"
openssl x509 -in "$CERT_DIR/tls.crt" -noout -subject -issuer -serial -dates -ext subjectAltName
rm -rf "$CERT_DIR"
dig TXT _acme-challenge.bestnathan.top @1.1.1.1 +short
```

Expected: SAN is exactly `*.bestnathan.top`, issuer is Let's Encrypt Staging, challenge TXT is cleaned up. Never deploy this Staging certificate to a consumer.

- [ ] **Step 5: Compare post-issuance DNS names/types with the baseline**

After Staging issuance, rerun the main-only PR-0 workflow and download its new artifact into a different new empty directory named by `AFTER_ARTIFACT_DIR`, as specified in the PR-0 amendment. Do not re-enter the Token locally and do not replace the committed SealedSecret with the rerun's randomized ciphertext. Read the mode-0700 backup path printed in Task 0, validate it, and compare only the sanitized inventories:

```bash
read -r BACKUP_DIR
test -d "$BACKUP_DIR" && test ! -L "$BACKUP_DIR"
test -f "$BACKUP_DIR/cloudflare-records-before.json"
cp "$AFTER_ARTIFACT_DIR/cloudflare-records.json" "$BACKUP_DIR/cloudflare-records-after.json"
python3 -I - "$BACKUP_DIR" <<'PY'
import json,sys
from pathlib import Path
root=Path(sys.argv[1])
for name in ['cloudflare-records-before.json','cloudflare-records-after.json']:
    records=json.loads((root/name).read_text())
    assert records and all(set(item)=={'name','type'} for item in records)
PY
diff -u "$BACKUP_DIR/cloudflare-records-before.json" "$BACKUP_DIR/cloudflare-records-after.json"
```

Expected: no added/changed business A, AAAA, or CNAME records. A still-present ACME TXT or any unrelated concurrent DNS change blocks the gate and must be investigated, not automatically deleted.

### Task 4: PR-2 production Certificate and controller monitoring

**Files:**
- Delete: `components/cert-manager/certificates/certificate-bestnathan-staging-test.yaml`
- Create: `components/cert-manager/certificates/certificate-bestnathan-wildcard.yaml`
- Modify: `scripts/cert_manager/test_manifests.py`
- Modify: `components/observability/prometheus/configmap.yaml`
- Modify: `components/observability/prometheus/deployment.yaml`
- Create: `components/observability/prometheus/configmap-rules.yaml`

- [ ] **Step 1: Replace Staging with the sole production Certificate**

First change the certificate block in `scripts/cert_manager/test_manifests.py` to the production expectation below and run `python3 scripts/cert_manager/test_manifests.py -v`; expected FAIL while the Staging Certificate remains:

```python
        certificates = docs("components/cert-manager/certificates/*.yaml")
        self.assertEqual(len(certificates), 1)
        production = certificates[0]
        self.assertEqual(production["metadata"]["name"], "bestnathan-wildcard")
        self.assertEqual(production["metadata"]["namespace"], "slb")
        self.assertEqual(production["spec"]["secretName"], "bestnathan-top-tls")
        self.assertEqual(production["spec"]["issuerRef"]["name"], "letsencrypt-production")
        self.assertEqual(production["spec"]["dnsNames"], ["*.bestnathan.top"])
        self.assertEqual(production["spec"]["privateKey"]["rotationPolicy"], "Always")
```

Then delete the Staging manifest and add:

```yaml
# components/cert-manager/certificates/certificate-bestnathan-wildcard.yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: bestnathan-wildcard
  namespace: slb
  annotations:
    argocd.argoproj.io/sync-wave: "20"
spec:
  secretName: bestnathan-top-tls
  issuerRef:
    name: letsencrypt-production
    kind: ClusterIssuer
    group: cert-manager.io
  dnsNames:
    - "*.bestnathan.top"
  privateKey:
    rotationPolicy: Always
```

Run `python3 scripts/cert_manager/test_manifests.py -v`; expected PASS with exactly one production Certificate and no apex name.

- [ ] **Step 2: Add the controller scrape and rules file reference**

Inside `data.prometheus.yml` in `components/observability/prometheus/configmap.yaml`, add immediately after `evaluation_interval: 15s`:

```yaml
    rule_files:
      - /etc/prometheus/rules/*.yml
```

Append this scrape job at the same indentation as the existing jobs:

```yaml
      - job_name: "cert-manager"
        static_configs:
          - targets: ["cert-manager.cert-manager.svc:9402"]
```

- [ ] **Step 3: Mount the rules ConfigMap**

Add a second container volumeMount in `components/observability/prometheus/deployment.yaml`:

```yaml
            - name: rules
              mountPath: /etc/prometheus/rules
```

Add a second Pod volume:

```yaml
        - name: rules
          configMap:
            name: prometheus-rules
```

- [ ] **Step 4: Add controller/Certificate alerts only**

```yaml
# components/observability/prometheus/configmap-rules.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-rules
  namespace: observability
data:
  cert-manager.yml: |
    groups:
      - name: cert-manager
        rules:
          - alert: CertManagerCertificateNotReady
            expr: certmanager_certificate_ready_status{namespace="slb",name="bestnathan-wildcard",condition="True"} == 0
            for: 5m
            labels: {severity: critical}
            annotations:
              summary: Production wildcard Certificate is not Ready
          - alert: CertManagerCertificateExpiring
            expr: time() > certmanager_certificate_expiration_timestamp_seconds{namespace="slb",name="bestnathan-wildcard"} - 14 * 86400
            for: 15m
            labels: {severity: warning}
            annotations:
              summary: Managed wildcard certificate expires within 14 days
          - alert: CertManagerCertificateMetricsAbsent
            expr: absent(certmanager_certificate_expiration_timestamp_seconds{namespace="slb",name="bestnathan-wildcard"})
            for: 5m
            labels: {severity: critical}
            annotations:
              summary: Production Certificate metrics are absent
          - alert: CertManagerScrapeDown
            expr: up{job="cert-manager"} == 0
            for: 5m
            labels: {severity: critical}
            annotations:
              summary: Prometheus cannot scrape cert-manager
          - alert: CertManagerSyncErrors
            expr: increase(certmanager_controller_sync_error_count[15m]) > 0
            for: 15m
            labels: {severity: warning}
            annotations:
              summary: cert-manager controller synchronization errors continue
```

- [ ] **Step 5: Validate and commit PR-2**

Run:

```bash
python3 -m unittest discover -s scripts/cert_manager -p 'test_*.py' -v
kubectl apply --dry-run=client -R -f components/cert-manager >/dev/null
kubectl apply --dry-run=client -f components/observability/prometheus >/dev/null
git add apps/cert-manager.yaml components/cert-manager components/observability/prometheus scripts/cert_manager
git diff --cached --check
git commit -m 'feat(cert-manager): issue production wildcard and monitor lifecycle' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

Expected: only one Certificate, no Blackbox alert yet, no unrelated modifications.

- [ ] **Step 6: Merge and enforce the production readiness gate**

After approval and merge:

```bash
kubectl -n observability rollout restart deployment/prometheus
kubectl -n observability rollout status deployment/prometheus --timeout=5m
kubectl wait --for=condition=Ready clusterissuer/letsencrypt-production --timeout=10m
kubectl -n slb wait --for=condition=Ready certificate/bestnathan-wildcard --timeout=15m
kubectl -n observability exec deploy/prometheus -- \
  promtool check config /etc/prometheus/prometheus.yml
```

Expected: production Ready and Prometheus config valid. Only after production Ready and with no consumer of the Staging Secret, remove its leftover generated data:

```bash
kubectl -n cert-manager delete secret bestnathan-staging-test-tls
```

Verify the production Secret SAN/fullchain/public key match, but do not print `tls.key`:

```bash
set -o pipefail
CERT_DIR=$(mktemp -d /tmp/nitops-production-cert.XXXXXX)
kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d > "$CERT_DIR/chain.pem"
kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.key}' | base64 -d > "$CERT_DIR/tls.key"
chmod 600 "$CERT_DIR/tls.key"
openssl x509 -in "$CERT_DIR/chain.pem" -noout -issuer -serial -dates -ext subjectAltName
openssl x509 -in "$CERT_DIR/chain.pem" -checkhost mcp-nession.bestnathan.top -noout
test "$(grep -c 'BEGIN CERTIFICATE' "$CERT_DIR/chain.pem")" -ge 2
CERT_PUBLIC=$(openssl x509 -in "$CERT_DIR/chain.pem" -pubkey -noout | openssl pkey -pubin -outform DER | shasum -a 256 | cut -d' ' -f1)
KEY_PUBLIC=$(openssl pkey -in "$CERT_DIR/tls.key" -pubout -outform DER | shasum -a 256 | cut -d' ' -f1)
test "$CERT_PUBLIC" = "$KEY_PUBLIC"
printf 'certificate/public-key match: yes\n'
rm -rf "$CERT_DIR"
```

Expected: production Let's Encrypt issuer, wildcard host match, and complete chain present. SLB/Router still serve their previous certificates.

### Task 5: PR-3A SLB Secret consumption and verified reload

**Files:**
- Create: `components/slb/nginx-proxy/configmap-reloader.yaml`
- Modify: `components/slb/nginx-proxy/daemonset.yaml`
- Modify: `components/slb/nginx-proxy/configmap-sites.yaml:33-34,56-57`
- Modify: `components/slb/kustomization.yaml`
- Create: `tests/slb/test-cert-reloader.sh`

- [ ] **Step 1: Write the failing reloader tests**

```bash
#!/usr/bin/env bash
# tests/slb/test-cert-reloader.sh
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"
TMP=$(mktemp -d /tmp/nitops-reloader-test.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin" "$TMP/certs" "$TMP/run"
export REAL_OPENSSL=$(command -v openssl)
"$REAL_OPENSSL" req -x509 -newkey rsa:2048 -sha256 -nodes -days 1 \
  -subj /CN=argocd.bestnathan.top -keyout "$TMP/certs/tls.key" \
  -out "$TMP/certs/tls.crt" >/dev/null 2>&1
cat > "$TMP/bin/nginx" <<'SH'
#!/bin/sh
printf '%s\n' "$*" >> "$CALL_LOG"
if [ "$1" = "-t" ] && [ "${NGINX_TEST_FAIL:-0}" = "1" ]; then exit 1; fi
SH
cat > "$TMP/bin/openssl" <<'SH'
#!/bin/sh
if [ "$1" = "s_client" ]; then cat "$PEER_CERT"; exit "${PEER_FAIL:-0}"; fi
exec "$REAL_OPENSSL" "$@"
SH
chmod +x "$TMP/bin/nginx" "$TMP/bin/openssl"
python3 -I - <<'PY' "$TMP/reloader.sh"
import sys, yaml
source = yaml.safe_load(open("components/slb/nginx-proxy/configmap-reloader.yaml"))
open(sys.argv[1], "w").write(source["data"]["cert-reloader.sh"])
PY
chmod +x "$TMP/reloader.sh"
export PATH="$TMP/bin:$PATH" CALL_LOG="$TMP/calls" CERT_DIR="$TMP/certs"
export RUN_DIR="$TMP/run" PROBE_HOST=argocd.bestnathan.top
export CERT_RELOADER_SOURCE_ONLY=1 PEER_CERT="$TMP/certs/tls.crt"
: > "$CALL_LOG"
source "$TMP/reloader.sh"
LAST_FINGERPRINT=previous
LAST_RELOAD=0
export NGINX_TEST_FAIL=1
if run_once; then echo 'invalid update unexpectedly succeeded' >&2; exit 1; fi
! grep -q 'reload' "$CALL_LOG"
[ "$LAST_FINGERPRINT" = previous ]
export NGINX_TEST_FAIL=0
: > "$CALL_LOG"
run_once
grep -q -- '-s reload' "$CALL_LOG"
[ "$LAST_FINGERPRINT" != previous ]
printf 'PASS: invalid update retains old config; valid update reloads and verifies serial\n'
```

The test intentionally uses a fake nginx/openssl boundary; live tests later prove real chain and serial behavior.

- [ ] **Step 2: Run the test and verify FAIL**

Run: `bash tests/slb/test-cert-reloader.sh`

Expected: FAIL because `configmap-reloader.yaml` does not exist.

- [ ] **Step 3: Add the production reloader script**

Create `ConfigMap/nginx-reloader-script` in Namespace `slb`, with `data.cert-reloader.sh` containing exactly:

```sh
#!/bin/sh
set -eu
CERT_DIR=${CERT_DIR:-/etc/nginx/certs-managed/bestnathan}
RUN_DIR=${RUN_DIR:-/var/run/cert-reloader}
PROBE_HOST=${PROBE_HOST:-argocd.bestnathan.top}
OPENSSL_BIN=${OPENSSL_BIN:-openssl}
mkdir -p "$RUN_DIR"
LAST_FINGERPRINT=${LAST_FINGERPRINT:-}
LAST_RELOAD=${LAST_RELOAD:-0}

fingerprint() {
  [ -s "$CERT_DIR/tls.crt" ] && [ -s "$CERT_DIR/tls.key" ] || return 1
  sha256sum "$CERT_DIR/tls.crt" "$CERT_DIR/tls.key" | sha256sum | cut -d' ' -f1
}

candidate_serial() {
  "$OPENSSL_BIN" x509 -in "$CERT_DIR/tls.crt" -noout -serial | cut -d= -f2
}

verify_served() {
  expected=$1
  attempt=0
  while [ "$attempt" -lt 6 ]; do
    if timeout 20 "$OPENSSL_BIN" s_client \
      -connect 127.0.0.1:443 -servername "$PROBE_HOST" \
      -verify_return_error -CAfile /etc/ssl/certs/ca-certificates.crt \
      </dev/null >"$RUN_DIR/peer.pem" 2>"$RUN_DIR/peer.err"; then
      actual=$("$OPENSSL_BIN" x509 -in "$RUN_DIR/peer.pem" -noout -serial | cut -d= -f2)
      if [ "$actual" = "$expected" ]; then
        printf 'verified served serial=%s\n' "$actual"
        return 0
      fi
    fi
    attempt=$((attempt + 1))
    sleep 5
  done
  return 1
}

run_once() {
  current=$(fingerprint) || return 1
  [ "$current" != "$LAST_FINGERPRINT" ] || return 0
  sleep 10
  confirmed=$(fingerprint) || return 1
  [ "$confirmed" = "$current" ] || return 1
  now=$(date +%s)
  elapsed=$((now - LAST_RELOAD))
  if [ "$elapsed" -lt 30 ]; then sleep $((30 - elapsed)); fi
  if ! timeout 20 nginx -t; then
    printf 'nginx config test failed; retaining active configuration\n' >&2
    return 1
  fi
  if ! timeout 20 nginx -s reload; then
    printf 'nginx reload failed; not advancing certificate generation\n' >&2
    return 1
  fi
  expected=$(candidate_serial)
  if ! verify_served "$expected"; then
    printf 'served certificate did not converge to expected serial\n' >&2
    return 1
  fi
  LAST_FINGERPRINT=$current
  LAST_RELOAD=$(date +%s)
  return 0
}

main() {
  LAST_FINGERPRINT=$(fingerprint)
  delay=10
  while true; do
    if run_once; then delay=10; else
      delay=$((delay * 2)); [ "$delay" -le 300 ] || delay=300
    fi
    sleep "$delay"
  done
}

if [ "${CERT_RELOADER_SOURCE_ONLY:-0}" != "1" ]; then main; fi
```

Set ConfigMap metadata annotation `argocd.argoproj.io/sync-wave: "0"`. Add `configmap-reloader.yaml` to `components/slb/kustomization.yaml` resources.

- [ ] **Step 4: Patch the DaemonSet completely**

Use image `docker.io/library/nginx@sha256:2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51` for both containers. Preserve all existing tolerations, hostNetwork, probes, ConfigMaps and NFS. Add Pod-level:

```yaml
      shareProcessNamespace: true
```

Add to the main nginx container:

```yaml
          volumeMounts:
            - name: nginx-run
              mountPath: /var/run
            - name: bestnathan-tls
              mountPath: /etc/nginx/certs-managed/bestnathan
              readOnly: true
```

Merge these mounts with the existing nginx-conf, nginx-sites and nfs-root mounts rather than replacing them. Add the reload container:

```yaml
        - name: cert-reloader
          image: docker.io/library/nginx@sha256:2f4e10193b0c4f61472245e9d26cea78e7df7af266c7e7cf5e67353ee2731b51
          command: ["/bin/sh", "/usr/local/bin/cert-reloader.sh"]
          env:
            - name: PROBE_HOST
              value: argocd.bestnathan.top
          volumeMounts:
            - name: nginx-conf
              mountPath: /etc/nginx/nginx.conf
              subPath: nginx.conf
            - name: nginx-sites
              mountPath: /etc/nginx/conf.d
            - name: nfs-root
              mountPath: /etc/nginx/certs
              subPath: slb/nginx-conf/certs
              readOnly: true
            - name: bestnathan-tls
              mountPath: /etc/nginx/certs-managed/bestnathan
              readOnly: true
            - name: nginx-run
              mountPath: /var/run
            - name: reloader-script
              mountPath: /usr/local/bin/cert-reloader.sh
              subPath: cert-reloader.sh
```

Add Pod volumes:

```yaml
        - name: bestnathan-tls
          secret:
            secretName: bestnathan-top-tls
            defaultMode: 0400
        - name: nginx-run
          emptyDir: {}
        - name: reloader-script
          configMap:
            name: nginx-reloader-script
            defaultMode: 0555
```

Change only the public server block paths in `configmap-sites.yaml`:

```nginx
      ssl_certificate     /etc/nginx/certs-managed/bestnathan/tls.crt;
      ssl_certificate_key /etc/nginx/certs-managed/bestnathan/tls.key;
```

Leave `/etc/nginx/certs/nhome.*` and `/etc/nginx/certs/default.*` untouched.

- [ ] **Step 5: Run reloader tests and static validation**

Run:

```bash
bash tests/slb/test-cert-reloader.sh
shellcheck <(python3 -I -c 'import yaml; print(yaml.safe_load(open("components/slb/nginx-proxy/configmap-reloader.yaml"))["data"]["cert-reloader.sh"])')
kubectl kustomize components/slb > /tmp/nitops-slb-rendered.yaml
kubectl apply --dry-run=client -f /tmp/nitops-slb-rendered.yaml >/dev/null
```

Expected: invalid update test PASS, ShellCheck no findings, Kustomize and dry-run succeed. If the test exposes a script defect, fix the script and rerun before proceeding.

- [ ] **Step 6: Commit PR-3A**

```bash
git add components/slb tests/slb/test-cert-reloader.sh
git diff --cached --check
git commit -m 'feat(slb): consume cert-manager secret with verified reload' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

- [ ] **Step 7: Merge and verify every SLB node**

After approval:

```bash
kubectl -n slb rollout status daemonset/nginx-proxy --timeout=15m
kubectl -n slb get pods -o wide
EXPECTED_SERIAL=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial | cut -d= -f2)
for IP in 192.168.2.162 192.168.2.86 192.168.2.232 192.168.2.224; do
  ACTUAL=$(openssl s_client -connect "$IP:443" -servername argocd.bestnathan.top \
    -verify_return_error -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null 2>/dev/null | \
    openssl x509 -noout -serial | cut -d= -f2)
  printf '%s expected=%s actual=%s\n' "$IP" "$EXPECTED_SERIAL" "$ACTUAL"
  test "$EXPECTED_SERIAL" = "$ACTUAL"
done
```

Expected: every node Ready with two containers and matching actual serial. Recheck the Task 0 `nhome`/`default` public hashes and representative routes; any change blocks PR-3B.

### Task 6: PR-3B LAN Blackbox and actual-served-certificate alerts

**Files:**
- Create: `components/observability/prometheus/configmap-blackbox.yaml`
- Create: `components/observability/prometheus/deployment-blackbox.yaml`
- Create: `components/observability/prometheus/service-blackbox.yaml`
- Modify: `components/observability/prometheus/configmap.yaml`
- Modify: `components/observability/prometheus/configmap-rules.yaml`

- [ ] **Step 1: Add Blackbox modules**

```yaml
# components/observability/prometheus/configmap-blackbox.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: blackbox-config
  namespace: observability
data:
  blackbox.yml: |
    modules:
      lan_slb_https:
        prober: http
        timeout: 10s
        http:
          method: GET
          preferred_ip_protocol: ip4
          valid_status_codes: [200, 301, 302, 400, 401, 403, 404, 405]
          headers:
            Host: argocd.bestnathan.top
          tls_config:
            server_name: argocd.bestnathan.top
            insecure_skip_verify: false
      public_mcp_https:
        prober: http
        timeout: 15s
        http:
          method: GET
          preferred_ip_protocol: ip4
          proxy_url: "http://192.168.2.98:7895"
          skip_resolve_phase_with_proxy: true
          valid_status_codes: [200, 400, 401, 403, 405]
          tls_config:
            insecure_skip_verify: false
```

The Public module sends CONNECT through the reviewed egress proxy and delegates resolution to it, preventing internal split DNS from turning the Public probe into another LAN probe. TLS verification still terminates at Blackbox with normal public trust.

- [ ] **Step 2: Add a pinned multi-arch Deployment and Service**

```yaml
# components/observability/prometheus/deployment-blackbox.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: blackbox-exporter
  namespace: observability
spec:
  replicas: 1
  selector:
    matchLabels: {app: blackbox-exporter}
  template:
    metadata:
      labels: {app: blackbox-exporter}
    spec:
      containers:
        - name: blackbox-exporter
          image: prom/blackbox-exporter@sha256:a50c4c0eda297baa1678cd4dc4712a67fdea713b832d43ce7fcc5f9bea05094d
          args:
            - --config.file=/etc/blackbox/blackbox.yml
          ports:
            - {name: http, containerPort: 9115}
          volumeMounts:
            - name: config
              mountPath: /etc/blackbox
              readOnly: true
      volumes:
        - name: config
          configMap: {name: blackbox-config}
```

```yaml
# components/observability/prometheus/service-blackbox.yaml
apiVersion: v1
kind: Service
metadata:
  name: blackbox-exporter
  namespace: observability
spec:
  selector: {app: blackbox-exporter}
  ports:
    - name: http
      port: 9115
      targetPort: http
```

- [ ] **Step 3: Add only LAN scrape targets in PR-3B**

Append to `scrape_configs`:

```yaml
      - job_name: "blackbox-lan-slb"
        metrics_path: /probe
        params:
          module: [lan_slb_https]
        static_configs:
          - targets:
              - https://192.168.2.162/
              - https://192.168.2.86/
              - https://192.168.2.232/
              - https://192.168.2.224/
        relabel_configs:
          - source_labels: [__address__]
            target_label: __param_target
          - source_labels: [__param_target]
            target_label: instance
          - target_label: __address__
            replacement: blackbox-exporter.observability.svc:9115
```

- [ ] **Step 4: Add LAN rules**

Add to `groups` in `configmap-rules.yaml`:

```yaml
      - name: edge-tls-lan
        rules:
          - alert: LanSlbTlsProbeFailed
            expr: probe_success{job="blackbox-lan-slb"} == 0
            for: 2m
            labels: {severity: critical}
            annotations:
              summary: LAN SLB HTTPS probe fails with normal trust verification
          - alert: LanSlbCertificateExpiring
            expr: time() > probe_ssl_earliest_cert_expiry{job="blackbox-lan-slb"} - 14 * 86400
            for: 15m
            labels: {severity: warning}
            annotations:
              summary: Actually served LAN certificate expires within 14 days
```

- [ ] **Step 5: Validate, commit, merge, and read back**

Run:

```bash
kubectl apply --dry-run=client -f components/observability/prometheus >/dev/null
git add components/observability/prometheus
git commit -m 'feat(monitoring): probe actual SLB TLS certificates' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

After approval/merge:

```bash
kubectl -n observability rollout status deployment/blackbox-exporter --timeout=5m
kubectl -n observability rollout restart deployment/prometheus
kubectl -n observability rollout status deployment/prometheus --timeout=5m
kubectl -n observability exec deploy/prometheus -- \
  promtool check config /etc/prometheus/prometheus.yml
kubectl -n observability exec deploy/prometheus -- \
  wget -qO- 'http://blackbox-exporter:9115/probe?module=lan_slb_https&target=https://192.168.2.86/' | rg 'probe_success|probe_ssl_earliest_cert_expiry'
```

Expected: `probe_success 1`, valid expiry, and no active LAN alerts. Public MCP scrape remains absent until Task 9.

### Task 7: PR-4 Router restricted syncer and tests

**Files:**
- Create: `components/cert-manager/router-rbac/serviceaccount.yaml`
- Create: `components/cert-manager/router-rbac/role.yaml`
- Create: `components/cert-manager/router-rbac/rolebinding.yaml`
- Create: `router/cert-sync/cert_sync.py`
- Create: `router/cert-sync/test_cert_sync.py`
- Create: `router/cert-sync/nitops-cert-sync.service`
- Create: `router/cert-sync/nitops-cert-sync.timer`

- [ ] **Step 1: Add name-restricted RBAC**

```yaml
# serviceaccount.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: router-cert-reader
  namespace: slb
  annotations: {argocd.argoproj.io/sync-wave: "0"}
```

```yaml
# role.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: router-cert-reader
  namespace: slb
  annotations: {argocd.argoproj.io/sync-wave: "0"}
rules:
  - apiGroups: [""]
    resources: ["secrets"]
    resourceNames: ["bestnathan-top-tls"]
    verbs: ["get"]
```

```yaml
# rolebinding.yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: router-cert-reader
  namespace: slb
  annotations: {argocd.argoproj.io/sync-wave: "0"}
subjects:
  - kind: ServiceAccount
    name: router-cert-reader
    namespace: slb
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: router-cert-reader
```

Verify `resourceNames` is present and no `list`, `watch`, or write verb appears.

- [ ] **Step 2: Write failing rollback tests**

```python
# router/cert-sync/test_cert_sync.py
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cert_sync


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.versions = self.base / "versions"
        self.old = self.versions / "OLD"
        self.new = self.versions / "NEW"
        self.old.mkdir(parents=True)
        self.new.mkdir()
        self.current = self.base / "current"
        self.current.symlink_to(Path("versions/OLD"))
        cert_sync.BASE = self.base
        cert_sync.VERSIONS = self.versions
        cert_sync.CURRENT = self.current

    def tearDown(self):
        self.tmp.cleanup()

    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test", side_effect=RuntimeError("invalid candidate"))
    def test_invalid_candidate_restores_symlink_without_reload(self, test, reload):
        with self.assertRaises(RuntimeError):
            cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.old.resolve())
        reload.assert_not_called()

    @patch("cert_sync.verify_served", side_effect=RuntimeError("wrong serial"))
    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test")
    def test_served_mismatch_reloads_previous_version(self, test, reload, verify):
        with self.assertRaises(RuntimeError):
            cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.old.resolve())
        self.assertEqual(reload.call_count, 2)

    @patch("cert_sync.verify_served")
    @patch("cert_sync.nginx_reload")
    @patch("cert_sync.nginx_test")
    def test_success_keeps_new_version(self, test, reload, verify):
        cert_sync.apply_candidate(self.new, "NEW")
        self.assertEqual(self.current.resolve(), self.new.resolve())
        reload.assert_called_once_with()


class CertificateValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        def openssl(*args):
            subprocess.run(["openssl", *map(str, args)], check=True,
                           capture_output=True, text=True)

        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                "-subj", "/CN=Nitops Test Root", "-addext", "basicConstraints=critical,CA:TRUE",
                "-keyout", self.root / "root.key", "-out", self.root / "root.crt")
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=*.bestnathan.top",
                "-keyout", self.root / "leaf.key", "-out", self.root / "leaf.csr")
        (self.root / "leaf.ext").write_text(
            "subjectAltName=DNS:*.bestnathan.top\nbasicConstraints=critical,CA:FALSE\n")
        openssl("x509", "-req", "-in", self.root / "leaf.csr", "-CA", self.root / "root.crt",
                "-CAkey", self.root / "root.key", "-CAcreateserial", "-days", "2",
                "-extfile", self.root / "leaf.ext", "-out", self.root / "leaf.crt")
        self.chain = ((self.root / "leaf.crt").read_bytes() +
                      (self.root / "root.crt").read_bytes())
        self.key = (self.root / "leaf.key").read_bytes()
        cert_sync.CA_FILE = str(self.root / "root.crt")

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_fullchain_and_matching_key(self):
        serial = cert_sync.validate_certificate(self.chain, self.key, self.root)
        self.assertRegex(serial, r"^[0-9A-F]+$")

    def test_mismatched_key_is_rejected(self):
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        "rsa_keygen_bits:2048", "-out", str(self.root / "other.key")],
                       check=True, capture_output=True)
        other_key = (self.root / "other.key").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "do not match"):
            cert_sync.validate_certificate(self.chain, other_key, self.root)


if __name__ == "__main__":
    unittest.main()
```

Run `python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v`; expected FAIL because `cert_sync.py` is absent.

- [ ] **Step 3: Implement the Router syncer**

```python
#!/usr/bin/env python3
# router/cert-sync/cert_sync.py
import argparse
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

BASE = Path("/root/docker-nginx/certs/le")
VERSIONS = BASE / "versions"
CURRENT = BASE / "current"
SECRET_URL = "/api/v1/namespaces/slb/secrets/bestnathan-top-tls"
PROBE_HOST = "mcp-nession.bestnathan.top"
CA_FILE = os.environ.get("CERT_SYNC_CA_FILE", "/etc/ssl/certs/ca-certificates.crt")


def run(args, input_text=None):
    return subprocess.run(args, input=input_text, text=True, capture_output=True,
                          check=True, timeout=20).stdout


def fetch_secret():
    endpoint = os.environ["KUBERNETES_API_URL"].rstrip("/") + SECRET_URL
    token = Path(os.environ["KUBERNETES_TOKEN_FILE"]).read_text().strip()
    context = ssl.create_default_context(cafile=os.environ["KUBERNETES_CA_FILE"])
    request = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(request, context=context, timeout=20) as response:
        document = json.load(response)
    if document["metadata"]["name"] != "bestnathan-top-tls":
        raise RuntimeError("unexpected Secret identity")
    return (base64.b64decode(document["data"]["tls.crt"]),
            base64.b64decode(document["data"]["tls.key"]))


def validate_certificate(cert, key, directory):
    chain_path = directory / "chain.pem"
    key_path = directory / "tls.key"
    chain_path.write_bytes(cert)
    key_path.write_bytes(key)
    key_path.chmod(0o600)
    blocks = re.findall(
        b"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----\\n?",
        cert, flags=re.DOTALL)
    if len(blocks) < 2:
        raise RuntimeError("tls.crt does not contain a full chain")
    leaf = directory / "leaf.pem"
    intermediates = directory / "intermediates.pem"
    leaf.write_bytes(blocks[0])
    intermediates.write_bytes(b"".join(blocks[1:]))
    run(["openssl", "verify", "-purpose", "sslserver", "-CAfile", CA_FILE,
         "-untrusted", str(intermediates), str(leaf)])
    run(["openssl", "x509", "-in", str(leaf), "-noout", "-checkend", "86400"])
    run(["openssl", "x509", "-in", str(leaf), "-noout", "-checkhost", PROBE_HOST])
    cert_pub = run(["openssl", "x509", "-in", str(leaf), "-pubkey", "-noout"])
    key_pub = run(["openssl", "pkey", "-in", str(key_path), "-pubout"])
    if cert_pub != key_pub:
        raise RuntimeError("certificate and private key do not match")
    output = run(["openssl", "x509", "-in", str(leaf), "-noout", "-serial"])
    serial = output.strip().split("=", 1)[1].upper()
    if not re.fullmatch(r"[0-9A-F]+", serial):
        raise RuntimeError("invalid serial")
    return serial


def candidate():
    VERSIONS.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".candidate-", dir=VERSIONS))
    try:
        cert, key = fetch_secret()
        serial = validate_certificate(cert, key, temp)
        destination = VERSIONS / serial
        if destination.exists():
            shutil.rmtree(temp)
        else:
            temp.rename(destination)
        return destination, serial
    except Exception:
        if temp.exists():
            shutil.rmtree(temp)
        raise


def switch_symlink(target):
    temporary = BASE / ".current.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(Path("versions") / target.name)
    os.replace(temporary, CURRENT)


def nginx_test():
    run(["docker", "exec", "nginx-proxy", "nginx", "-t"])


def nginx_reload():
    run(["docker", "exec", "nginx-proxy", "nginx", "-s", "reload"])


def verify_served(serial):
    for attempt in range(6):
        process = subprocess.run([
            "openssl", "s_client", "-connect", "127.0.0.1:443",
            "-servername", PROBE_HOST, "-verify_hostname", PROBE_HOST,
            "-verify_return_error", "-CAfile", "/etc/ssl/certs/ca-certificates.crt",
        ], input="", text=True, capture_output=True, timeout=20)
        if process.returncode == 0:
            output = run(["openssl", "x509", "-noout", "-serial"], process.stdout)
            if output.strip().split("=", 1)[1].upper() == serial:
                return
        time.sleep(5)
    raise RuntimeError("Router did not serve the candidate serial")


def apply_candidate(destination, serial):
    previous = CURRENT.resolve(strict=True)
    if previous == destination:
        verify_served(serial)
        return
    switch_symlink(destination)
    reloaded = False
    try:
        nginx_test()
        nginx_reload()
        reloaded = True
        verify_served(serial)
    except Exception:
        switch_symlink(previous)
        if reloaded:
            nginx_test()
            nginx_reload()
        raise
    for path in VERSIONS.iterdir():
        if path.is_dir() and not path.name.startswith(".") and path not in {previous, destination}:
            shutil.rmtree(path)
    print(f"verified router serial={serial}")


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    destination, serial = candidate()
    if args.check:
        print(f"validated candidate serial={serial}; nginx unchanged")
        return
    apply_candidate(destination, serial)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and syntax checks**

```bash
python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v
python3 -m py_compile router/cert-sync/cert_sync.py
```

Expected: five tests PASS, covering rollback, served-serial mismatch, success, a real temporary CA/fullchain, and mismatched private-key rejection. All certificates and keys are generated inside the test temporary directory; production Secrets are never used.

- [ ] **Step 5: Add observe-only systemd units**

```ini
# router/cert-sync/nitops-cert-sync.service
[Unit]
Description=Validate and synchronize cert-manager wildcard to Router
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/root/nitops-cert-sync/env
ExecStart=/usr/local/sbin/nitops-cert-sync --check
```

```ini
# router/cert-sync/nitops-cert-sync.timer
[Unit]
Description=Poll cert-manager wildcard every five minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 6: Commit PR-4 source**

```bash
python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v
kubectl apply --dry-run=client -R -f components/cert-manager/router-rbac >/dev/null
git add components/cert-manager/router-rbac router/cert-sync
git diff --cached --check
git commit -m 'feat(router): add restricted certificate sync with rollback' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

### Task 8: PR-4 Router observe-only installation

**Files:**
- Modify on Router only: `/root/nitops-cert-sync/`, `/usr/local/sbin/nitops-cert-sync`, systemd unit files
- Do not modify `/root/docker-nginx/conf/` or reload Nginx

- [ ] **Step 1: Merge and verify RBAC is no broader than intended**

```bash
kubectl -n slb get role router-cert-reader -o yaml
kubectl auth can-i get secret bestnathan-top-tls -n slb \
  --as=system:serviceaccount:slb:router-cert-reader
kubectl auth can-i list secrets -n slb \
  --as=system:serviceaccount:slb:router-cert-reader
kubectl auth can-i get secret argocd-tls -n slb \
  --as=system:serviceaccount:slb:router-cert-reader
```

Expected: `yes`, `no`, `no` respectively.

- [ ] **Step 2: Bootstrap a long-lived token Secret outside ArgoCD tracking**

With approval:

```bash
kubectl -n slb apply -f - <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: router-cert-reader-token
  annotations:
    kubernetes.io/service-account.name: router-cert-reader
type: kubernetes.io/service-account-token
YAML
kubectl -n slb get secret router-cert-reader-token -o jsonpath='{.data.token}' >/dev/null
```

This Secret is intentionally untracked by ArgoCD. Never print its decoded data to terminal or issue comments.

- [ ] **Step 3: Install Router files and CA-verifying API credentials**

Use the existing non-loopback API Server endpoint from the admin kubeconfig and the CA delivered in the ServiceAccount token Secret. Do not set `insecure-skip-tls-verify`. Run:

```bash
ssh root@192.168.2.106 'install -d -m 700 /root/nitops-cert-sync; install -d -m 700 /root/docker-nginx/certs/le/versions'
scp router/cert-sync/cert_sync.py root@192.168.2.106:/root/nitops-cert-sync/cert_sync.py
scp router/cert-sync/nitops-cert-sync.service router/cert-sync/nitops-cert-sync.timer root@192.168.2.106:/root/nitops-cert-sync/
ssh root@192.168.2.106 'install -m 700 /root/nitops-cert-sync/cert_sync.py /usr/local/sbin/nitops-cert-sync; install -m 644 /root/nitops-cert-sync/nitops-cert-sync.service /etc/systemd/system/; install -m 644 /root/nitops-cert-sync/nitops-cert-sync.timer /etc/systemd/system/'

BOOT_DIR=$(mktemp -d /tmp/nitops-router-bootstrap.XXXXXX)
chmod 700 "$BOOT_DIR"
API_SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
case "$API_SERVER" in
  https://127.*|https://localhost*|http://*|"") printf 'non-routable API endpoint: %s\n' "$API_SERVER" >&2; exit 1 ;;
  https://*) ;;
  *) printf 'unsupported API endpoint: %s\n' "$API_SERVER" >&2; exit 1 ;;
esac
kubectl -n slb get secret router-cert-reader-token -o jsonpath='{.data.token}' | base64 -d > "$BOOT_DIR/token"
kubectl -n slb get secret router-cert-reader-token -o jsonpath='{.data.ca\.crt}' | base64 -d > "$BOOT_DIR/ca.crt"
printf 'KUBERNETES_API_URL=%s\nKUBERNETES_CA_FILE=/root/nitops-cert-sync/ca.crt\nKUBERNETES_TOKEN_FILE=/root/nitops-cert-sync/token\n' "$API_SERVER" > "$BOOT_DIR/env"
chmod 600 "$BOOT_DIR/token" "$BOOT_DIR/ca.crt" "$BOOT_DIR/env"
scp "$BOOT_DIR/token" "$BOOT_DIR/ca.crt" "$BOOT_DIR/env" root@192.168.2.106:/root/nitops-cert-sync/
ssh root@192.168.2.106 'chown root:root /root/nitops-cert-sync/token /root/nitops-cert-sync/ca.crt /root/nitops-cert-sync/env; chmod 600 /root/nitops-cert-sync/token /root/nitops-cert-sync/ca.crt /root/nitops-cert-sync/env'
rm -rf "$BOOT_DIR"
```

Expected: Router has only `env`, `ca.crt`, `token`, the syncer, and unit files under `/root/nitops-cert-sync`; all credential files are root-only and their contents never appear in terminal output.

- [ ] **Step 4: Run observe-only and record before/after Nginx hashes**

```bash
ssh root@192.168.2.106 'systemctl daemon-reload; systemctl enable --now nitops-cert-sync.timer; systemctl start nitops-cert-sync.service; systemctl status nitops-cert-sync.service --no-pager; journalctl -u nitops-cert-sync.service -n 20 --no-pager'
ssh root@192.168.2.106 'sha256sum /root/docker-nginx/certs/nession.crt /root/docker-nginx/certs/argocd.crt; docker exec nginx-proxy nginx -t; test ! -e /root/docker-nginx/certs/le/current'
```

Expected: candidate validation succeeds, journal contains only sanitized serial, no `current` symlink exists, Nginx hashes/config are unchanged, and no reload occurred.

### Task 9: PR-5 Nession MCP-only Router policy and active sync

**Files:**
- Create: `router/nginx/cloudflare-ips.inc`
- Create: `router/nginx/nession.conf`
- Create: `router/nginx/nession-deny-only.conf`
- Modify: `router/cert-sync/nitops-cert-sync.service` from `--check` to `--apply`
- Modify: `components/observability/prometheus/configmap.yaml` for each actually active MCP only
- Modify: `components/observability/prometheus/configmap-rules.yaml` with Public MCP alert

- [ ] **Step 1: Write the exact public MCP configuration**

```nginx
# router/nginx/cloudflare-ips.inc
allow 173.245.48.0/20;
allow 103.21.244.0/22;
allow 103.22.200.0/22;
allow 103.31.4.0/22;
allow 141.101.64.0/18;
allow 108.162.192.0/18;
allow 190.93.240.0/20;
allow 188.114.96.0/20;
allow 197.234.240.0/22;
allow 198.41.128.0/17;
allow 162.158.0.0/15;
allow 104.16.0.0/13;
allow 104.24.0.0/14;
allow 172.64.0.0/13;
allow 131.0.72.0/22;
allow 2400:cb00::/32;
allow 2606:4700::/32;
allow 2803:f800::/32;
allow 2405:b500::/32;
allow 2405:8100::/32;
allow 2a06:98c0::/29;
allow 2c0f:f248::/32;
deny all;
```

The `.inc` suffix prevents the main `conf.d/*.conf` include from applying these restrictions globally.

```nginx
# router/nginx/nession.conf
map $http_upgrade $nession_connection_upgrade {
    default upgrade;
    ''      '';
}
map $http_cf_connecting_ip $nession_client_ip {
    default $http_cf_connecting_ip;
    ''      $remote_addr;
}

server {
    listen 80;
    server_name mcp-nession.bestnathan.top staging-mcp-nession.bestnathan.top;
    include /etc/nginx/conf.d/cloudflare-ips.inc;
    return 308 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name mcp-nession.bestnathan.top staging-mcp-nession.bestnathan.top;
    ssl_certificate     /etc/nginx/certs/le/current/chain.pem;
    ssl_certificate_key /etc/nginx/certs/le/current/tls.key;

    include /etc/nginx/conf.d/cloudflare-ips.inc;

    location = /mcp {
        if ($ssl_server_name != $host) { return 444; }
        proxy_pass https://192.168.2.86:443;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $nession_client_ip;
        proxy_set_header X-Forwarded-For $nession_client_ip;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $nession_connection_upgrade;
        proxy_ssl_server_name on;
        proxy_ssl_name $host;
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 3;
    }
    location / { return 404; }
}

server {
    listen 80;
    listen 443 ssl;
    server_name nession.bestnathan.top *.nession.bestnathan.top ~^([a-z0-9-]+-)?nession\.bestnathan\.top$;
    ssl_certificate     /etc/nginx/certs/le/current/chain.pem;
    ssl_certificate_key /etc/nginx/certs/le/current/tls.key;
    return 444;
}
```

Before activation, compare the allowlist with `https://www.cloudflare.com/ips-v4` and `ips-v6`; update the reviewed file if Cloudflare has changed it. Do not use a wildcard partial-label certificate or public non-MCP server block.

- [ ] **Step 2: Add fail-closed activation config**

Create the complete `router/nginx/nession-deny-only.conf`; it has no maps and no proxy location:

```nginx
# router/nginx/nession-deny-only.conf
server {
    listen 80;
    listen 443 ssl;
    server_name mcp-nession.bestnathan.top staging-mcp-nession.bestnathan.top;
    ssl_certificate     /etc/nginx/certs/le/current/chain.pem;
    ssl_certificate_key /etc/nginx/certs/le/current/tls.key;
    return 444;
}

server {
    listen 80;
    listen 443 ssl;
    server_name nession.bestnathan.top *.nession.bestnathan.top ~^([a-z0-9-]+-)?nession\.bestnathan\.top$;
    ssl_certificate     /etc/nginx/certs/le/current/chain.pem;
    ssl_certificate_key /etc/nginx/certs/le/current/tls.key;
    return 444;
}
```

If activation fails, install this deny-only file rather than restoring a self-signed public origin.

- [ ] **Step 3: Activate with explicit rollback**

With downstream Nession readiness and operator approval:

```bash
EXPECTED_SERIAL=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial | cut -d= -f2 | tr '[:lower:]' '[:upper:]')
ssh root@192.168.2.106 "test -d /root/docker-nginx/certs/le/versions/$EXPECTED_SERIAL; ln -sfn versions/$EXPECTED_SERIAL /root/docker-nginx/certs/le/current.new && mv -Tf /root/docker-nginx/certs/le/current.new /root/docker-nginx/certs/le/current"
scp router/nginx/cloudflare-ips.inc root@192.168.2.106:/root/docker-nginx/conf/cloudflare-ips.inc
scp router/nginx/nession.conf root@192.168.2.106:/root/docker-nginx/conf/nession.conf.new
scp router/nginx/nession-deny-only.conf root@192.168.2.106:/root/nitops-cert-sync/nession-deny-only.conf
ssh root@192.168.2.106 'cp /root/docker-nginx/conf/nession.conf /root/nitops-cert-sync/nession.conf.pre-cert-manager; mv /root/docker-nginx/conf/nession.conf.new /root/docker-nginx/conf/nession.conf; docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload'
```

If the final command or subsequent probes fail, copy the deny-only file to `/root/docker-nginx/conf/nession.conf`, run `nginx -t && nginx -s reload`, and keep public MCP closed. Do not restore the old self-signed public origin.

Then change `ExecStart` to `/usr/local/sbin/nitops-cert-sync --apply`, reinstall the unit, run `systemctl daemon-reload`, and start the service once manually. Expected journal: verified Router serial equal to the SLB Secret serial.

- [ ] **Step 4: Add Public probe only for active MCP backends**

Before treating a Public probe as Full(strict) evidence, obtain the downstream issue's sanitized Cloudflare per-host SSL-mode readback showing `Full (strict)`. A successful probe without that readback proves reachability only, not the effective Cloudflare mode.

Append this complete scrape job when staging MCP is confirmed active. If production MCP is also active, add its one target exactly where shown; otherwise omit that line and leave production acceptance Pending.

```yaml
      - job_name: "blackbox-public-mcp"
        metrics_path: /probe
        params:
          module: [public_mcp_https]
        static_configs:
          - targets:
              - https://staging-mcp-nession.bestnathan.top/mcp
              - https://mcp-nession.bestnathan.top/mcp
        relabel_configs:
          - source_labels: [__address__]
            target_label: __param_target
          - source_labels: [__param_target]
            target_label: instance
          - target_label: __address__
            replacement: blackbox-exporter.observability.svc:9115
```

Do not add an inactive target. Add the Public alert group:

```yaml
      - name: edge-tls-public
        rules:
          - alert: PublicMcpProbeFailed
            expr: probe_success{job="blackbox-public-mcp"} == 0
            for: 2m
            labels: {severity: critical}
            annotations:
              summary: Public MCP route or Cloudflare Full(strict) origin is failing
```

- [ ] **Step 5: Run real Host/SNI/path/source negative probes**

Use `curl --resolve` or `openssl s_client` with normal trust stores, never `-k`. Prove:

```text
mcp SNI + mcp Host + /mcp + valid token -> MCP success
mcp SNI + mcp Host + /mcp + invalid token -> authentication rejection
mcp SNI + nession Host -> 444
nession SNI + mcp Host -> 444
agent-nession/ui-nession/staging private Hosts -> 444
old nested *.nession.bestnathan.top -> 444
/mcp/, /api, / -> 404 or 444, never proxied as MCP
non-Cloudflare direct Router source -> denied
spoofed X-Forwarded-For -> not trusted as client identity
ArgoCD, EMQX, Grafana, MinIO, nhome.local -> baseline unchanged
```

Record sanitized statuses and TLS serials. No public negative result may be inferred solely from missing DNS.

- [ ] **Step 6: Commit and publish PR-5 source**

```bash
python3 -m py_compile router/cert-sync/cert_sync.py
python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v
kubectl apply --dry-run=client -f components/observability/prometheus >/dev/null
ssh root@192.168.2.106 'docker exec nginx-proxy nginx -t'
git add router components/observability/prometheus
git diff --cached --check
git commit -m 'feat(edge): enforce MCP-only public TLS and automate router renewal' -m 'Co-Authored-By: Claude Code <noreply@anthropic.com>'
```

Expected: Python compilation/tests, manifest dry-run, and live Nginx syntax all pass. Live negative probes from Step 5 remain mandatory; these static checks do not replace them.

### Task 10: PR-6 controlled production renewal

**Files:**
- Create: `tests/prometheus/cert-manager-rules-test.yml`
- No manifest changes
- Create outside Git: a new empty cmctl download directory and sanitized evidence file

- [ ] **Step 1: Install and checksum cmctl outside the repository**

```bash
CMCTL_DIR=$(mktemp -d /tmp/nitops-cmctl-v2.6.2.XXXXXX)
curl -fsSL https://github.com/cert-manager/cmctl/releases/download/v2.6.2/cmctl_darwin_arm64 -o "$CMCTL_DIR/cmctl"
printf '%s  %s\n' 'c05aeddc8ba789942529ca99c284f8de655d8bc7de3680d87bea7452e86f30f2' "$CMCTL_DIR/cmctl" | shasum -a 256 -c -
chmod 700 "$CMCTL_DIR/cmctl"
printf '%s\n' "$CMCTL_DIR"
"$CMCTL_DIR/cmctl" version --client
```

Expected: checksum OK and client v2.6.2. On another OS/architecture, use the matching asset from the same v2.6.2 release and verify its published SHA-256 rather than reusing this digest.

- [ ] **Step 2: Record pre-renewal serials and trigger one renewal**

With explicit operator approval, run `read -r CMCTL_DIR` and enter the directory printed in Step 1. Then:

```bash
test -x "$CMCTL_DIR/cmctl"
OLD_SERIAL=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial)
OLD_PUBLIC=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER | shasum -a 256 | cut -d' ' -f1)
printf 'old %s public-key-sha256=%s\n' "$OLD_SERIAL" "$OLD_PUBLIC"
"$CMCTL_DIR/cmctl" renew bestnathan-wildcard --namespace slb
kubectl -n slb wait --for=condition=Ready certificate/bestnathan-wildcard --timeout=15m
NEW_SERIAL=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -noout -serial)
NEW_PUBLIC=$(kubectl -n slb get secret bestnathan-top-tls -o jsonpath='{.data.tls\.crt}' | base64 -d | openssl x509 -pubkey -noout | openssl pkey -pubin -outform DER | shasum -a 256 | cut -d' ' -f1)
printf 'new %s public-key-sha256=%s\n' "$NEW_SERIAL" "$NEW_PUBLIC"
test "$OLD_SERIAL" != "$NEW_SERIAL"
test "$OLD_PUBLIC" != "$NEW_PUBLIC"
```

Expected: a new production serial and a new private key generation. Do not print or compare raw private keys in evidence; compare public-key fingerprints only.

- [ ] **Step 3: Prove bounded convergence and route health**

Within 10 minutes, repeat Task 5 Step 7 for all four SLB nodes and run:

```bash
ssh root@192.168.2.106 'systemctl start nitops-cert-sync.service; journalctl -u nitops-cert-sync.service -n 30 --no-pager; openssl s_client -connect 127.0.0.1:443 -servername mcp-nession.bestnathan.top -verify_return_error -CAfile /etc/ssl/certs/ca-certificates.crt </dev/null | openssl x509 -noout -serial -issuer -dates'
```

Expected: all serials equal `NEW_SERIAL`, Router keeps current plus one previous version, and MCP/LAN routes remain healthy. Any manual certificate copy invalidates the acceptance result.

- [ ] **Step 4: Verify rules and execute a deterministic Firing-alert test**

Create `tests/prometheus/cert-manager-rules-test.yml`:

```yaml
rule_files:
  - /etc/prometheus/rules/cert-manager.yml
tests:
  - interval: 15s
    input_series:
      - series: 'up{job="cert-manager",instance="cert-manager.cert-manager.svc:9402"}'
        values: '0x30'
    alert_rule_test:
      - eval_time: 6m
        alertname: CertManagerScrapeDown
        exp_alerts:
          - exp_labels:
              severity: critical
              job: cert-manager
              instance: cert-manager.cert-manager.svc:9402
            exp_annotations:
              summary: Prometheus cannot scrape cert-manager
```

Run:

```bash
POD=$(kubectl -n observability get pods -l app=prometheus -o jsonpath='{.items[0].metadata.name}')
kubectl cp tests/prometheus/cert-manager-rules-test.yml "observability/$POD:/tmp/cert-manager-rules-test.yml"
kubectl -n observability exec "$POD" -- promtool test rules /tmp/cert-manager-rules-test.yml
kubectl -n observability exec "$POD" -- rm /tmp/cert-manager-rules-test.yml
kubectl -n observability exec "$POD" -- \
  wget -qO- 'http://localhost:9090/api/v1/rules' | jq '.data.groups[].rules[] | select(.name|test("CertManager|Slb|Mcp")) | {name,state,health}'
```

Expected: `promtool test rules` reports success and proves the Firing transition without touching the production Certificate; live configured rules are loaded and no active expiry/readiness/probe alerts remain.

### Task 11: Runbook, self-review, and issue acceptance

**Files:**
- Create: `docs/runbooks/cert-manager.md`
- Modify externally after approval: `BestNathan/nitops#2` acceptance/evidence comment

- [ ] **Step 1: Create the operational runbook with executable recovery procedures**

Write `docs/runbooks/cert-manager.md` with the following content:

````markdown
# cert-manager Operations Runbook

## Invariants

- cert-manager exclusively writes `slb/bestnathan-top-tls`.
- Never delete CRDs, the cert-manager Namespace, production Certificate, or ACME account Secrets as an upgrade/rollback shortcut.
- Never restore a self-signed public Nession origin while claiming Full(strict).
- `nhome.local`, `default`, and unrelated public services are outside the certificate rollout.

## SLB rollback

Select the reviewed PR-3A commit from `git log --oneline -- components/slb`, then run `read -r PR3A_COMMIT` and:

```bash
git cat-file -e "$PR3A_COMMIT^{commit}"
git revert --no-commit "$PR3A_COMMIT"
git diff --cached --check
kubectl kustomize components/slb > /tmp/nitops-slb-rollback.yaml
kubectl apply --dry-run=client -f /tmp/nitops-slb-rollback.yaml >/dev/null
git diff --cached -- components/slb/nginx-proxy/configmap-sites.yaml
```

Confirm the public paths return to `/etc/nginx/certs/bestnathan.crt` and `.key`, while `nhome` and `default` remain unchanged. Commit, obtain merge approval, and wait for the DaemonSet rollout. Do not delete cert-manager resources.

## Router trusted-version rollback

List current and retained public versions, then select a different retained serial with `read -r ROLLBACK_SERIAL`:

```bash
ssh root@192.168.2.106 'readlink -f /root/docker-nginx/certs/le/current; find /root/docker-nginx/certs/le/versions -mindepth 1 -maxdepth 1 -type d -printf "%f\n"'
ssh root@192.168.2.106 "test -d /root/docker-nginx/certs/le/versions/$ROLLBACK_SERIAL; ln -sfn versions/$ROLLBACK_SERIAL /root/docker-nginx/certs/le/current.new && mv -Tf /root/docker-nginx/certs/le/current.new /root/docker-nginx/certs/le/current; docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload"
```

Verify the actual MCP SNI serial and chain after rollback. To close public Nession instead:

```bash
scp router/nginx/nession-deny-only.conf root@192.168.2.106:/root/docker-nginx/conf/nession.conf
ssh root@192.168.2.106 'docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload'
```

## Controller rollback

Select the reviewed cert-manager source commit with `git log --oneline -- components/cert-manager`, run `read -r CERT_MANAGER_COMMIT`, then:

```bash
git cat-file -e "$CERT_MANAGER_COMMIT^{commit}"
git revert --no-commit "$CERT_MANAGER_COMMIT"
git diff --cached --check
kubectl apply --dry-run=client -R -f components/cert-manager >/dev/null
```

Review and merge the revert through GitOps. Never use `kubectl delete crd`, Namespace deletion, or production Secret deletion.

## ACME account-key loss

Use this only when the account Secret is confirmed missing, not when ACME is merely failing:

```bash
kubectl -n cert-manager get secret letsencrypt-production-account-key
kubectl -n cert-manager rollout restart deployment/cert-manager
kubectl -n cert-manager rollout status deployment/cert-manager --timeout=5m
kubectl wait --for=condition=Ready clusterissuer/letsencrypt-production --timeout=10m
kubectl -n slb get certificate bestnathan-wildcard -o jsonpath='{.status.conditions}' | jq
```

cert-manager generates a new account key and re-registers with the configured email. Existing issued certificates remain in place; do not delete them.

## Router token rotation

Create a second token Secret without printing its data:

```bash
kubectl -n slb apply -f - <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: router-cert-reader-token-2
  annotations:
    kubernetes.io/service-account.name: router-cert-reader
type: kubernetes.io/service-account-token
YAML
ROTATE_DIR=$(mktemp -d /tmp/nitops-router-rotate.XXXXXX)
chmod 700 "$ROTATE_DIR"
kubectl -n slb get secret router-cert-reader-token-2 -o jsonpath='{.data.token}' | base64 -d > "$ROTATE_DIR/token"
chmod 600 "$ROTATE_DIR/token"
scp "$ROTATE_DIR/token" root@192.168.2.106:/root/nitops-cert-sync/token.new
ssh root@192.168.2.106 'chmod 600 /root/nitops-cert-sync/token.new; mv /root/nitops-cert-sync/token.new /root/nitops-cert-sync/token; set -a; . /root/nitops-cert-sync/env; set +a; /usr/local/sbin/nitops-cert-sync --check'
rm -rf "$ROTATE_DIR"
kubectl -n slb delete secret router-cert-reader-token
```

If validation fails, do not delete the old token Secret. Restore service using the old valid credential or keep public MCP closed while investigating.

## Validation commands

```bash
python3 -m unittest discover -s scripts/cert_manager -p 'test_*.py' -v
python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v
bash tests/slb/test-cert-reloader.sh
shellcheck tests/slb/test-cert-reloader.sh
kubectl kustomize components/slb > /tmp/nitops-slb-check.yaml
kubectl apply --dry-run=client -f /tmp/nitops-slb-check.yaml >/dev/null
kubectl -n observability exec deploy/prometheus -- promtool check config /etc/prometheus/prometheus.yml
kubectl -n observability exec deploy/blackbox-exporter -- /bin/blackbox_exporter --config.file=/etc/blackbox/blackbox.yml --config.check
ssh root@192.168.2.106 'docker exec nginx-proxy nginx -t'
```

## Baseline comparison

After each stage, hash only public certificates and compare with the pre-rollout values:

```bash
for POD in $(kubectl -n slb get pods -l app=nginx-proxy -o name); do
  kubectl -n slb exec "$POD" -- sha256sum /etc/nginx/certs/nhome.crt /etc/nginx/certs/default.crt
done
ssh root@192.168.2.106 'sha256sum /root/docker-nginx/certs/argocd.crt; docker exec nginx-proxy nginx -t'
```

Check representative ArgoCD, EMQX, Grafana, MinIO and `nhome.local` routes with their existing trust model. Do not convert an unrelated self-signed baseline failure into a new cert-manager acceptance claim.
````

Expected: the runbook contains executable SLB, Router, controller, ACME account and token recovery procedures plus validation and baseline commands; it contains no real token or private key.

- [ ] **Step 2: Perform the final source/security review**

Run:

```bash
python3 -m unittest discover -s scripts/cert_manager -p 'test_*.py' -v
python3 -m unittest discover -s router/cert-sync -p 'test_*.py' -v
python3 scripts/cert_manager/test_manifests.py -v
rg -n 'BEGIN .*PRIVATE KEY|apiToken:|insecure_skip_verify: true|skipTLSVerify: true' \
  components/cert-manager components/slb components/observability/prometheus router || true
kubectl apply --dry-run=client -R -f components/cert-manager >/dev/null
kubectl kustomize components/slb > /tmp/nitops-slb-final.yaml
kubectl apply --dry-run=client -f /tmp/nitops-slb-final.yaml >/dev/null
```

Expected: tests PASS and no prohibited plaintext/trust-bypass match. Verify only `*.bestnathan.top` is issued and no Certificate/SealedSecret exists for `nhome.local`.

- [ ] **Step 3: Attach sanitized evidence and update issue #2**

Include exact commits, ArgoCD and Kubernetes readback, certificate serials/dates/issuers/SAN, per-node and Router convergence, controlled renewal timing, alert readback, and positive/negative Host/SNI/path/source probes. Mark any unavailable production MCP or external probe as Pending. Do not claim completion from manifests or PR merges alone. Obtain approval before posting the issue comment; do not include tokens, private keys, full Secret YAML, personal email addresses, or internal credentials.

- [ ] **Step 4: Final cleanup**

Remove only temporary local download/certificate directories created by these tasks after evidence is complete. Preserve the initial non-secret CRD/Issuer backup until issue #2 acceptance is closed. Leave cert-manager, CRDs, ACME account Secrets, production Certificate, and Router last-known-good versions installed.
