# cert-manager、Let's Encrypt DNS-01 与边缘 TLS 生命周期设计

- 日期：2026-11-26
- 状态：设计已确认，尚未实施
- 主需求：[BestNathan/nitops#2](https://github.com/BestNathan/nitops/issues/2)
- 下游协作：[BestNathan/nession#4588](https://github.com/BestNathan/nession/issues/4588)、[BestNathan/nession#4589](https://github.com/BestNathan/nession/issues/4589)

## 1. 目标

为集群建立唯一、可审计、可自动续期的公网通配符证书基础设施：

1. 通过 root ArgoCD Application 部署一次 cert-manager，使用固定版本和经过审核的纯 YAML 清单。
2. 使用 Let's Encrypt Staging/Production 与 Cloudflare DNS-01 签发 `*.bestnathan.top`。
3. 由 cert-manager 独占管理 `slb/bestnathan-top-tls`，SLB 和外部 Router 消费同一证书。
4. LAN 直接访问 SLB 时能够使用普通公共信任库完成 HTTPS 校验。
5. 公网继续由 Cloudflare Universal SSL 面向客户端；实际 origin 使用可信的 Let's Encrypt 证书，支持对批准的 Nession MCP 站点启用 Full(strict)。
6. 续期后自动执行有界、可验证的 nginx 优雅 reload；无效更新保留最后一份有效配置。
7. 公网只开放明确批准的 Nession MCP Host 和精确 `/mcp` 路径，其他 Nession 服务私网默认拒绝。

## 2. 非目标与不变量

- `nhome.local` 完全不变，包括现有手工 AdGuard 记录、HTTP 行为、NFS 自签名证书和路由。
- 不建设内网私有根 CA，不签发新的 `nhome.local` 证书。
- 不修改 Higress 的 TLS 边界；Higress 继续接收 SLB 转发来的 HTTP。
- 不在 Nession 应用中重复部署 cert-manager，不创建第二张通配符 Certificate。
- 不迁移 `*.vol.bestnathan.top` 的 `default.crt/default.key`。
- 不修改 ArgoCD、EMQX、Grafana、MinIO、Portainer、PGAdmin 等无关公网服务的 TLS 或路由。
- 不为了恢复可用性而启用 `insecure_skip_verify`、`curl -k` 或降低 Cloudflare 校验要求。
- 不把通配符私钥写回现有的广泛共享 NFS，不向 Router 同步容器开放 Docker socket。
- 不实施 Nession 应用自身的 Ingress、WSS、Origin、DNS registry 或旧 Origin CA 退役；这些由下游 issue 负责。

## 3. 已验证基线

截至 2026-11-26，通过仓库和只读集群/主机检查确认：

- Kubernetes Server 为 `v1.35.6`，集群包含 amd64 和 arm64 节点。
- `apps/app-of-apps.yaml` 管理 `apps/` 下的子 Application；`apps/slb.yaml` 管理 `components/slb`。
- SLB 是 `slb/nginx-proxy` DaemonSet，`hostNetwork: true`，监听 80/443，向后代理到 Higress HTTP。
- SLB 当前从 NFS 只读挂载：
  - `nhome.crt/key`：`nhome.local`、`*.nhome.local`，10 年自签名。
  - `bestnathan.crt/key`：`bestnathan.top`、`*.bestnathan.top`，10 年自签名。
  - `default.crt/key`：`vol.bestnathan.top`、`*.vol.bestnathan.top`。
- 集群存在 `cert-manager.io/v1` 的 6 个 CRD，但没有 `cert-manager` Namespace、controller Deployment 或 Certificate。
- `default` 和 `gitlab` Namespace 各存在一个历史 GitLab Issuer；二者均未 Ready，曾发生 ACME 出站连接超时。
- `bestnathan.top` 的权威 DNS 为 Cloudflare。
- Router `192.168.2.106` 运行非 GitOps 管理的 Docker `nginx-proxy`。
- Router 上 `argocd.bestnathan.top` 和 Nession 当前均终止 TLS，再以 HTTP 转发到 `192.168.2.86:80`；两者使用自签名证书。
- 当前 Nession Router 配置覆盖 `nession.bestnathan.top` 和 `*.nession.bestnathan.top`，尚未形成 first-level MCP-only 公网边界。
- 当前监控只有 Prometheus/Grafana，没有 Alertmanager、Blackbox Exporter 或既有证书告警规则。

这些事实意味着：不能仅在 SLB 部署证书就宣称公网 origin 已完成；Router 必须被显式纳入生命周期，或进行更大范围的 TLS passthrough 改造。本设计选择影响面更小的受限自动拉取。

## 4. 总体架构

```text
公网 MCP
Client
  → Cloudflare Universal SSL
  → Router 192.168.2.106:443
       Let's Encrypt *.bestnathan.top
       仅 Cloudflare 来源
       仅批准的 MCP Host + 精确 /mcp
       SNI 必须匹配 Host
  → 校验证书的 HTTPS
  → K8s SLB :443
       同一 Let's Encrypt 证书
  → HTTP
  → Higress
  → Nession MCP Service

LAN
Client
  → split DNS
  → K8s SLB :443
       Let's Encrypt *.bestnathan.top
  → HTTP
  → Higress

nhome.local
现有客户端 → 现有 NFS 证书/HTTP 路径 → Higress（不变）
```

### TLS 所有权

| 层 | 证书/配置所有者 | 消费方式 |
|---|---|---|
| Cloudflare 公网边缘 | Cloudflare Universal SSL，由下游站点配置维护 | 不在 nitops 中替换 |
| cert-manager | nitops `apps/cert-manager.yaml` | 独占生成 `slb/bestnathan-top-tls` |
| LAN SLB | nitops `components/slb` | 只读 Secret volume，自动 reload |
| 公网 Router | nitops 版本化的主机同步器与配置 | 受限拉取、原子切换、自动 reload |
| Higress | 现有 nitops 配置 | 继续 HTTP，不申领证书 |
| Nession 应用 | nession#4588/#4589 | 消费基础设施就绪契约，不持有通配符私钥 |

## 5. GitOps 与文件布局

```text
apps/cert-manager.yaml
components/cert-manager/
├── namespace/
│   └── namespace.yaml
├── crds/                       # 每个 CRD 一个文件，共 6 个
├── controller/                 # controller、cainjector、webhook、Service、RBAC
├── issuers/
│   ├── clusterissuer-letsencrypt-staging.yaml
│   └── clusterissuer-letsencrypt-production.yaml
├── certificates/
│   └── certificate-bestnathan-wildcard.yaml
├── secrets/
│   └── sealedsecret-cloudflare-api-token.yaml
└── router-rbac/
    ├── serviceaccount.yaml
    ├── role.yaml
    └── rolebinding.yaml

components/slb/nginx-proxy/
├── daemonset.yaml              # Secret volume + reload 容器
└── configmap-sites.yaml        # 仅 bestnathan server block 改证书路径

router/cert-sync/               # 主机文件版本化，不由 ArgoCD 直接 apply
├── 同步与校验脚本
├── systemd service/timer
└── 安装、恢复和凭据轮换手册

components/observability/prometheus/
├── configmap.yaml              # cert-manager/Blackbox scrape 与 rule_files
├── configmap-rules.yaml        # 证书和探测告警
├── configmap-blackbox.yaml
├── deployment-blackbox.yaml
└── service-blackbox.yaml
```

`apps/cert-manager.yaml` 使用 `source.directory.recurse: true`，不依赖 Helm 或远程 `latest`。所有 Kubernetes 资源继续遵守“一资源一文件”。SLB 现有 Kustomization 只更新已有资源，不改变其归属 Application。

## 6. cert-manager 安装与保护

### 版本与调度

- 固定使用 cert-manager `v1.21.3`；实施时先核对其官方 Kubernetes 1.35 支持矩阵，再固定上游清单校验值和多架构镜像 digest。
- 如果支持矩阵不满足，必须重新评审版本，不能直接改用未审核的最新版本。
- controller、cainjector、webhook 优先调度到具有受验证出网能力的 amd64 节点。
- `quay.io` 镜像通过现有 `192.168.2.106:5004` mirror 获取。
- ACME 与 Cloudflare HTTPS 请求复用仓库已有的 `192.168.2.98:7895` 代理；`.svc`、`.cluster.local` 和集群内部地址必须位于 `NO_PROXY`。
- SLB reload 容器必须使用与主容器相同且固定的多架构 nginx 镜像，避免 amd64/arm64 行为差异。

### CRD 与同步顺序

- 现有 CRD 采用原位升级，禁止删除 CRD 后重建。
- CRD 添加 ArgoCD `Prune=false`、`Delete=false` 保护，并在卸载手册中明确禁止级联删除 CRD。
- 使用 Server-Side Apply 避免大型 CRD annotation 超限。
- sync wave 顺序为：CRD/Namespace → controller/webhook/cainjector/RBAC → Issuer/SealedSecret → Certificate。
- 即使 ArgoCD 已显示 Synced，也必须通过 Kubernetes readiness/condition 回读，不能以 Application 状态替代组件健康检查。
- SLB/Router 的消费变更分成后续提交，确保证书 Ready 之前没有消费者开始挂载或切换。

### ACME 账户密钥

- Staging 与 Production 使用独立的 `privateKeySecretRef`。
- 账户密钥由 cert-manager 在 `cert-manager` Namespace 中生成和持久化，不由 SealedSecret 双写。
- 生成的账户 Secret 不属于 ArgoCD 跟踪资源，不应被 prune；禁止删除整个 Namespace 作为升级手段。
- 如果账户密钥丢失，保留现有 Certificate 和已签发证书，让 cert-manager 使用相同联系邮箱重新注册账户并生成新密钥。不得通过删除 CRD 或生产 Certificate 来“恢复”。
- 集群备份可以加密包含账户 Secret，但不得将解密后的私钥提交到 Git 或写入验收记录。

### 历史资源处理

- 升级前导出 6 个 CRD 和两个 GitLab Issuer。
- 再次确认没有 Certificate 或工作负载引用后，只删除两个持续失败的历史 Issuer，避免新 controller 启动后继续无效重试。
- 保留 CRD、相关历史 Secret 和无关 GitLab 资源，不批量清理 `cert-manager.io` 资源。

## 7. ACME 与 Cloudflare 设计

### 凭据

- 经 Owner 批准复用 `BestNathan/nitops` 的 GitHub Actions Secret `CLOUDFLARE_API_TOKEN`。PR-0 合并评审必须确认其权限仅为 `bestnathan.top` Zone 的 `Zone Read + DNS Edit`；现有凭据是 account-owned API Token，工作流与 Nession 一致，不调用 `/user/tokens/verify` 或其他独立 Token verify，而是通过已认证的 Zone GET 验证唯一的 active Zone 和读取权限，再分页读取 DNS records。该预检只证明实际读取权限，不声称单独验证 Token 元状态；Staging 实际 DNS-01 验证 DNS 写入能力。不得把“能签发”单独表述为已证明最小权限。
- 不复用 Nession DNS 工作流凭据，不要求额外创建 CF 专用 ACME 凭据。
- 新增 PR-0 受保护的 main-only `workflow_dispatch`：使用仓库中的 SealedSecrets **公开加密证书**在 GitHub Runner 内离线生成 SealedSecret，并输出仅含 DNS name/type 的基线。Artifact 保留 1 天；Token 仅注入密封和 DNS inventory 步骤，不进入 checkout、upload、日志、本机文件或 Git。签发后再次运行工作流取得新 DNS inventory，与基线比较。
- Token 通过 PR-0 artifact 中的 SealedSecret 投递到 `cert-manager` Namespace，不在本机读取 GitHub Secret 原值。
- ACME 联系邮箱由运维在 Staging 执行前通过受审核配置提供；未提供时不得启用对应 ClusterIssuer。
- Token、账户私钥、TLS 私钥不得出现在 Git 明文、日志、命令回显或验收证据中。

### DNS-01

- Staging 和 Production ClusterIssuer 均只配置 Cloudflare DNS-01 solver。
- 明确配置并验证公共递归 DNS 自检地址，默认候选为 `1.1.1.1:53`、`8.8.8.8:53`。
- 不使用内网 AdGuard/split DNS 作为 ACME 传播判断依据；如果公共 DNS 自检不可达，预检失败，不跳过传播验证。
- solver 只能维护 `_acme-challenge.bestnathan.top` TXT。
- 不注册任何私有业务 A/AAAA/CNAME，不依据 DNS 是否存在来判断公网授权。

### Staging 到 Production

1. 先部署 Staging Issuer 和位于 `cert-manager` Namespace 的临时测试 Certificate。
2. 验证完整 ACME 流程、TXT 清账、证书链和私钥匹配，不配置信任绕过，不切任何消费者。
3. 测试通过后，在后续提交中删除临时测试 Certificate，并添加唯一的生产 Certificate。
4. ArgoCD prune 只删除测试 Certificate；其生成的测试 Secret 在确认无消费者后显式清理。
5. Staging ClusterIssuer 保留用于后续安全验证；最终常驻配置只保留一张生产 Certificate。

生产 Certificate 定义：

- Namespace：`slb`
- 名称：`bestnathan-wildcard`
- Secret：`bestnathan-top-tls`
- DNS 名称：仅 `*.bestnathan.top`，不包含 apex `bestnathan.top`
- 私钥轮换：`rotationPolicy: Always`
- `tls.crt`：包含面向客户端的完整链
- Secret 唯一写者：cert-manager

## 8. SLB 消费与 reload

### 挂载

- `bestnathan-top-tls` 以只读目录挂载，例如 `/etc/nginx/certs-managed/bestnathan/`。
- 不使用 `subPath`，保证 kubelet 能原子更新 Secret projection。
- `bestnathan` server block 引用该目录下的 `tls.crt/tls.key`。
- `nhome`、`default` 和现有 NFS volume 保持原路径。

### reload 容器

- reload 容器复用主 nginx 镜像、ConfigMap、NFS 证书和 Secret 挂载，使 `nginx -t` 校验完整实际配置。
- 与主容器共享进程命名空间和 PID 文件目录，使 `nginx -s reload` 能定位正在运行的 master。
- 监视 Secret projection 的代次与文件内容变化，先等待连续两次读取稳定，再进行操作。
- 每次 `nginx -t` 和 `nginx -s reload` 都有超时；设置最小 reload 间隔，避免连续抖动。
- reload 后以本节点真实 SNI 回读 leaf serial；serial 未变化时不能报告 reload 成功。

### 失败策略

- 文件不完整、证书/私钥不匹配或 `nginx -t` 失败时，不 reload、不自动重启 Pod。
- 运行中的 nginx master/worker 保留旧配置和旧证书，以有界指数退避重试并产生告警。
- 不删除或修改 cert-manager 拥有的 Secret。
- 如果 Pod 在无效 Secret 存在时被外部重启，新进程必须无法通过配置校验并保持未 Ready，而不是跳过证书校验继续服务。
- 有效的受控续期通过 serial、reload 时间和路由探测共同证明，不能仅依赖 Secret 内容变化。

## 9. Router 受限同步与原子回滚

### Kubernetes 授权

- 在 `slb` Namespace 创建专用 ServiceAccount。
- Role 只允许对名为 `bestnathan-top-tls` 的 Secret 执行 `get`，使用 `resourceNames` 限定；不授予 list、watch、update、delete 或其他 Secret 的权限。
- 首次引导时通过 `kubernetes.io/service-account-token` Secret 为该 ServiceAccount 创建长期 token。该 bootstrap Secret 由受审批的主机安装步骤创建、不由 ArgoCD 跟踪，避免控制器注入的 token/CA 字段产生 Git diff；token 本身只写入 Router 的 root-only kubeconfig。
- Router 使用独立 kubeconfig，校验 API Server CA，禁止 `insecure-skip-tls-verify`。
- 凭据文件仅 root 可读，存放在同步器独立目录，不进入 Git。
- 凭据疑似泄露时创建第二个 token Secret、更新 Router kubeconfig、验证后删除旧 token Secret；凭据轮换不改变证书所有权。

### 每次同步

PR-4 的 observe-only 模式只执行下述第 1～5 步，不更新 `current`、不执行 Nginx reload。只有 PR-5 已将 Nession 证书路径切换到 `current` 后，才激活完整切换流程：

1. 每 5 分钟通过 Kubernetes API 获取指定 Secret。
2. 将候选写入新的版本目录，不覆盖当前版本。
3. 验证证书与私钥匹配、SAN 为 `*.bestnathan.top`、有效期有效、完整链可被系统公共信任库验证。
4. 检查 serial 和版本状态，避免同一版本反复切换或异常回退震荡。
5. 证书文件只读，私钥权限为 `0600`。
6. 原子更新 `current` 符号链接，再执行主机上的 `docker exec nginx-proxy nginx -t`。
7. 校验失败立即恢复上一符号链接；运行中的 nginx 不 reload。
8. 校验成功后执行 `nginx -s reload`，再以实际 MCP SNI 从本机回读证书。
9. serial 或链不符合候选时恢复上一版本并 reload；成功后保留当前和上一版本，清理更旧副本。
10. 连续失败只保留 last-known-good 并报警，不删除最后一份有效证书，不修改其他站点证书目录。

主机同步脚本和 systemd unit 以 root 所有、不可被普通用户修改的方式安装。首次安装和配置切换需要审批；之后每次续期不再有人工复制步骤。

## 10. Nession 公网边缘策略

### 允许集合

只有下游 issue 确认工作负载和路由已存在时，才可激活：

- `mcp-nession.bestnathan.top`
- `staging-mcp-nession.bestnathan.top`

公网只代理精确 `= /mcp`。`/`、`/mcp/`、`/api` 和其他路径均不继承 MCP 放行规则。

### 默认拒绝

- 对 `nession.bestnathan.top`、所有其他 first-level `*-nession.bestnathan.top` 和旧多级 `*.nession.bestnathan.top` 配置显式拒绝。
- 精确 MCP server name 的优先级高于 Nession 拒绝正则；其他未知 Nession 名称不能落到通配业务路由。
- MCP location 校验 `$ssl_server_name` 与 `$host` 一致，阻止“公网 MCP SNI + 私网 Host”或反向组合。
- 公网 MCP server block 只接受 Cloudflare 来源；LAN 访问使用 split DNS 直达 SLB，不需要把 Router 当作 LAN 入口。
- Router 重建转发身份头，不信任客户端传入的 `X-Forwarded-*`。只有来源确属 Cloudflare 时才使用其客户端 IP 信息。
- Router 到 SLB 使用 HTTPS，启用 `proxy_ssl_verify`、`proxy_ssl_server_name`、系统公共 CA 信任库，并按请求 Host 设置上游 SNI。
- Cloudflare Full(strict) 由下游站点配置按 MCP hostname 启用；不在 nitops 中盲目切换整个 Zone。

## 11. 监控与告警

### 指标与探测

- Prometheus 增加 cert-manager controller metrics scrape。
- 在 observability 中新增固定版本的 Blackbox Exporter Deployment/Service 和配置。
- LAN probe 固定连接 SLB 节点 IP，并显式设置 Host/SNI；TLS 校验必须使用系统公共信任库。
- LAN probe 在 Nession 激活前可使用稳定的 `argocd.bestnathan.top` SNI 做证书验证，允许后端返回正常路由结果或 404；任何 TLS 失败仍判定失败。
- Public probe 只针对已激活的 MCP hostname。未认证请求返回 200、400、401、403 或 405 可证明入口可达；Cloudflare 521/526、5xx、超时和 TLS 失败均判定异常。
- Public probe 观察到的是 Cloudflare 客户端证书，不把它误记为 origin serial；origin serial 由 Router 本地回读记录证明。Cloudflare Full(strict) 失败通过 Public probe 失败体现。
- Router 同步器记录 sanitized serial、有效期、reload 和本地回读结果，不记录凭据或私钥。

### 告警规则

- Certificate 非 Ready 超过 5 分钟。
- cert-manager Secret 证书距离到期不足 14 天。
- LAN 实际服务证书距离到期不足 14 天。
- cert-manager Certificate 指标消失。
- cert-manager controller 同步错误持续增加。
- LAN TLS probe 连续失败。
- Public MCP probe 连续失败。

当前没有 Alertmanager 或外发联系人，因此本期告警验收以 Prometheus/Grafana 中可见且可触发的 Firing 状态为准。不得将告警规则描述为已经具备短信、邮件或 IM 消息推送；外发渠道是独立后续能力。

## 12. 分批上线与门禁

### PR-1：控制器与 Staging

- 备份并原位升级 CRD，部署固定版本 controller、webhook、cainjector、SealedSecret 和 Staging Issuer。
- 清理已确认无引用的历史 Issuer。
- 验证镜像、代理、公共 DNS 自检、ACME Staging、TXT 清账和 Webhook Ready。
- 不修改任何消费者。

### PR-2：Production Certificate 与控制器监控

- 移除临时 Staging Certificate，创建生产 Certificate。
- 添加 cert-manager metrics，以及只针对 controller/Certificate 的 Prometheus rules；此时尚不启用 LAN/Public Blackbox 告警，避免旧证书尚未切换时产生预期内误报。
- 等待 ClusterIssuer/Certificate Ready，验证 SAN、完整链、私钥匹配和有效期。
- 此时 SLB/Router 尚未切换，不能宣称流量已使用可信证书。

### PR-3：SLB 消费

- 添加 Secret volume、配置路径和 reload 容器。
- DaemonSet 逐节点更新，逐节点回读真实 SNI 的 serial 和信任链。
- 所有节点确认使用新证书后，再启用 Blackbox LAN probe、实际服务证书到期告警和 LAN TLS 失败告警。
- 检查所有节点收敛，且 `nhome.local`、`default` 证书 serial 和既有路由不变。

### PR-4：Router 同步器

- 安装受限凭据和 systemd timer，以 observe-only 模式运行候选下载与验证，不更新 `current`，不修改或 reload Nginx。
- 验证候选目录、权限、回滚副本策略和其他站点证书文件/配置未变化。
- Router 正式自动 reload 在 PR-5 证书路径切换后启用。

### PR-5：Nession 边缘策略

- 与 nession#4588/#4589 联调，先部署私网 Host 拒绝、SNI/Host 一致性和 MCP 白名单。
- 将 Nession server block 的证书路径切换到 Router `current`，再启用同步器完整切换与自动 reload。
- Router→SLB 切换为校验证书的 HTTPS。
- 只有负向探测通过后才激活对应公网 MCP；未激活的环境保持不公开。
- 对已激活的 MCP 启用 Blackbox Public probe 和 Public MCP 失败告警。

### PR-6：受控续期验收

- 使用 `cmctl renew` 触发一次经授权的受控续期。
- 记录续期前后 serial，不记录私钥。
- 证明所有 SLB 节点与 Router 在有界时间内自动收敛，路由保持可用，且没有手工复制证书。
- Nession 旧 Origin CA 机制只能在对应仓库确认新链路回读后退役。

## 13. 测试设计

### 静态和确定性测试

- 渲染 ArgoCD Application，校验 Kubernetes schema、CRD 顺序、sync wave 和 Secret 引用。
- 对 SLB/Router 配置执行 `nginx -t`。
- 执行 `promtool check config/rules` 和 Blackbox 配置检查。
- Router 脚本执行 `sh -n` 和 ShellCheck。
- 使用一次性测试目录与测试证书覆盖：有效更新、过期、私钥不匹配、错误 SAN、断链、API 超时、重复版本和 reload 失败。
- 无效候选必须保留 last-known-good；不通过手工篡改生产 cert-manager Secret 来做故障测试。
- 检查 Git 中没有明文 Token/私钥，且没有 SealedSecret 或其他控制器竞争写 `bestnathan-top-tls`。

### 真实正向探测

- 不使用 `curl -k`，以系统信任库验证 LAN HTTPS。
- 公网 MCP 正常认证请求成功，错误 Token 仍被拒绝。
- HTTPS/WSS、SSE、WebSocket upgrade 和 `/mcp` 协议行为由 Nession 联调验收。
- 所有实际 LAN/origin TLS 端点回读的 serial 与 cert-manager Secret 一致。

### 真实负向探测

- Server、Agent、UI、Staging 私网 Host 即使手工指定公网 IP 也被拒绝。
- 公网 MCP SNI 搭配私网 Host，以及私网 SNI 搭配 MCP Host，均被拒绝。
- `/`、`/mcp/`、`/api` 等路径不能绕过精确 `/mcp` 限制。
- 非 Cloudflare 来源直连 Router 公网 MCP 被拒绝。
- 伪造 `X-Forwarded-*` 不改变后端身份判断。
- 旧多级 Nession 域名不能通过通配符回退获得公网访问。
- ArgoCD 等既有公网服务及全部 `nhome.local` 行为保持不变。

## 14. 回滚与恢复

- **SLB**：通过 Git 恢复保留的 NFS `bestnathan.crt/key` 路径并滚动更新。保留 cert-manager、CRD 和 Secret，不级联卸载。
- **Router**：优先恢复上一版可信证书和配置；同步器故障不自动删除当前证书。
- **整体退出公网改造**：先关闭 MCP 公网发布，再恢复旧 Router 配置。不得恢复自签名 origin 后同时宣称 Full(strict)，也不得通过关闭校验维持表面可用。
- **cert-manager 升级失败**：保持旧 controller 和现有证书服务，按固定版本回滚 controller 清单；不得删除 CRD、Namespace、Certificate 或账户 Secret。
- **ACME 账户密钥丢失**：保留现有证书，按第 6 节重新注册账户；既有证书在自身有效期内仍可继续使用。
- **Nession 应用回滚**：由下游仓库处理，不要求回滚或删除共享 cert-manager Secret。

## 15. 验收证据

最终验收记录必须包含：

1. 精确 Git commit 和固定上游版本/digest。
2. 清单渲染、schema、nginx、Prometheus、Blackbox 和脚本检查结果。
3. ArgoCD Application Synced/Healthy，以及独立的 CRD Established、Pod Ready、Webhook Ready 回读。
4. Staging/Production ClusterIssuer Ready、Certificate Ready。
5. SAN、完整链、serial、有效期和私钥匹配的 sanitized 证据。
6. ACME 仅使用 `_acme-challenge.bestnathan.top` TXT、无私有业务 DNS 记录创建的 readback。
7. 每个 SLB 节点和 Router 的实际 SNI/Host serial、reload 时间与路由结果。
8. 受控续期后自动收敛的证据，以及无效候选保留 last-known-good 的测试证据。
9. LAN/Public 正向探测和 Host/SNI/path/source/header 负向探测结果。
10. 无关公网服务、`nhome.local` 和 `default` 证书保持不变的对比结果。

任何缺少真实运行证据的条目标记为 Pending。清单存在、PR 合并、ArgoCD Synced 或 Secret Ready 均不能单独证明端到端验收完成；证据中不得包含 Token、私钥或完整 Secret。
