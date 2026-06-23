# Coder Kubernetes Deployment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy Coder server and PostgreSQL on Kubernetes following the existing new-api GitOps pattern.

**Architecture:** 11 new files under `components/coder/` and `apps/coder.yaml`. Dedicated `coder` namespace with its own NFS PV/PVC. Coder uses K8s-native workspace provisioner (no Docker socket). Postgres password in a K8s Secret, everything else as plain env vars.

**Tech Stack:** Kubernetes YAML manifests, Kustomize, ArgoCD, Higress ingress, NFS storage

---

## File Map

| File | Purpose |
|------|---------|
| `components/coder/namespace/namespace.yaml` | Create `coder` namespace |
| `components/coder/namespace/pv.yaml` | Cluster-scoped NFS PersistentVolume |
| `components/coder/namespace/pvc.yaml` | Namespace-scoped PVC binding to PV |
| `components/coder/secret.yaml` | Opaque Secret for postgres password |
| `components/coder/postgres/deployment.yaml` | PostgreSQL 17 with health probes |
| `components/coder/postgres/service.yaml` | ClusterIP for postgres:5432 |
| `components/coder/coder/deployment.yaml` | Coder server with env, probes, volume |
| `components/coder/coder/service.yaml` | ClusterIP for coder:7080 |
| `components/coder/coder/ingress.yaml` | Higress dual-host ingress |
| `components/coder/kustomization.yaml` | Kustomize entrypoint listing all resources |
| `apps/coder.yaml` | ArgoCD Application pointing to components/coder |

No existing files are modified.

---

### Task 1: Namespace, PV, PVC

**Files:**
- Create: `components/coder/namespace/namespace.yaml`
- Create: `components/coder/namespace/pv.yaml`
- Create: `components/coder/namespace/pvc.yaml`

- [ ] **Step 1: Create namespace**

Write `components/coder/namespace/namespace.yaml`:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: coder
```

- [ ] **Step 2: Create PersistentVolume**

Write `components/coder/namespace/pv.yaml`:
```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: coder-nfs
  labels:
    app: coder
    type: nfs
spec:
  capacity:
    storage: 100Gi
  accessModes:
    - ReadWriteMany
  persistentVolumeReclaimPolicy: Retain
  nfs:
    server: 192.168.2.105
    path: /mnt/share/k8s
```

- [ ] **Step 3: Create PersistentVolumeClaim**

Write `components/coder/namespace/pvc.yaml`:
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: coder-nfs
  namespace: coder
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 100Gi
  volumeName: coder-nfs
```

- [ ] **Step 4: Validate**

Run: `kubectl apply --dry-run=client -f components/coder/namespace/`
Expected: `namespace/coder created (dry run)`, `persistentvolume/coder-nfs created (dry run)`, `persistentvolumeclaim/coder-nfs created (dry run)`

- [ ] **Step 5: Commit**

```bash
git add components/coder/namespace/namespace.yaml components/coder/namespace/pv.yaml components/coder/namespace/pvc.yaml
git commit -m "feat: add coder namespace, PV, and PVC

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Secret

**Files:**
- Create: `components/coder/secret.yaml`

- [ ] **Step 1: Create Secret**

Write `components/coder/secret.yaml`:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: coder-secret
  namespace: coder
type: Opaque
stringData:
  postgres-password: "123456"
```

- [ ] **Step 2: Validate**

Run: `kubectl apply --dry-run=client -f components/coder/secret.yaml`
Expected: `secret/coder-secret created (dry run)`

- [ ] **Step 3: Commit**

```bash
git add components/coder/secret.yaml
git commit -m "feat: add coder secret for postgres password

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: PostgreSQL Deployment + Service

**Files:**
- Create: `components/coder/postgres/deployment.yaml`
- Create: `components/coder/postgres/service.yaml`

- [ ] **Step 1: Create PostgreSQL Deployment**

Write `components/coder/postgres/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: postgres
  namespace: coder
  labels:
    app: coder-postgres
spec:
  replicas: 1
  selector:
    matchLabels:
      app: coder-postgres
  template:
    metadata:
      labels:
        app: coder-postgres
    spec:
      containers:
        - name: postgres
          image: postgres:17
          env:
            - name: POSTGRES_USER
              value: "coder"
            - name: POSTGRES_DB
              value: "coder"
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: coder-secret
                  key: postgres-password
          ports:
            - containerPort: 5432
              name: postgres
          livenessProbe:
            exec:
              command:
                - pg_isready
                - -U
                - coder
                - -d
                - coder
            initialDelaySeconds: 10
            periodSeconds: 10
          readinessProbe:
            exec:
              command:
                - pg_isready
                - -U
                - coder
                - -d
                - coder
            initialDelaySeconds: 5
            periodSeconds: 5
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
              subPath: coder/postgres
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: coder-nfs
```

- [ ] **Step 2: Create PostgreSQL Service**

Write `components/coder/postgres/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: postgres
  namespace: coder
  labels:
    app: coder-postgres
spec:
  type: ClusterIP
  selector:
    app: coder-postgres
  ports:
    - port: 5432
      targetPort: 5432
      protocol: TCP
      name: postgres
