# EMQX GitOps 纳管 + Nginx 代理设计

**日期:** 2026-06-26
**状态:** 已确认

## 背景

EMQX 5.8.4 已在集群中运行（kubectl 直接部署），需要：
1. 纳入 GitOps 仓库管理（ArgoCD）
2. slb nginx-proxy 添加 Dashboard 和 MQTT WebSocket 代理

## 架构

```
外网客户端 ──→ Router (192.168.2.106:80/443)
                  │
                  ▼
          slb nginx-proxy (hostNetwork, DaemonSet)
           │                    │
           │ :80/:443           │ :80/:443
           ▼                    ▼
    emqx-dashboard.*      mqtt-ws.* /mqtt
    (标准 HTTP 反向代理)    (WebSocket 升级代理)
           │                    │
           ▼                    ▼
   emqx-nodeport:18083   emqx-nodeport:8083
           ╲                  ╱
            ▼                ▼
        EMQX StatefulSet (emqx:5.8.4)
              │
              ▼
          NFS (10Gi, /opt/emqx/data)
```

## 仓库结构

```
components/emqx/                  # 新增
├── kustomization.yaml
├── namespace/
│   ├── namespace.yaml            # emqx namespace
│   ├── pv.yaml                   # emqx-nfs, 10Gi, RWX, 192.168.2.105
│   └── pvc.yaml                  # emqx-data → emqx-nfs
├── emqx/
│   ├── statefulset.yaml          # 1 副本, emqx/emqx:5.8.4
│   └── service.yaml              # emqx-headless (ClusterIP None) + emqx-nodeport (NodePort)
└── ingress.yaml                  # Higress: /mqtt → emqx-nodeport:8083
```

## Nginx 代理规则（slb configmap-sites.yaml）

新增两个 server group：

### Dashboard — 标准 HTTP 反向代理

| 属性 | 值 |
|------|-----|
| 域名 | `emqx.nhome.local`, `emqx.bestnathan.top` |
| 上游 | `emqx-nodeport.emqx.svc.cluster.local:18083` |
| 端口 | 80 + 443 (SSL) |
| WebSocket | 需要（Dashboard 实时更新依赖） |
| 证书 | `certs/emqx.crt` + `certs/emqx.key`（NFS `emqx/certs/`） |

### MQTT WebSocket — WebSocket 升级代理

| 属性 | 值 |
|------|-----|
| 域名 | `mqtt.nhome.local`, `mqtt.bestnathan.top` |
| 上游 | `emqx-nodeport.emqx.svc.cluster.local:8083` |
| 端口 | 80 + 443 (SSL) |
| WebSocket | 需要（MQTT over WS） |
| 证书 | `certs/mqtt.crt` + `certs/mqtt.key`（NFS `emqx/certs/`） |

## TLS 证书

- 证书存储在 NFS: `emqx/certs/` 子路径
- slb nginx-proxy 通过 `slb-nfs` PVC 以 `subPath: emqx/certs` 只读挂载，路径为 `/etc/nginx/certs/by-app/emqx/`
- 初始化用自签名证书 bootstrap，后续替换为 CA 签名证书

## DaemonSet 变更

- 新增 volume mount: `subPath: emqx/certs` → `/etc/nginx/certs/by-app/emqx`
- 端口不变（80/443 已存在）；不需要暴露 1883/8883（原生 MQTT 不走 nginx）

## ArgoCD Application

新增 `apps/emqx.yaml`：
- path: `components/emqx`
- namespace: `emqx`
- syncPolicy: automated (prune + selfHeal)

## 需要确认的风险

1. **EMQX 已在集群中运行** — ArgoCD 接管时，资源 diff 应显示无变更（manifest 与现有资源一致）。如果 StatefulSet 的 podTemplate 有任何差异，会触发滚动重启 → EMQX 短暂中断。
2. **NodePort 端口冲突** — 现有 NodePort service 使用 31883/31083/30883/30884，复制到仓库 manifest 时需精确匹配。
3. **域名是否可用** — `emqx.bestnathan.top` 和 `mqtt.bestnathan.top` 需要在路由器/公网 DNS 解析到集群节点 IP（192.168.2.106:80/443）。
