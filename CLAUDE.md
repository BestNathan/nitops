# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GitOps control plane for Kubernetes observability components, managed by ArgoCD. Repo: `https://github.com/BestNathan/nitops`

**Stack:** Prometheus (metrics), Loki (logs), Grafana (dashboards)
**Namespace:** `observability`
**Tech:** Plain Kubernetes YAML manifests — no Helm, no Kustomize.

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
│   └── shared-nfs-pv.yaml     # Shared NFS PersistentVolume
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
│   │   └── pvc.yaml           # PVC binding to shared NFS
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
  - PersistentVolumes (PV) are cluster-scoped resources defined under `components/cluster/`
  - PersistentVolumeClaims (PVC) are namespace-scoped resources and should be declared by each namespace that needs storage
  - Workloads choose their own `subPath` values in their Deployments to isolate data inside a shared PVC
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

The repository uses a shared NFS PersistentVolume (`shared-nfs`) as a cluster-scoped storage resource. Namespaces that need shared NFS storage declare their own PVCs against that PV, then each workload uses `subPath` in its Deployment to isolate its data.

### observability

The `observability` namespace binds its own PVC to `shared-nfs` and uses per-component `subPath` values:
- Prometheus → `subPath: prometheus`
- Loki → `subPath: loki`
- Grafana → `subPath: grafana`
- Jaeger → `subPath: jaeger`

### new-api

The `new-api` namespace binds its own PVC to `shared-nfs` and uses only `new-api/`-prefixed `subPath` values:
- new-api data → `subPath: new-api/data`
- new-api logs → `subPath: new-api/logs`
- PostgreSQL data → `subPath: new-api/postgres`

MinIO is separate from this shared-PV pattern and uses its own dedicated PV/PVC resources under `components/minio/`.
