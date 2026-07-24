# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitOps control plane for Kubernetes observability components, managed by ArgoCD. Repo: `https://github.com/BestNathan/nitops`

**Stack:** Prometheus (metrics), Loki (logs), Grafana (dashboards)
**Namespace:** `observability`
**Tech:** Plain Kubernetes YAML manifests — no Helm, no Kustomize.

## Cluster Access

### Kubectl

Kubectl context is configured locally — run commands directly:

```sh
kubectl get node -o wide   # list all nodes with IPs, roles, versions
```

### SSH to Nodes

All cluster nodes are on the local network and accept root SSH with key auth:

```sh
# Discover node IPs then SSH:
kubectl get node -o wide   # get INTERNAL-IP column
ssh root@<node-ip>         # key auth, no password needed
```

The cluster has a mix of amd64 (Ubuntu) and arm64 (Armbian/Rockchip) nodes — check `kubectl get node -o wide` for arch info via OS-IMAGE column.

### ARM64 Node Caveats

One or more nodes may be ARM64 (Rockchip). On these nodes:
- **Registry mirror:** containerd uses `http://192.168.2.106:5000` as mirror for `docker.io`. These nodes have **no public internet access** — all images must be available via the mirror or pre-cached.
- **DNS fallback failure:** when the mirror doesn't have a tag, containerd falls back to `registry-1.docker.io` which fails DNS resolution (`no such host`). This causes pods to get stuck in `ContainerCreating` → eventually `Terminating`.
- **Workaround:** use `@sha256:...` digest images for any image not explicitly cached by tag on the node.
- **Image pull on ARM64 is slow/timeout-prone** — adjust `activeDeadlineSeconds` accordingly for CronJobs targeting ARM64 nodes.
- **containerd version:** 1.7.28 (uses deprecated `mirrors` config, not `config_path`). To inspect images on a node: `ssh root@<node-ip> crictl images` or `ssh root@<node-ip> ctr -n k8s.io image ls`.

### Registry Mirror

```
http://192.168.2.106:5000    # docker.io mirror (registry cache)
http://192.168.2.106:5002    # ghcr.io mirror
http://192.168.2.106:5004    # quay.io mirror
```

## Architecture

Three-layer App-of-Apps pattern:

```
apps/                          # ArgoCD Application resources
├── app-of-apps.yaml           # Root app — bootstraps everything
├── cluster.yaml               # Cluster-level resources (PVs)
├── observability-namespace.yaml  # Namespace + PVC
├── prometheus.yaml            # ArgoCD app for Prometheus
├── loki.yaml                  # ArgoCD app for Loki
├── grafana.yaml               # ArgoCD app for Grafana
├── minio.yaml                 # ArgoCD app for MinIO
├── redis.yaml                 # ArgoCD app for Redis
├── jaeger.yaml                # ArgoCD app for Jaeger
├── new-api.yaml               # ArgoCD app for new-api stack
└── mcp.yaml                   # ArgoCD sub-app-of-apps → apps-mcp/ (directory recurse)

apps-mcp/                      # MCP services — ArgoCD Application resources
├── namespace.yaml             # mcp namespace definition
└── docs-rs-mcp.yaml           # ArgoCD app for docs-rs-mcp

components/                    # Kubernetes manifests
├── cluster/                   # Cluster-scoped resources
│   └── shared-nfs-pv.yaml     # Shared NFS PersistentVolume for observability
├── observability/             # observability namespace resources
│   ├── namespace/
│   │   ├── namespace.yaml     # Namespace definition
│   │   └── pvc.yaml           # PVC binding to shared NFS
│   ├── prometheus/
│   ├── loki/
│   ├── grafana/
│   └── jaeger/
├── new-api/                   # new-api namespace resources
│   ├── namespace/
│   │   ├── namespace.yaml     # Namespace definition
│   │   ├── pv.yaml            # Dedicated NFS PersistentVolume
│   │   └── pvc.yaml           # PVC binding to the dedicated PV
│   ├── new-api/
│   ├── postgres/
│   └── redis/
├── minio/
├── redis/
└── mcp/
    └── docs-rs-mcp/
        ├── deployment.yaml
        └── service.yaml
```

**Bootstrap flow:** `kubectl apply -f apps/app-of-apps.yaml` → ArgoCD creates child Applications → each Application syncs its component manifests.

The `cluster.yaml` app deploys cluster-scoped resources (no namespace). The `observability-namespace.yaml` app creates the namespace and shared PVC. Component apps (prometheus, loki, grafana) deploy into the `observability` namespace.

## Key Conventions

