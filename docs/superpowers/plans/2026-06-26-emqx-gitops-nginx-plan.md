# EMQX GitOps 纳管 + Nginx 代理 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已运行的 EMQX 5.8.4 纳入 GitOps 管理，同时为 Dashboard 和 MQTT WebSocket 添加 slb nginx-proxy 代理规则。

**Architecture:** 新增 `components/emqx/` 目录（namespace + PV/PVC + StatefulSet + Service + Ingress），在 slb 的 `configmap-sites.yaml` 新增两个 nginx server block（Dashboard HTTP 反向代理 + MQTT WebSocket 升级代理），daemonset 新增 EMQX 证书 volume mount。

**Tech Stack:** Kubernetes YAML (Kustomize), Nginx, ArgoCD

---

### Task 1: 创建 EMQX 命名空间和存储资源

**Files:**
- Create: `components/emqx/namespace/namespace.yaml`
- Create: `components/emqx/namespace/pv.yaml`
- Create: `components/emqx/namespace/pvc.yaml`

- [ ] **Step 1: 创建 namespace.yaml**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: emqx
```

- [ ] **Step 2: 创建 pv.yaml**

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: emqx-nfs
  labels:
    app: emqx
    role: data
spec:
  accessModes:
    - ReadWriteMany
  capacity:
    storage: 10Gi
  nfs:
    path: /mnt/share/emqx
    server: 192.168.2.105
  persistentVolumeReclaimPolicy: Retain
  storageClassName: nfs
  volumeMode: Filesystem
```

- [ ] **Step 3: 创建 pvc.yaml**

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: emqx-data
  namespace: emqx
spec:
  accessModes:
    - ReadWriteMany
  resources:
    requests:
      storage: 10Gi
  selector:
    matchLabels:
      app: emqx
      role: data
  storageClassName: nfs
  volumeMode: Filesystem
```

- [ ] **Step 4: Commit**

```bash
git add components/emqx/namespace/ && git commit -m "feat: add EMQX namespace, PV, and PVC manifests"
```

---

### Task 2: 创建 EMQX Service 资源

**Files:**
- Create: `components/emqx/emqx/service.yaml`

> 注意：两个 Service 放入同一个文件，因为它们是同一组件的紧密耦合资源。

- [ ] **Step 1: 创建 service.yaml**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: emqx-headless
  namespace: emqx
  labels:
    app: emqx
spec:
  clusterIP: None
  ports:
    - name: mqtt
      port: 1883
      targetPort: 1883
    - name: dashboard
      port: 18083
      targetPort: 18083
  selector:
    app: emqx
  type: ClusterIP
---
apiVersion: v1
kind: Service
metadata:
  name: emqx-nodeport
  namespace: emqx
  labels:
    app: emqx
spec:
  ports:
    - name: mqtt
      port: 1883
      targetPort: 1883
      nodePort: 31883
    - name: dashboard
      port: 18083
      targetPort: 18083
      nodePort: 31083
    - name: ws
      port: 8083
      targetPort: 8083
      nodePort: 30883
    - name: wss
      port: 8084
      targetPort: 8084
      nodePort: 30884
  selector:
    app: emqx
  type: NodePort
```

- [ ] **Step 2: Commit**

```bash
git add components/emqx/emqx/service.yaml && git commit -m "feat: add EMQX headless and NodePort services"
```

---

### Task 3: 创建 EMQX StatefulSet

**Files:**
- Create: `components/emqx/emqx/statefulset.yaml`

- [ ] **Step 1: 创建 statefulset.yaml**

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: emqx
  namespace: emqx
spec:
  replicas: 1
  selector:
    matchLabels:
      app: emqx
  serviceName: emqx-headless
  template:
    metadata:
      labels:
        app: emqx
    spec:
      affinity:
        nodeAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - preference:
                matchExpressions:
                  - key: kubernetes.io/hostname
                    operator: In
                    values:
                      - k8s-worker2
              weight: 100
      containers:
        - name: emqx
          image: emqx/emqx:5.8.4
          env:
            - name: EMQX_NODE__NAME
              value: emqx@node.emqx.k8s
          ports:
            - containerPort: 1883
              name: mqtt
            - containerPort: 8083
              name: ws
            - containerPort: 8084
              name: wss
            - containerPort: 8883
              name: mqtts
            - containerPort: 18083
              name: dashboard
          resources:
            limits:
              cpu: "2"
              memory: 2Gi
            requests:
              cpu: 200m
              memory: 512Mi
          volumeMounts:
            - name: emqx-data
              mountPath: /opt/emqx/data
              subPath: k8s-prod/data
      nodeSelector:
        kubernetes.io/arch: amd64
      volumes:
        - name: emqx-data
          persistentVolumeClaim:
            claimName: emqx-data
