---
name: nitops-slb
description: Use when maintaining the nitops SLB (Server Load Balancer) nginx-proxy component — adding new domains, TLS certificates, updating proxy rules, or debugging 502/404 routing issues. Also use when adding a new public .bestnathan.top domain that needs external access through the router at 192.168.2.106.
---

# NITOps SLB — Edge Load Balancer

## Architecture

```
Internet → Router (192.168.2.106, Docker nginx)
          → K8s SLB DaemonSet (hostNetwork :80/:443, every worker node)
          → Backend (Higress or directly to K8s Service)
```

Two-tier nginx:
1. **Router nginx** — Docker container `nginx-proxy` on 192.168.2.106, port 80/443. Only handles `*.bestnathan.top` public domains. Config at `/root/docker-nginx/conf/`.
2. **K8s SLB nginx** — DaemonSet `nginx-proxy` in `slb` namespace. `hostNetwork: true`, listens on :80/:443 on every worker node. Handles ALL domains (internal `*.nhome.local` and public `*.bestnathan.top`).

## Component Files

```
components/slb/
├── kustomization.yaml
├── namespace/{namespace,pv,pvc}.yaml    # slb namespace + NFS 10Gi PV/PVC
└── nginx-proxy/
    ├── configmap.yaml                   # nginx.conf (main config)
    ├── configmap-sites.yaml             # All site server blocks (GitOps)
    └── daemonset.yaml                   # nginx:alpine, hostNetwork :80/:443
```

### Volume Mounts

| Mount | Source | Contents |
|-------|--------|----------|
| `/etc/nginx/nginx.conf` | ConfigMap `nginx-config` | Main config (events + http include) |
| `/etc/nginx/conf.d/` | ConfigMap `nginx-sites` | All site configs |
| `/etc/nginx/certs/` | NFS `slb/nginx-conf/certs` | default.crt/key, coder.crt/key |
| `/etc/nginx/certs/by-app/coder/` | NFS `coder/home/certs` | coder TLS cert |
| `/etc/nginx/certs/by-app/argocd/` | NFS `argocd/certs` | argocd TLS cert |
| `/etc/nginx/certs/by-app/emqx/` | NFS `emqx/certs` | emqx TLS certs (dashboard + mqtt) |

### Certificates

- **default** — self-signed for `*.vol.bestnathan.top`, lives on NFS at `slb/nginx-conf/certs/default.{crt,key}`
- **coder** — on NFS at `coder/home/certs/coder.{crt,key}`, SLB mounts via subPath read-only
- **argocd** — on NFS at `argocd/certs/argocd.{crt,key}` (PV `argocd-nfs`), SLB mounts via subPath read-only
- **emqx** — on NFS at `emqx/certs/emqx.{crt,key}` and `emqx/certs/mqtt.{crt,key}` (PV `emqx-nfs`), SLB mounts via subPath read-only. Serves `emqx.{nhome.local,bestnathan.top}` (dashboard) and `mqtt.{nhome.local,bestnathan.top}` (WebSocket)

## Adding a New Domain

### Internal only (`.nhome.local`)

1. Add server block to `components/slb/nginx-proxy/configmap-sites.yaml`:
```nginx
server {
    listen 80;
    server_name <app>.nhome.local;
    location / {
        proxy_pass http://<service>.<namespace>.svc.cluster.local:<port>;
        proxy_set_header Host $host;
        ...
    }
}
```
2. If HTTPS needed: add 443 block + cert mount (see TLS section below)
3. Commit → ArgoCD sync → DaemonSet rolling update

### Public (`.bestnathan.top`)

1. Add server block(s) to `configmap-sites.yaml` (same as internal but with `.bestnathan.top` hostname)
2. Commit → ArgoCD sync → SLB DaemonSet update
3. **Also add config on Router (192.168.2.106):**
```bash
ssh root@192.168.2.106
# Edit /root/docker-nginx/conf/<app>.conf
docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload
```
4. If HTTPS: also copy cert to router + update config (see TLS section)

## Adding TLS Certificate

### For a new app on K8s SLB

1. Create NFS-backed PV/PVC for the app (if not exists):
```yaml
# components/<app>/namespace/pv.yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: <app>-nfs
spec:
  capacity: {storage: 10Gi}
  accessModes: [ReadWriteMany]
  nfs: {server: 192.168.2.105, path: /mnt/share/k8s}
```
2. Write cert to NFS at `<app>/certs/<app>.{crt,key}`
3. Add volume mount in SLB DaemonSet:
```yaml
- name: nfs-root
  mountPath: /etc/nginx/certs/by-app/<app>
  subPath: <app>/certs
  readOnly: true
```
4. Update site config in `configmap-sites.yaml` to reference cert path:
```nginx
ssl_certificate     /etc/nginx/certs/by-app/<app>/<app>.crt;
ssl_certificate_key /etc/nginx/certs/by-app/<app>/<app>.key;
```