- **One resource per file** — DO NOT merge different resources into one YAML file
- **NFS storage:** server=`192.168.2.105`, mount=`/mnt/share/k8s`
- **Storage model:**
  - PersistentVolumes (PV) are cluster-scoped Kubernetes resources, but repository ownership can be either shared (`components/cluster/`) or app-local (`components/<app>/namespace/`)
  - PersistentVolumeClaims (PVC) are namespace-scoped resources and should be declared alongside the workloads that use them
  - Workloads choose their own `subPath` values in their Deployments to isolate data inside a shared PVC when multiple components in the same namespace reuse one claim
- Loki uses MinIO S3 (`minio.minio.svc:9000`) for object storage
- ArgoCD apps use `syncPolicy.automated` with `prune: true` and `selfHeal: true`
- **Ingress convention** (Higress):
  - Always use `ingressClassName: higress`
  - Add annotation `nginx.ingress.kubernetes.io/ssl-redirect: "false"`
  - Add annotation `higress.io/domain: "<app>.nhome.local"`
  - Domain pattern: `<app>.nhome.local` → service name, port matches the app
  - MinIO has two ingresses: `api.minio.nhome.local` (9000) and `console.minio.nhome.local` (9001)
  - Higress gateway is NodePort — external access uses port `31693` (HTTP) / `32077` (HTTPS)
  - For external access, configure DNS or router port forwarding: external 80 → node IP:31693
- **Edge proxy** (nginx DaemonSet in `slb` namespace):
  - `hostNetwork: true`, listens on ports `80` and `443` on every worker node
  - Purpose: terminate SSL at the edge and proxy to internal services (Higress or direct)
  - nginx configuration stored on NFS (`slb-nfs` PVC, `subPath: slb/nginx-conf`)
  - Site configs live under `conf.d/*.conf`, certs under `certs/`
  - Init containers auto-seed default configs and self-signed certs on first deploy (idempotent, won't overwrite existing files)
  - For new domains: add a `.conf` file on NFS + cert, then `kubectl -n slb rollout restart ds/nginx-proxy`
- **SSL certificate convention:**
  - Each component owns its own TLS certificates on its own NFS storage (`components/<app>/`, subPath: `<app>/certs/`)
  - slb nginx-proxy reads app certs **read-only** via its own PVC (`slb-nfs`) using the same NFS subPath (`<app>/certs/`) — possible because both PVs mount the same NFS server
  - Cert path in nginx: `/etc/nginx/certs/by-app/<app>/<app>.crt` and `.key`
  - slb-managed certs (not app-specific): `certs/default.crt`, `certs/default.key` on `slb-nfs`
  - Self-signed certs are auto-generated by `init-certs` container for bootstrapping; replace with CA-signed certs by overwriting files on NFS
  - Cert ownership rule: **the component that serves the traffic owns the cert**; slb only reads it
  - Example: `coder.bestnathan.top` cert lives in `components/coder/` namespace on `coder-nfs` at `coder/home/certs/`, slb mounts it via `slb-nfs` with `subPath: coder/home/certs`

## Useful Commands

**Get ArgoCD admin password:**
```sh
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d; echo
```

**Validate manifests (dry-run):**
```sh
kubectl apply --dry-run=client -f components/
```

**Check ArgoCD app status:**
```sh
argocd app list
```

**Rollback an app:**
```sh
argocd app rollback <app-name>
```

## Storage Model

PersistentVolumes are cluster-scoped Kubernetes resources, but this repository stores them by ownership boundary rather than forcing every PV into one directory.

- Shared infrastructure PVs can live under `components/cluster/`
- App-dedicated PVs can live with the owning namespace resources under `components/<app>/namespace/`
- PVCs are always namespace-scoped and should be declared in the same namespace as the workloads that use them
- When multiple workloads in one namespace share a PVC, each workload should use its own `subPath` to isolate data

### observability

The `observability` namespace uses the shared cluster-level PV `shared-nfs` and binds it through `components/observability/namespace/pvc.yaml`. Workloads in that namespace isolate their data with per-component `subPath` values:
- Prometheus → `subPath: prometheus`
- Loki → `subPath: loki`
- Grafana → `subPath: grafana`
- Jaeger → `subPath: jaeger`

### new-api

The `new-api` namespace owns its own NFS PV/PVC pair under `components/new-api/namespace/` and uses only `new-api/`-prefixed `subPath` values:
- new-api data → `subPath: new-api/data`
- new-api logs → `subPath: new-api/logs`
- PostgreSQL data → `subPath: new-api/postgres`

MinIO is separate from both patterns and uses its own dedicated PV/PVC resources under `components/minio/`.
