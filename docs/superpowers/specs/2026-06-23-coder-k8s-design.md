# Coder Kubernetes Deployment — Design Spec

**Date:** 2026-06-23
**Status:** Approved
**Source:** Docker Compose reference → Kubernetes GitOps manifests

## Overview

Deploy [Coder](https://coder.com) (open-source remote development platform) and its PostgreSQL database on Kubernetes, managed by ArgoCD via the existing GitOps control plane. Follows the established App-of-Apps pattern used by all other applications in this repository.

Coder uses its native Kubernetes provisioner for workspace management — no Docker socket required.

## Architecture

Three-layer App-of-Apps pattern:

```
Layer 1: apps/app-of-apps.yaml         →  bootstraps apps/coder.yaml (no change needed)
Layer 2: apps/coder.yaml               →  ArgoCD Application → components/coder/
Layer 3: components/coder/             →  Kustomize → all K8s resources
```

### File Inventory (11 new files)

```
components/coder/
├── kustomization.yaml
├── namespace/
│   ├── namespace.yaml
│   ├── pv.yaml
│   └── pvc.yaml
├── secret.yaml
├── coder/
│   ├── deployment.yaml
│   ├── service.yaml
│   └── ingress.yaml
└── postgres/
    ├── deployment.yaml
    └── service.yaml

apps/
└── coder.yaml
```

## Resource Specifications

### Namespace

- Namespace `coder` — dedicated, isolated from observability and other apps.

### PersistentVolume (`namespace/pv.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder-nfs` |
| Capacity | 100Gi |
| AccessMode | ReadWriteMany |
| ReclaimPolicy | Retain |
| NFS Server | `192.168.2.105` |
| NFS Path | `/mnt/share/k8s` |

Cluster-scoped. Same NFS server/path as `new-api-nfs`.

### PersistentVolumeClaim (`namespace/pvc.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder-nfs` |
| Namespace | `coder` |
| AccessMode | ReadWriteMany |
| Storage | 100Gi |
| volumeName | `coder-nfs` (static binding) |

### Secret (`secret.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder-secret` |
| Namespace | `coder` |
| Type | Opaque |
| Key | `postgres-password` |

Only the Postgres password is stored as a Secret. Non-sensitive values (user, db name) remain as plain env vars in the Deployment.

### PostgreSQL Deployment (`postgres/deployment.yaml`)

| Field | Value |
|-------|-------|
| Name | `postgres` |
| Namespace | `coder` |
| Labels | `app: coder-postgres` |
| Replicas | 1 |
| Image | `postgres:15` (changed from 17 — Docker Hub unreachable on ARM node, 15 is already cached) |
| Port | 5432 |
| POSTGRES_USER | `coder` (plain env var) |
| POSTGRES_DB | `coder` (plain env var) |
| POSTGRES_PASSWORD | From `secretKeyRef: coder-secret.postgres-password` |
| Volume | `coder-nfs` PVC, `subPath: coder/postgres`, mount `/var/lib/postgresql/data` |
| Liveness probe | `pg_isready -U coder -d coder`, initialDelay=10s, period=10s |
| Readiness probe | `pg_isready -U coder -d coder`, initialDelay=5s, period=5s |

Label `coder-postgres` (not just `postgres`) avoids naming ambiguity while remaining namespace-scoped.

### PostgreSQL Service (`postgres/service.yaml`)

| Field | Value |
|-------|-------|
| Name | `postgres` |
| Namespace | `coder` |
| Type | ClusterIP |
| Port | 5432 |
| Selector | `app: coder-postgres` |

Resolves as `postgres.coder.svc:5432`.

### Coder Deployment (`coder/deployment.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder` |
| Namespace | `coder` |
| Labels | `app: coder` |
| Replicas | 1 |
| Image | `ghcr.io/coder/coder:latest` |
| Port | 7080 |
| CODER_HTTP_ADDRESS | `0.0.0.0:7080` |
| CODER_ACCESS_URL | `https://coder.bestnathan.top` |
| CODER_PG_CONNECTION_URL | `postgresql://coder:$(POSTGRES_PASSWORD)@postgres.coder.svc:5432/coder?sslmode=disable` |
| POSTGRES_PASSWORD | From `secretKeyRef: coder-secret.postgres-password` (declared before `CODER_PG_CONNECTION_URL` for variable expansion) |
| Volume | `coder-nfs` PVC, `subPath: coder/home`, mount `/home/coder` |
| Liveness probe | HTTP GET `/api/v2` on port 7080, initialDelay=15s, period=15s |
| Readiness probe | HTTP GET `/api/v2` on port 7080, initialDelay=5s, period=10s |
| Docker socket | **Not mounted** — uses K8s-native workspace provisioner |

No privileged mode, no hostPath mounts. Clean K8s-native deployment.

### Coder Service (`coder/service.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder` |
| Namespace | `coder` |
| Type | ClusterIP |
| Port | 7080 |
| Selector | `app: coder` |

### Coder Ingress (`coder/ingress.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder` |
| Namespace | `coder` |
| IngressClass | `higress` |
| Annotations | `nginx.ingress.kubernetes.io/ssl-redirect: "false"` |
| | `higress.io/domain: "coder.nhome.local,coder.bestnathan.top"` |
| Host 1 | `coder.nhome.local` → `coder:7080` |
| Host 2 | `coder.bestnathan.top` → `coder:7080` |
| PathType | Prefix |

### Kustomization (`kustomization.yaml`)

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace/namespace.yaml
  - namespace/pv.yaml
  - namespace/pvc.yaml
  - secret.yaml
  - coder/deployment.yaml
  - coder/service.yaml
  - coder/ingress.yaml
  - postgres/deployment.yaml
  - postgres/service.yaml
```

Flat resource list. Order places namespace creation before namespace-scoped resources.

### ArgoCD Application (`apps/coder.yaml`)

| Field | Value |
|-------|-------|
| Name | `coder` |
| Namespace | `argocd` |
| Source | `https://github.com/BestNathan/nitops.git`, branch `main`, path `components/coder` |
| Destination | `https://kubernetes.default.svc`, namespace `coder` |
| SyncPolicy | Automated, prune=true, selfHeal=true |

No changes to `apps/app-of-apps.yaml` — the `directory` recursion with `exclude: "app-of-apps.yaml"` picks up `coder.yaml` automatically.

## Data Flow

```
User browser → Ingress (Higress) → coder.coder.svc:7080 → Coder pod
Coder pod → postgres.coder.svc:5432 → Postgres pod → NFS coder/postgres/
Coder pod → NFS coder/home/ (persistent state)
Workspaces → https://coder.bestnathan.top (CODER_ACCESS_URL proxy)
```

### Startup Sequence

```
PV created (cluster-scoped)
  → Namespace created
    → PVC binds to PV
      → Postgres pod starts → readiness probe passes
        → Coder pod starts → connects to Postgres, serves on :7080
```

No explicit `depends_on` — soft ordering via Kubernetes restart loop when Postgres is unavailable.

## Error Handling

- **Postgres unavailable:** Coder liveness probe fails → pod restarts. Reconnects when Postgres recovers.
- **NFS unavailable:** Both pods lose storage access. Probes eventually fail, pods restart when NFS returns.
- **Coder crash:** Deployment restarts pod. Persistent state survives via NFS `/home/coder`.

## Deviations from Docker Compose

| Docker Compose | Kubernetes Equivalent | Reason |
|----------------|----------------------|--------|
| `/var/run/docker.sock` mount | Not mounted | K8s-native workspace provisioner instead |
| `depends_on: database (healthy)` | K8s restart loop + probes | No equivalent in K8s; soft ordering |
| Docker named volumes | NFS PV/PVC with subPaths | Follows existing repo storage model |
| `${VAR:-default}` syntax | K8s env vars + `secretKeyRef` | K8s-native config |
| `healthcheck: pg_isready` | Liveness + readiness probes | K8s-native health checking |
| `group_add: docker` | Not needed | No Docker socket |

## Open Items

- **RBAC for workspace provisioning** — Deferred. Coder needs cluster permissions to create workspace Pods/Services/PVCs. A scoped ClusterRole should be created when workspace templates are added. For now, the initial deployment serves the Coder dashboard and API; workspaces will fail to provision until RBAC is configured.

## Testing

```sh
# Dry-run validation
kubectl apply --dry-run=client -f components/coder/

# Post-deployment verification
kubectl -n coder get pods
kubectl -n coder logs deployment/coder
kubectl -n coder logs deployment/postgres
argocd app get coder

# Coder health check
curl http://coder.nhome.local:31693/api/v2
```