### For Router (192.168.2.106)

1. Copy cert to router:
```bash
scp <app>.crt root@192.168.2.106:/root/docker-nginx/certs/
scp <app>.key root@192.168.2.106:/root/docker-nginx/certs/
```
2. Add HTTPS server block in `/root/docker-nginx/conf/<app>.conf`
3. Reload: `docker exec nginx-proxy nginx -t && docker exec nginx-proxy nginx -s reload`

## Constraints

1. **No init containers** — Configs come from ConfigMaps (GitOps), certs from NFS. No auto-generation anymore.
2. **ConfigMap size limit** — `nginx-sites` ConfigMap must stay under 1MB. If it grows too large, split into multiple ConfigMaps.
3. **Cert on NFS only** — Certs are NOT in ConfigMaps (too large, sensitive). They live on NFS and are mounted read-only.
4. **hostNetwork is required** — SLB must bind to host ports 80/443. Cannot run behind K8s Service/NodePort.
5. **Rock 5B Plus disk** — Node `rock-5b-plus` has `/var/log` on 50MB zram. Pod log symlinks to `/var/log.hdd/` are maintained by `k8s-log-symlink.service`. If SLB pod fails to start on this node, check `df -h /var/log`.
6. **Router is Docker, not K8s** — Router at 192.168.2.106 runs nginx in Docker. Not managed by GitOps. Manual config update required for new public domains.
7. **Kustomization must be updated** — When adding new files to `components/slb/`, update `kustomization.yaml`.

## Quality Gates

### Before Commit

- [ ] `kubectl apply --dry-run=client -f components/slb/` passes
- [ ] `kustomization.yaml` includes all new resources
- [ ] ConfigMap nginx syntax: confirm valid nginx config (no missing semicolons, valid paths)

### After ArgoCD Sync

- [ ] `kubectl -n slb get pods` — all pods Running on all 3 worker nodes
- [ ] `kubectl -n slb exec ds/nginx-proxy -- nginx -t` — config syntax OK
- [ ] `kubectl -n argocd get application slb` — Synced/Healthy

### After Deploy

- [ ] HTTP 200 test from internal:
```bash
curl -sk -o /dev/null -w "%{http_code}" -H "Host: <domain>" "http://<node-ip>:80/"
```
- [ ] HTTPS 200 test (if TLS configured)
- [ ] For public domains: test from router AND from external
```bash
curl -sk -o /dev/null -w "%{http_code}" -H "Host: <domain>" "https://192.168.2.106/"
```

## Common Issues

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| HTTP 502 from router | Router nginx missing domain config | Add config in `/root/docker-nginx/conf/` + reload |
| HTTP 444 from SLB | Domain not in ConfigMap `nginx-sites` | Add server block in `configmap-sites.yaml` |
| HTTP 404 from backend | Wrong upstream service/port | Check `proxy_pass` target: `<svc>.<ns>.svc.cluster.local:<port>` |
| SLB pod stuck ContainerCreating on rock-5b-plus | zram `/var/log` full | Clean logs or restart `k8s-log-symlink` service |
| nginx reload fails on router | Other config has broken upstream hostname | `docker exec nginx-proxy nginx -t` to find error |
| ArgoCD shows OutOfSync for slb | Manual change on cluster (e.g., rollout restart) | Wait for selfHeal or trigger refresh |

## Network Topology

```
                    ┌──────────────────────────────────┐
                    │         Router (192.168.2.106)    │
                    │  Docker: nginx-proxy              │
                    │  Ports: 80→80, 443→443            │
                    │  Conf:  /root/docker-nginx/conf/  │
                    │  Certs: /root/docker-nginx/certs/ │
                    └──────────┬───────────────────────┘
                               │ proxy_pass http://192.168.2.86:80
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
    ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
    │ k8s-worker1 │  │ k8s-worker2 │  │ rock-5b-plus│
    │  .2.86      │  │  .2.232     │  │  .2.224     │
    │ SLB :80/443 │  │ SLB :80/443 │  │ SLB :80/443 │
    └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
           │                │                │
           └────────────────┼────────────────┘
                            │ proxy_pass via K8s Service DNS
              ┌─────────────┼─────────────┬──────────────┐
              ▼             ▼             ▼              ▼
        ┌──────────┐ ┌──────────┐ ┌──────────────┐ ┌──────────┐
        │ ArgoCD   │ │ Coder    │ │ Higress      │ │ EMQX     │
        │ :80      │ │ :7080    │ │ :80 (vol.*)  │ │ :18083   │
        └──────────┘ └──────────┘ └──────────────┘ │ :8083    │
                                                    └──────────┘
```