```

- [ ] **Step 2: Commit**

```bash
git add components/emqx/emqx/statefulset.yaml && git commit -m "feat: add EMQX StatefulSet manifest"
```

---

### Task 4: 创建 EMQX Ingress（MQTT WebSocket / Higress 层）

**Files:**
- Create: `components/emqx/ingress.yaml`

- [ ] **Step 1: 创建 ingress.yaml**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: emqx-mqtt-ws
  namespace: emqx
  annotations:
    higress.io/upstream-read-timeout: "3600"
spec:
  ingressClassName: higress
  rules:
    - http:
        paths:
          - backend:
              service:
                name: emqx-nodeport
                port:
                  number: 8083
            path: /mqtt
            pathType: Prefix
```

- [ ] **Step 2: Commit**

```bash
git add components/emqx/ingress.yaml && git commit -m "feat: add EMQX MQTT WebSocket Higress Ingress"
```

---

### Task 5: 创建 EMQX Kustomization 文件

**Files:**
- Create: `components/emqx/kustomization.yaml`

- [ ] **Step 1: 创建 kustomization.yaml**

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace/namespace.yaml
  - namespace/pv.yaml
  - namespace/pvc.yaml
  - emqx/service.yaml
  - emqx/statefulset.yaml
  - ingress.yaml
```

- [ ] **Step 2: Commit**

```bash
git add components/emqx/kustomization.yaml && git commit -m "feat: add EMQX kustomization.yaml"
```

---

### Task 6: 创建 ArgoCD Application

**Files:**
- Create: `apps/emqx.yaml`

- [ ] **Step 1: 创建 apps/emqx.yaml**

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: emqx
  namespace: argocd
  labels:
    app: emqx
spec:
  project: default
  source:
    repoURL: https://github.com/BestNathan/nitops.git
    targetRevision: main
    path: components/emqx
  destination:
    server: https://kubernetes.default.svc
    namespace: emqx
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

- [ ] **Step 2: Commit**

```bash
git add apps/emqx.yaml && git commit -m "feat: add ArgoCD Application for EMQX"
```

---

### Task 7: 更新 slb nginx-proxy configmap-sites.yaml（新增 EMQX Dashboard + MQTT WS 代理规则）

**Files:**
- Modify: `components/slb/nginx-proxy/configmap-sites.yaml`

- [ ] **Step 1: 追加 emqx-dashboard.conf 和 emqx-ws.conf 到 configmap-sites.yaml data 字段**

在 `configmap-sites.yaml` 的 `data:` 块末尾（`vol.conf` 之后）追加以下内容：

```yaml
  # EMQX Dashboard — HTTP reverse proxy
  emqx-dashboard.conf: |
    server {
      listen 80;
      server_name emqx.nhome.local emqx.bestnathan.top;

      location / {
        proxy_pass http://emqx-nodeport.emqx.svc.cluster.local:18083;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
      }
    }

    server {
      listen 443 ssl;
      server_name emqx.nhome.local emqx.bestnathan.top;

      ssl_certificate     /etc/nginx/certs/by-app/emqx/emqx.crt;
      ssl_certificate_key /etc/nginx/certs/by-app/emqx/emqx.key;
      ssl_protocols       TLSv1.2 TLSv1.3;
      ssl_ciphers         HIGH:!aNULL:!MD5;

      location / {
        proxy_pass http://emqx-nodeport.emqx.svc.cluster.local:18083;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
      }
    }

  # EMQX MQTT WebSocket — WebSocket upgrade proxy
  emqx-ws.conf: |
    server {
      listen 80;
      server_name mqtt.nhome.local mqtt.bestnathan.top;

      location / {
        proxy_pass http://emqx-nodeport.emqx.svc.cluster.local:8083;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
      }
    }

    server {
      listen 443 ssl;
      server_name mqtt.nhome.local mqtt.bestnathan.top;

      ssl_certificate     /etc/nginx/certs/by-app/emqx/mqtt.crt;
      ssl_certificate_key /etc/nginx/certs/by-app/emqx/mqtt.key;
      ssl_protocols       TLSv1.2 TLSv1.3;
      ssl_ciphers         HIGH:!aNULL:!MD5;

      location / {
        proxy_pass http://emqx-nodeport.emqx.svc.cluster.local:8083;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
      }
    }
```

- [ ] **Step 2: Commit**

```bash
git add components/slb/nginx-proxy/configmap-sites.yaml && git commit -m "feat: add EMQX Dashboard and MQTT WS nginx proxy rules"
```

---

### Task 8: 更新 slb nginx-proxy DaemonSet（新增 EMQX 证书 volume mount）

**Files:**
- Modify: `components/slb/nginx-proxy/daemonset.yaml`

- [ ] **Step 1: 在 volumeMounts 数组末尾（argocd mount 之后）添加 EMQX certs mount**

找到 daemonset.yaml 中：
```yaml
            - name: nfs-root
              mountPath: /etc/nginx/certs/by-app/argocd
              subPath: argocd/certs
              readOnly: true
```

在其后追加：
```yaml
            - name: nfs-root
              mountPath: /etc/nginx/certs/by-app/emqx
              subPath: emqx/certs
              readOnly: true
