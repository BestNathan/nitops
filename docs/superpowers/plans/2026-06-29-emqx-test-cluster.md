# EMQX k8s-test 集群实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `emqx` 命名空间新增 `k8s-test` 测试集群，支持 DNS 自动发现和弹性扩缩（3-10 节点），不动现有 `k8s-prod`。

**Architecture:** 新增 StatefulSet `emqx-test`（DNS 发现 + subPathExpr 数据隔离）+ Headless Service + NodePort Service，复用现有 NFS PVC。`replicas` 改数字即扩缩。

**Tech Stack:** Kubernetes StatefulSet, EMQX 5.8.4, NFS storage, Kustomize (聚合用)

---

## 文件结构

```
components/emqx/
├── kustomization.yaml              # Modify: 追加 test 资源
├── emqx-test/                       # Create dir
│   ├── statefulset.yaml            # Create: 集群化 StatefulSet
│   └── service.yaml                # Create: headless + nodeport
├── emqx/                            # Unchanged
│   ├── statefulset.yaml
│   └── service.yaml
├── ingress.yaml                     # Unchanged
└── namespace/                       # Unchanged
    ├── namespace.yaml
    ├── pv.yaml
    └── pvc.yaml
```

---

### Task 1: 创建 StatefulSet

**Files:**
- Create: `components/emqx/emqx-test/statefulset.yaml`

- [ ] **Step 1: 创建目录**

```bash
mkdir -p components/emqx/emqx-test
```

- [ ] **Step 2: 写入 StatefulSet 清单**

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: emqx-test
  namespace: emqx
  labels:
    app: emqx
    cluster: k8s-test
spec:
  replicas: 3
  podManagementPolicy: Parallel
  selector:
    matchLabels:
      app: emqx
      cluster: k8s-test
  serviceName: emqx-test-headless
  template:
    metadata:
      labels:
        app: emqx
        cluster: k8s-test
    spec:
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                labelSelector:
                  matchLabels:
                    app: emqx
                    cluster: k8s-test
                topologyKey: kubernetes.io/hostname
      nodeSelector:
        kubernetes.io/arch: amd64
      containers:
        - name: emqx
          image: emqx/emqx:5.8.4
          env:
            - name: POD_NAME
              valueFrom:
                fieldRef:
                  fieldPath: metadata.name
            - name: POD_IP
              valueFrom:
                fieldRef:
                  fieldPath: status.podIP
            - name: EMQX_NODE__NAME
              value: "emqx@$(POD_IP)"
            - name: EMQX_CLUSTER__DISCOVERY_STRATEGY
              value: dns
            - name: EMQX_CLUSTER__DNS__RECORD_TYPE
              value: a
            - name: EMQX_CLUSTER__DNS__NAME
              value: emqx-test-headless.emqx.svc.cluster.local
            - name: EMQX_RPC__PORT_DISCOVERY
              value: stateless
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
              subPathExpr: k8s-test/data/$(POD_NAME)
      volumes:
        - name: emqx-data
          persistentVolumeClaim:
            claimName: emqx-data
```

- [ ] **Step 3: 验证 YAML 语法**

```bash
kubectl apply --dry-run=client -f components/emqx/emqx-test/statefulset.yaml
```
Expected: `statefulset.apps/emqx-test created (dry run)`

---

### Task 2: 创建 Service

**Files:**
- Create: `components/emqx/emqx-test/service.yaml`

- [ ] **Step 1: 写入 Service 清单（Headless + NodePort 合并）**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: emqx-test-headless
  namespace: emqx
  labels:
    app: emqx
    cluster: k8s-test
spec:
  clusterIP: None
  ports:
    - name: mqtt
      port: 1883
      targetPort: 1883
    - name: dashboard
      port: 18083
      targetPort: 18083
    - name: ekka
      port: 4370
      targetPort: 4370
  selector:
    app: emqx
    cluster: k8s-test
  type: ClusterIP
---
apiVersion: v1
kind: Service
metadata:
  name: emqx-test-nodeport
  namespace: emqx
  labels:
    app: emqx
    cluster: k8s-test
spec:
  ports:
    - name: mqtt
      port: 1883
      targetPort: 1883
      nodePort: 32883
    - name: dashboard
      port: 18083
      targetPort: 18083
      nodePort: 32083
    - name: ws
      port: 8083
      targetPort: 8083
      nodePort: 32884
    - name: wss
      port: 8084
      targetPort: 8084
      nodePort: 32885
  selector:
    app: emqx
    cluster: k8s-test
  type: NodePort
```

- [ ] **Step 2: 验证 YAML 语法**

```bash
kubectl apply --dry-run=client -f components/emqx/emqx-test/service.yaml
```
Expected: `service/emqx-test-headless created (dry run)` + `service/emqx-test-nodeport created (dry run)`

---

### Task 3: 更新 kustomization.yaml

**Files:**
- Modify: `components/emqx/kustomization.yaml`

- [ ] **Step 1: 追加 test 资源引用**

当前文件内容：
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

替换为（追加最后 2 行，保持现有行不变）：
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace/namespace.yaml
  - namespace/pv.yaml
  - namespace/pvc.yaml
  - emqx/service.yaml
  - emqx/statefulset.yaml
  - emqx-test/service.yaml
  - emqx-test/statefulset.yaml
  - ingress.yaml
```

- [ ] **Step 2: 验证 Kustomize 构建**

```bash
kubectl kustomize components/emqx/
```
Expected: 输出全部 8 个资源（原有 6 + 新增 2），无错误。

---

### Task 4: 提交

- [ ] **Step 1: 提交所有变更**

```bash
git add components/emqx/emqx-test/statefulset.yaml \
        components/emqx/emqx-test/service.yaml \
        components/emqx/kustomization.yaml
git commit -m "feat(emqx): add k8s-test cluster with dns discovery and flexible scaling

- New StatefulSet emqx-test (replicas: 3, Parallel pod management)
- DNS-based cluster discovery via emqx-test-headless service
- Per-pod NFS data isolation with subPathExpr (k8s-test/data/<pod>)
- Dedicated NodePort range (32883-32885) separate from prod
- Existing k8s-prod unchanged

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 部署验证

- [ ] **Step 1: 推送并触发 ArgoCD 同步**

```bash
git push
```
ArgoCD 检测到 `components/emqx` 变更后自动同步。

- [ ] **Step 2: 确认 Pod 全部 Running**

```bash
kubectl -n emqx get pods -l cluster=k8s-test -o wide
```
Expected: 3 个 Pod `emqx-test-0`, `emqx-test-1`, `emqx-test-2` 状态 `Running`，分布在不同节点。

- [ ] **Step 3: 确认集群发现成功**

```bash
kubectl -n emqx logs emqx-test-0 | grep -i "cluster"
```
Expected: 日志中出现发现 `emqx-test-1`, `emqx-test-2` 节点的记录。

- [ ] **Step 4: 确认 Dashboard 可访问**

```bash
curl -s <any-node-ip>:32083 | head -20
```
Expected: 返回 EMQX Dashboard HTML。

- [ ] **Step 5: 测试扩缩容（3 → 5）**

```bash
kubectl -n emqx scale statefulset emqx-test --replicas=5
kubectl -n emqx get pods -l cluster=k8s-test -w
```
Expected: 5 个 Pod 全部 Running。

- [ ] **Step 6: 缩回 3 节点**

```bash
kubectl -n emqx scale statefulset emqx-test --replicas=3
```
Expected: 多余 Pod 被终止，保留 3 个。