```

- [ ] **Step 3: Validate**

Run: `kubectl apply --dry-run=client -f components/coder/postgres/`
Expected: `deployment.apps/postgres created (dry run)`, `service/postgres created (dry run)`

- [ ] **Step 4: Commit**

```bash
git add components/coder/postgres/deployment.yaml components/coder/postgres/service.yaml
git commit -m "feat: add coder postgres deployment and service

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Coder Deployment, Service, Ingress

**Files:**
- Create: `components/coder/coder/deployment.yaml`
- Create: `components/coder/coder/service.yaml`
- Create: `components/coder/coder/ingress.yaml`

- [ ] **Step 1: Create Coder Deployment**

Write `components/coder/coder/deployment.yaml`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: coder
  namespace: coder
  labels:
    app: coder
spec:
  replicas: 1
  selector:
    matchLabels:
      app: coder
  template:
    metadata:
      labels:
        app: coder
    spec:
      containers:
        - name: coder
          image: ghcr.io/coder/coder:latest
          env:
            - name: CODER_HTTP_ADDRESS
              value: "0.0.0.0:7080"
            - name: CODER_ACCESS_URL
              value: "https://coder.bestnathan.top"
            - name: POSTGRES_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: coder-secret
                  key: postgres-password
            - name: CODER_PG_CONNECTION_URL
              value: "postgresql://coder:$(POSTGRES_PASSWORD)@postgres.coder.svc:5432/coder?sslmode=disable"
          ports:
            - containerPort: 7080
              name: http
          livenessProbe:
            httpGet:
              path: /api/v2
              port: 7080
            initialDelaySeconds: 15
            periodSeconds: 15
          readinessProbe:
            httpGet:
              path: /api/v2
              port: 7080
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: home
              mountPath: /home/coder
              subPath: coder/home
      volumes:
        - name: home
          persistentVolumeClaim:
            claimName: coder-nfs
```

- [ ] **Step 2: Create Coder Service**

Write `components/coder/coder/service.yaml`:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: coder
  namespace: coder
  labels:
    app: coder
spec:
  type: ClusterIP
  selector:
    app: coder
  ports:
    - port: 7080
      targetPort: 7080
      protocol: TCP
      name: http
```

- [ ] **Step 3: Create Coder Ingress**

Write `components/coder/coder/ingress.yaml`:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: coder
  namespace: coder
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "false"
    higress.io/domain: "coder.nhome.local,coder.bestnathan.top"
spec:
  ingressClassName: higress
  rules:
    - host: coder.nhome.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: coder
                port:
                  number: 7080
    - host: coder.bestnathan.top
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: coder
                port:
                  number: 7080
```

- [ ] **Step 4: Validate**

Run: `kubectl apply --dry-run=client -f components/coder/coder/`
Expected: `deployment.apps/coder created (dry run)`, `service/coder created (dry run)`, `ingress.networking.k8s.io/coder created (dry run)`

- [ ] **Step 5: Commit**

```bash
git add components/coder/coder/deployment.yaml components/coder/coder/service.yaml components/coder/coder/ingress.yaml
git commit -m "feat: add coder deployment, service, and ingress

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Kustomization Entrypoint

**Files:**
- Create: `components/coder/kustomization.yaml`

- [ ] **Step 1: Create Kustomization**

Write `components/coder/kustomization.yaml`:
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

- [ ] **Step 2: Validate full kustomize build**

Run: `kubectl apply --dry-run=client -k components/coder/`
Expected: All 9 resources show `created (dry run)` with no errors.

- [ ] **Step 3: Commit**

```bash
git add components/coder/kustomization.yaml
git commit -m "feat: add coder kustomization entrypoint

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: ArgoCD Application

**Files:**
- Create: `apps/coder.yaml`

- [ ] **Step 1: Create ArgoCD Application**

Write `apps/coder.yaml`:
```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: coder
  namespace: argocd
  labels:
    app: coder
spec:
  project: default
  source:
    repoURL: https://github.com/BestNathan/nitops.git
    targetRevision: main
    path: components/coder
  destination:
    server: https://kubernetes.default.svc
    namespace: coder
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

- [ ] **Step 2: Validate**

Run: `kubectl apply --dry-run=client -f apps/coder.yaml`
Expected: `application.argoproj.io/coder created (dry run)`

- [ ] **Step 3: Commit**

```bash
git add apps/coder.yaml
git commit -m "feat: add coder ArgoCD application

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 7: Full Validation & Final Commit

- [ ] **Step 1: Dry-run the complete stack**

Run: `kubectl apply --dry-run=client -k components/coder/ && kubectl apply --dry-run=client -f apps/coder.yaml`
Expected: All 10 resources (9 from kustomize + 1 ArgoCD app) show `created (dry run)`.

- [ ] **Step 2: Verify file tree**

Run: `find components/coder apps/coder.yaml -type f | sort`
Expected:
```
apps/coder.yaml
components/coder/coder/deployment.yaml
components/coder/coder/ingress.yaml
components/coder/coder/service.yaml
components/coder/kustomization.yaml
components/coder/namespace/namespace.yaml
components/coder/namespace/pv.yaml
components/coder/namespace/pvc.yaml
components/coder/postgres/deployment.yaml
components/coder/postgres/service.yaml
components/coder/secret.yaml
```

- [ ] **Step 3: Verify no unintended modifications**

Run: `git status`
Expected: All 11 files shown as new (untracked or staged). No modified files.

- [ ] **Step 4: Final commit (if any unstaged changes remain)**

```bash
git status
# If clean, no action needed. If files remain, stage and commit.
```