```

- [ ] **Step 2: Commit**

```bash
git add components/slb/nginx-proxy/daemonset.yaml && git commit -m "feat: mount EMQX TLS certs in slb nginx-proxy"
```

---

### Task 9: 在 NFS 上生成 EMQX 自签名证书

**Files:**
- Create: NFS 路径 `emqx/certs/emqx.crt`, `emqx/certs/emqx.key`, `emqx/certs/mqtt.crt`, `emqx/certs/mqtt.key`

> 证书放在 slb-nfs PV 对应的 NFS 路径 `/mnt/share/k8s/emqx/certs/` 下。需要先检查 NFS 是否可从当前节点访问。

- [ ] **Step 1: 检查 NFS 挂载状态**

```bash
showmount -e 192.168.2.105 2>/dev/null || echo "NFS showmount failed"
mount | grep nfs
ls /mnt/share/k8s/ 2>/dev/null || echo "NFS not mounted locally"
```

Expected: 如果本地没有挂载 NFS，需要通过 kubectl 临时 Pod 来操作。

- [ ] **Step 2: 在 NFS 上创建 EMQX certs 目录并生成证书**

如果 NFS 已挂载到本地：

```bash
mkdir -p /mnt/share/k8s/emqx/certs
cd /mnt/share/k8s/emqx/certs

# Dashboard 证书 (SAN: emqx.nhome.local, emqx.bestnathan.top)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout emqx.key -out emqx.crt \
  -days 3650 \
  -subj "/CN=emqx.bestnathan.top" \
  -addext "subjectAltName=DNS:emqx.nhome.local,DNS:emqx.bestnathan.top"

# MQTT WebSocket 证书 (SAN: mqtt.nhome.local, mqtt.bestnathan.top)
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout mqtt.key -out mqtt.crt \
  -days 3650 \
  -subj "/CN=mqtt.bestnathan.top" \
  -addext "subjectAltName=DNS:mqtt.nhome.local,DNS:mqtt.bestnathan.top"
```

如果 NFS 没有本地挂载，通过 kubectl 在 slb namespace 创建临时 Pod：

```bash
kubectl -n slb run cert-bootstrap --rm -i --restart=Never --image=nginx:alpine --overrides='
{
  "spec": {
    "volumes": [
      {"name": "nfs", "persistentVolumeClaim": {"claimName": "slb-nfs"}}
    ],
    "containers": [{
      "name": "cert-bootstrap",
      "image": "nginx:alpine",
      "command": ["sh", "-c"],
      "args": ["mkdir -p /nfs/emqx/certs && cd /nfs/emqx/certs && apk add --no-cache openssl && openssl req -x509 -newkey rsa:2048 -nodes -keyout emqx.key -out emqx.crt -days 3650 -subj \"/CN=emqx.bestnathan.top\" -addext \"subjectAltName=DNS:emqx.nhome.local,DNS:emqx.bestnathan.top\" && openssl req -x509 -newkey rsa:2048 -nodes -keyout mqtt.key -out mqtt.crt -days 3650 -subj \"/CN=mqtt.bestnathan.top\" -addext \"subjectAltName=DNS:mqtt.nhome.local,DNS:mqtt.bestnathan.top\" && ls -la /nfs/emqx/certs/"],
      "volumeMounts": [{"name": "nfs", "mountPath": "/nfs"}]
    }]
  }
}'
```

- [ ] **Step 3: 重启 slb nginx-proxy 加载新证书**

```bash
kubectl -n slb rollout restart daemonset/nginx-proxy
```

- [ ] **Step 4: 验证证书挂载成功**

```bash
kubectl -n slb exec daemonset/nginx-proxy -- ls -la /etc/nginx/certs/by-app/emqx/
```

Expected: 看到 `emqx.crt`, `emqx.key`, `mqtt.crt`, `mqtt.key` 四个文件。

---

### Task 10: 验证 ArgoCD 同步和 nginx 代理

- [ ] **Step 1: 推送所有变更到 GitHub**

```bash
git push origin main
```

- [ ] **Step 2: 等待 ArgoCD 同步 EMQX app**

```bash
argocd app sync emqx
argocd app get emqx
```

Expected: `Healthy`, `Synced`（与现有集群资源无 diff，不会重启 Pod）

- [ ] **Step 3: 测试 nginx Dashboard 代理**

```bash
curl -k -H "Host: emqx.nhome.local" https://192.168.2.106:443/
```

Expected: EMQX Dashboard HTML 页面返回（302 重定向到 `/dashboard` 正常）

- [ ] **Step 4: 测试 nginx MQTT WebSocket 代理**

```bash
curl -k -H "Host: mqtt.nhome.local" https://192.168.2.106:443/mqtt -i
```

Expected: HTTP 400 Bad Request（正常，因为 curl 不是 WebSocket 客户端，但连接已到达 EMQX）

---

### 风险提示

1. **ArgoCD 接管风险低**：manifest 与运行中资源完全一致，不会有 diff → 不触发重启
2. **NFS 证书位置**：证书在 slb-nfs（`/mnt/share/k8s/emqx/certs/`），与 EMQX 数据（`/mnt/share/emqx/`）分离
3. **域名**：`emqx.bestnathan.top` 和 `mqtt.bestnathan.top` 需要在路由器/公网 DNS 配置解析到 `192.168.2.106`
