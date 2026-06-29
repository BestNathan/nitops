# EMQX k8s-test 集群设计

Date: 2026-06-29 | Status: Approved

## 背景

EMQX 已有 `k8s-prod` 集群（StatefulSet `emqx`, 1 副本），但存在硬编码节点名、缺少集群发现、共用数据路径等问题，无法弹性扩展。本次在**不动 prod 的前提下**新增 `k8s-test` 测试集群，验证集群化部署方案。

## 目标

- 同一 `emqx` 命名空间内新增独立测试集群
- DNS 自动发现，改 `replicas` 即可扩缩（3-10 节点）
- 每节点独立 NFS 数据路径，互不冲突
- 现有 `k8s-prod` 零改动

## 文件变更汇总

| 文件 | 操作 |
|---|---|
| `components/emqx/emqx-test/statefulset.yaml` | **新增** |
| `components/emqx/emqx-test/service.yaml` | **新增** (headless + nodeport) |
| `components/emqx/kustomization.yaml` | **修改** — 追加 test 资源引用 |

现有 `emqx/`, `namespace/`, `ingress.yaml` 不动。

## StatefulSet 设计

### 资源标识

- **name**: `emqx-test`
- **labels**: `app: emqx, cluster: k8s-test`
- **replicas**: 3（初始值，改数字即扩缩）
- **serviceName**: `emqx-test-headless`
- **podManagementPolicy**: `Parallel`（节点并行启动）

### 集群发现

| 环境变量 | 值 |
|---|---|
| `EMQX_NODE__NAME` | `emqx@$(POD_IP)` |
| `EMQX_CLUSTER__DISCOVERY_STRATEGY` | `dns` |
| `EMQX_CLUSTER__DNS__RECORD_TYPE` | `a` |
| `EMQX_CLUSTER__DNS__NAME` | `emqx-test-headless.emqx.svc.cluster.local` |
| `EMQX_RPC__PORT_DISCOVERY` | `stateless` |

`POD_IP` 通过 Downward API (`status.podIP`) 注入，每个 Pod 自动获得唯一节点名。DNS 记录由 Headless Service 提供。

### 数据隔离

```yaml
env:
  - name: POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
# ...
volumeMounts:
  - name: emqx-data
    mountPath: /opt/emqx/data
    subPathExpr: k8s-test/data/$(POD_NAME)
```

结果：
- `emqx-test-0` → `k8s-test/data/emqx-test-0/`
- `emqx-test-1` → `k8s-test/data/emqx-test-1/`
- `emqx-test-2` → `k8s-test/data/emqx-test-2/`

PVC 复用现有 `emqx-data`（NFS），不新建。

### 扩缩容

```sh
# 3 → 5 节点
kubectl -n emqx scale statefulset emqx-test --replicas=5
# 或在 YAML 中改 replicas 后 push，ArgoCD 自动同步
```

新 Pod 启动后通过 DNS 发现自动加入集群。

## Service 设计

### Headless (`emqx-test-headless`)

| 端口 | 用途 |
|---|---|
| 1883 | MQTT |
| 18083 | Dashboard |
| 4370 | Ekka 集群 RPC |

`clusterIP: None`，为每个 Pod 生成 A 记录。

### NodePort (`emqx-test-nodeport`)

| 用途 | 端口 | 对应 prod |
|---|---|---|
| MQTT | 32883 | 31883 |
| Dashboard | 32083 | 31083 |
| WebSocket | 32884 | 30883 |
| WSS | 32885 | 30884 |

Selector 均为 `app: emqx, cluster: k8s-test`，精确匹配测试集群 Pod。

## 不在范围内

- Ingress / SLB 域名：测试集群通过 NodePort 直接访问
- MQTT TLS (8883)：测试集群暂不配置证书，后续需要时追加
- k8s-prod 改造：验证通过后单独进行

## 测试验证

1. `kubectl -n emqx get pods -l cluster=k8s-test` — 3 个 Running
2. `kubectl -n emqx logs emqx-test-0 | grep "cluster_discovery"` — 发现其他 2 个节点
3. `curl <node-ip>:32083` — Dashboard 可访问
4. MQTT 客户端连接 `<node-ip>:32883` — 收发消息正常
5. `kubectl -n emqx scale statefulset emqx-test --replicas=5` — 3→5 自动加入
