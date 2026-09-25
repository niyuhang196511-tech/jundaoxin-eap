# 生产部署 Runbook（v0.4+）

> 适用版本：M7/M8 加固后（Alembic 迁移、Key 哈希/加密、任务恢复、/metrics）。
> 部署拓扑：docker-compose（postgres + redis + milvus + eap + console）。

## 1. 上线前必改清单

| 项 | 环境变量 | 默认（仅开发） | 生产要求 |
|---|---|---|---|
| 数据库 | `EAP_DB_URL` | sqlite | `postgresql+psycopg://user:pass@postgres/eap` |
| 监听 | `EAP_HOST` | 127.0.0.1 | `0.0.0.0`（容器内）+ 反代 TLS |
| CORS | `EAP_CORS_ORIGINS` | 空（同源） | 控制台来源，如 `https://console.corp.cn` |
| 开发密钥 | `EAP_DEV_API_KEY` | dev-key-1 | 换强随机值（启动日志会警告默认值） |
| 会话签名 | `EAP_SESSION_SECRET` | change-me | `openssl rand -hex 32` |
| 秘密加密 | `EAP_SECRET_KEY` | 未配置=明文 | `openssl rand -hex 32`（**生成后不可更换**，否则库内密文无法解密） |
| OIDC | `EAP_OIDC_ISSUER` 等 | 未配置 | 对接企业 IdP（可选，JWT 资源服务器模式） |
| 队列 | `EAP_REDIS_URL` | 未配置=进程内 | 配置 Redis（多副本必需） |
| Milvus | `EAP_MILVUS_URI` | 未配置=本地余弦 | 生产知识库规模建议配置 |

启动日志出现 `仍为开发默认值` / `明文存储` 字样即表示有项未改。

## 2. 数据库迁移

```bash
# 应用启动时自动执行（空库 baseline / 存量 stamp / 已纳管 upgrade）。
# 手动操作（CI/CD 中推荐显式执行）：
cd eap && uv run alembic upgrade head

# 新增模型变更的标准流程：
# 1) 改 src/eap/models/__init__.py
# 2) uv run alembic revision --autogenerate -m "描述"  （检查生成脚本，清理漂移噪音）
# 3) 本地空库 + 存量库各验证一次再提交
```

存量库首次接入 Alembic：`alembic stamp head`（对齐版本号，不改动数据）。

### 2.1 RLS 角色收敛（M51-C，`EAP_DB_APP_ROLE`）

背景：应用以 postgres 初始用户（`POSTGRES_USER=eap`，建表 owner/超级用户）连接时，
PostgreSQL 对 owner/超级用户默认豁免 RLS——11 张表的 `tenant_isolation` 策略建而不评。
迁移 `c7e9b2d4f6a8` 创建 NOLOGIN 应用角色 `eap_app` 并授应用级 DML（全量 public 表
SELECT/INSERT/UPDATE/DELETE + 序列 + 默认权限；租户隔离由 RLS 策略承担，不做表级裁剪）。
`EAP_DB_APP_ROLE=eap_app` 时 **JWT 租户通道**在请求事务内 `SET LOCAL ROLE eap_app`，
RLS 真实生效；API Key/embed/worker/触发器与 webhook 引擎保持平台身份（owner 豁免）不变。

启用步骤：

```bash
# 1) 先跑迁移（建角色 + 授权；compose 形态由 eap-migrate 服务自动执行）
cd eap && uv run alembic upgrade head

# 2) .env.prod 设置（deploy/.env.example.prod 已有条目）
#    EAP_DB_APP_ROLE=eap_app

# 3) 重启后端使配置生效
docker compose -f deploy/docker-compose.prod.yml up -d eap worker

# 4) 验证：以 eap_app 身份跨租户 SELECT 应为空（fail-closed），设 GUC 后只见本租户行
docker exec -it eap-postgres-1 psql -U eap -d eap -c "
  BEGIN;
  SET LOCAL ROLE eap_app;
  SELECT count(*) FROM policies;                       -- 仅 tenant_id=0 平台默认行
  SELECT set_config('eap.tenant_id', '1', true);
  SELECT count(*) FROM policies WHERE tenant_id = 1;   -- 只见租户 1 的行
  ROLLBACK;"
# 应用侧冒烟：JWT（租户 A）GET /api/v1/policies 不见租户 B 的行；API Key 通道全见。
```

回滚：`EAP_DB_APP_ROLE=`（置空）+ 重启即回退 owner 豁免路径，无需回退迁移；
彻底移除角色走 `alembic downgrade -1`（REVOKE 成员关系 → DROP OWNED → DROP ROLE）。

托管 RDS/Cloud SQL 注意事项：迁移以登录主用户执行时会自动 `CREATE ROLE eap_app`
并 `GRANT eap_app TO <登录用户>`（非超级用户 `SET ROLE` 需要成员身份；该句尽力而为，
失败仅 NOTICE）。若组织策略禁止应用迁移建角色，请 DBA 预先手工执行：
`CREATE ROLE eap_app NOLOGIN;` + 迁移同款 GRANT（见 `eap/migrations/versions/c7e9b2d4f6a8_rls_app_role.py`）
+ `GRANT eap_app TO <应用登录用户>;`——迁移幂等，角色已存在即跳过创建。

既有语义如实说明（commit 后 RLS 约束回退）：`SET LOCAL ROLE` 与
`set_config('eap.tenant_id', ..., true)` 同为**事务级**——请求内一旦 `commit`，
角色自动恢复登录角色（owner）、GUC 在池化连接上残留为 `''`；同一请求 commit 之后的
后续查询**不再受 RLS 约束**（与 M49-C 登记的 GUC 丢失语义一致，策略谓词已 NULLIF
加固为 fail-closed 不抛错）。现有端点的 commit 后落库（审计/用量）走独立会话的平台
身份路径，不受影响；新端点若在 JWT 通道 commit 后仍需租户隔离查询，应在新事务内
重设 GUC 或拆分为独立请求。

## 3. 备份

### PostgreSQL（全量数据：任务/知识库元数据/chunk/用量）

```bash
# 每日全量（crontab 示例，保留 14 天）
0 2 * * * docker exec eap-postgres-1 pg_dump -U eap -Fc eap > /backup/eap-$(date +\%F).dump
```

### 对象数据（Milvus 向量 + MinIO）

```bash
# Milvus standalone：备份其数据目录卷（etcd+minio）
tar czf /backup/milvus-$(date +\%F).tgz /var/lib/docker/volumes/eap_milvus_data
```

### Redis

仅队列/限流瞬态数据，**可不备份**（任务丢失由 PENDING 恢复 + XAUTOCLAIM 接管兜底）。

## 4. 恢复

```bash
# 1) 停 eap 应用（保留 postgres）
docker compose stop eap

# 2) 恢复库
cat /backup/eap-2026-09-16.dump | docker exec -i eap-postgres-1 pg_restore -U eap -d eap --clean --if-exists

# 3) 启动应用（自动 alembic upgrade head 补齐版本差异）
docker compose start eap

# 4) Milvus：替换数据卷后重启 milvus 容器
```

**验证恢复成功**：
- `GET /health` → agents 全部 `started`
- `GET /api/v1/models` 返回注册表
- 任一知识库 `POST /api/v1/kb/{name}/retrieve` 命中（向量/图谱路可用）
- `GET /metrics` 有 `eap_requests_total` 序列

## 5. 健康与告警

- 存活：`GET /health`（agents 状态含在内）
- 指标：`GET /metrics`（Prometheus 格式）。建议告警规则：
  - `rate(eap_requests_total{status=~"5.."}[5m]) > 0.05`（5xx 突增）
  - `increase(eap_tasks_total{state="FAILED"}[15m]) > 0`（任务失败）
  - `eap_agent_invocations_total{status="error"}` 增速异常

## 6. 常见故障

| 症状 | 处置 |
|---|---|
| 启动报 `duplicate column` | 存量库未 stamp：`alembic stamp head` 后重启 |
| `enc1:` 密文解密失败 | `EAP_SECRET_KEY` 与加密时不一致——密钥不可更换，找回原值 |
| 登录后 401 | IdP token 的 `tenant_id/tid` 声明未映射到租户：核对 claims 与 tenants 表 |
| 定时任务不触发 | `task_schedules.next_run_at` 是否过期 + `enabled`；多副本已被抢占属正常 |
| 任务长期 PENDING | 单副本形态确认 worker 存活（`/health`）；重启后启动恢复会自动重投 |

## 7. 发布与晋升（M32）

发布流水线 [.github/workflows/release.yml](../.github/workflows/release.yml)，两条路径：

- **发布**：push `v*` tag（如 `v0.8.0`）→ 自动构建 [eap/Dockerfile](../eap/Dockerfile) 并推送
  `ghcr.io/<owner>/<repo>/eap:<semver>`（去 `v` 前缀）与 `:latest`，同时生成 Release notes。
- **晋升**：Actions → Release → `promote`（workflow_dispatch），输入 `image_tag`（如 `0.8.0`）与
  `environment`（`dev|staging|prod`）→ 将已发布镜像重打 `:<environment>` 标签推送；镜像不可变，
  环境间只挪标签不重构建。

环境保护建议：仓库 Settings → Environments → `production` 配置必需审批人（required reviewers）
与可部署分支限制；部署侧按 `:<environment>` 标签拉取。控制台镜像（eap/frontend）目前仅 CI 构建校验，
自动发布待后续补齐。

## 8. 恢复演练与 HA 部署（M33）

### 8.1 一键备份与恢复演练（[scripts/drill_restore.py](../scripts/drill_restore.py)）

```bash
# 备份（按 EAP_DB_URL 形态自动选择：SQLite 在线 backup API / PostgreSQL pg_dump -Fc）
uv run python scripts/drill_restore.py backup --out /backup

# 演练（--latest 取最近一份）：临时目录还原 → alembic upgrade head → 冒烟断言 → 报告 → 清理
uv run python scripts/drill_restore.py drill --latest /backup

# PostgreSQL 演练：pg_restore 到独立演练库（--clean --if-exists，不影响生产库）
EAP_DB_URL=postgresql+psycopg://eap:pass@pg:5432/eap \
  uv run python scripts/drill_restore.py drill --latest /backup --restore-db drill_eap
```

- 退出码全绿 0 / 失败非 0，每步打印 `[步骤 N]` 行可直接进 CI；`--keep` 保留现场，`--report-json` 出结构化报告。
- 演练只在临时副本/演练库上操作，绝不回写源库；备份文件非空、能打开、可迁移、关键表可计数、`alembic_version` 单头。
- 月度演练建议（crontab，演练不过 = 备份等于没备）：

```bash
0 5 1 * * cd /opt/eap && uv run python scripts/drill_restore.py drill --latest /backup --restore-db drill_eap >> /var/log/eap-drill.log 2>&1
```

### 8.2 双实例 HA（[deploy/docker-compose.ha.yml](../deploy/docker-compose.ha.yml)）

启动顺序由 depends_on + healthcheck 保证：postgres/redis 健康 → eap-api-1/2（`EAP_WORKER_COUNT=0`，
不内嵌 worker）→ eap-worker（`python -m eap.worker`）→ nginx。镜像用晋升标签 `:prod`（§7）：
`EAP_IMAGE=… docker compose -f deploy/docker-compose.ha.yml up -d`。

- nginx（[deploy/nginx.conf](../deploy/nginx.conf)）：upstream `least_conn` 分流、`/health` 直通、
  SSE `proxy_buffering off`；故障实例由 `max_fails` 自动摘除。
- 晋升切换要点：换镜像标签逐实例滚动重启（一次一个）；迁移由启动时 `alembic upgrade head` 补齐，
  先放行一个实例验证 `/health` 与 `/metrics` 再升第二个；回滚即换回旧 tag 逐实例重启。
- k8s 等价样例 [deploy/k8s-ha.yaml](../deploy/k8s-ha.yaml)（Deployment 2 副本 + `/health` 探针 +
  独立 worker Deployment），生产需按环境补 Ingress TLS/HPA/PDB 并外置 PostgreSQL/Redis。

### 8.3 告警接入（[deploy/prometheus-alerts.yml](../deploy/prometheus-alerts.yml)）

指标名取自 `/metrics` 实际导出（`eap_requests_total` / `eap_request_latency_seconds_sum` /
`eap_tasks_total` / `eap_circuit_state` / `eap_gateway_inflight` 等）。Prometheus 端 scrape 两个
API 实例（`job="eap"`），`rule_files` 挂载该文件；规则含：实例宕机（M50-D 起副本数无关口径：
按实例 `up{job="eap"} == 0` warning + 全挂 `sum(up{job="eap"}) == 0 or absent(up{job="eap"})`
critical，单/双实例部署均适用）、5xx 占比 > 5%、平均延迟（histogram 桶导出前为均值代理）、
任务失败增速、熔断跳闸、网关在途。


## 9. 生产部署栈与运营资产（M47-C）

### 9.1 一键生产栈（[deploy/docker-compose.prod.yml](../deploy/docker-compose.prod.yml)）

与 §8.2 HA 样例的定位区别：本栈面向**首次交付/单机生产**——ghcr 镜像（不本地 build）、
PG/Redis 不暴露宿主端口、启动前独立 `eap-migrate` 服务执行 Alembic、控制台镜像由
release.yml `release-console` job 同 tag 发布。可选 Milvus 向量栈走 `--profile milvus`。

```bash
cp deploy/.env.example.prod .env.prod    # 逐项填写，[*] 为必改
EAP_VERSION=v1.0.0 docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod up -d
```

### 9.2 启动 fail-fast（`EAP_STRICT_CONFIG=1`）

prod 栈默认开启：存在开发默认密钥（dev api key / session secret）、技能签名或加密密钥
未配置时，进程以退出码 78（EX_CONFIG）拒绝启动——判定清单与警告同源
（`eap/__main__.py::insecure_config_problems`）。开发环境勿开此开关。

### 9.3 备份纳入媒体目录（消除 DB 与媒体版本漂移）

`drill_restore.py backup --media-dir $EAP_MEDIA_DIR`：库备份之外把媒体目录打包为
`eap-media-<时间戳>.tar.gz` + sha256 清单（sidecar）。演练侧
`drill --media <包路径>` 增加完整性校验步骤（解包后逐文件复核哈希）。prod 栈的
`backup` 服务已带 `--media-dir`（媒体卷只读挂载）。注意 `--media-dir` 不要与 `--out`
指向同一目录。

### 9.4 审计日志治理（M47-B）

- 导出：`GET /api/v1/audit/export?format=csv|json&...过滤条件`（admin；控制台审计页
  「导出 CSV」按钮所见即所导）；行数硬上限 `EAP_AUDIT_EXPORT_LIMIT`（默认 50000，防拖库）。
- 保留期：`EAP_AUDIT_RETENTION_DAYS`（默认 365，0=永久）+ `POST /api/v1/audit/purge`
  （admin，可单次覆盖天数）显式触发；动作落审计仅条数。如需自动化，调度器直接调
  `observability/audit.purge_expired` 即可。

### 9.5 可观测栈样例（deploy/observability/）

Prometheus（M49-D 补入：抓 eap `/metrics`，加载 [deploy/prometheus.yml](../deploy/prometheus.yml)
+ [deploy/prometheus-alerts.yml](../deploy/prometheus-alerts.yml) 规则并推 Alertmanager）+
Loki + Promtail（采 Docker 容器日志，EAP 结构化日志的 level 提炼为标签、trace_id 走
structured metadata——M54-C）+
Grafana（预置数据源与 `grafana-dashboard-eap.json` 仪表盘：请求速率/p95 延迟/Agent 调用/
Token 用量/队列深度/熔断状态/沙箱违规/网关拒绝）+ Alertmanager 路由样例。

```bash
# 前提：后端栈已起（observability 栈以 external network 接入其内网抓 eap:8300，见下）
docker compose -f deploy/docker-compose.prod.yml --env-file .env.prod up -d
docker compose -f deploy/observability/docker-compose.observability.yml up -d
# Grafana http://localhost:3300 → Dashboards → EAP 平台运行概览
```

#### 告警链路联调（M49-D：P2 #23 Loki 生产调余 + #24 alertmanager 联调）

M49-D 补齐 #24 核心缺口：此前仓库没有 Prometheus 主配置，`prometheus-alerts.yml` 的
10 条规则从未被加载。现在 `deploy/prometheus.yml`（scrape/rule_files/alerting）由
observability 栈新增的 prometheus 服务（prom/prometheus:v2.54.1，与 alertmanager
v0.27.0 同代）挂载；Loki 从镜像内置 `local-config.yaml` 换为自定义
[deploy/observability/loki-config.yml](../deploy/observability/loki-config.yml)。

**网络前提**（跨 compose 项目，诚实说明）：eap 服务只在后端栈内网 `expose` 8300
（不映射宿主），prometheus 以 external network 接入——默认 `eap-prod_default`
（生产栈 `name: eap-prod` 的默认网络，须先起生产栈）；开发栈用
`EAP_BACKEND_NETWORK=eap_default` 覆盖。external 网络必须先存在，否则本栈 `up`
直接报错；仅想校验观测栈本身（无后端可抓）时先 `docker network create
eap-prod_default` 建同名空网络，prometheus 能起但 eap target 为 down，属预期。

**联调步骤**：

1. 静态 + 深度校验（`--docker` 用容器内 promtool / amtool / `loki -verify-config`
   做深度校验，镜像拉取失败打印 SKIP 不计失败）：

   ```bash
   uv run --with pyyaml python scripts/check_observability.py
   uv run --with pyyaml python scripts/check_observability.py --docker
   ```

2. 采集验证：起栈后开 Prometheus `http://localhost:9090/targets`——`job=eap`
   端点应为 UP（down 则先查 external 网络前提）；`/rules` 应见 3 组 / 10 条规则。

3. 触发测试告警（两种方式，均已在 v2.54.1 + v0.27.0 实测走通）：
   - 临时规则端到端：向 `prometheus-alerts.yml` 临时追加
     `- alert: SmokeTestAlert` / `expr: vector(1)` / `labels: {severity: critical}`，
     `curl -X POST http://localhost:9090/-/reload` 热加载（`--web.enable-lifecycle`
     已开启），`/alerts` 页见其 firing 后到 Alertmanager 确认；**验证完删除临时规则
     并再次 reload**。
   - amtool 直推 Alertmanager（跳过 Prometheus，只验路由/UI；容器需加入观测栈网络，
     默认项目名 observability → 网络 observability_default）：

     ```bash
     docker run --rm --network observability_default --entrypoint amtool \
       prom/alertmanager:v0.27.0 --alertmanager.url=http://alertmanager:9093 \
       alert add alertname=AmtoolSmoke severity=warning
     ```

   单实例口径（M50-D 已修正）：旧版 `EapInstanceDown` 用双实例 HA 口径
   `sum(up{job="eap"}) < 2`，单机形态（sum(up)=1）会常燃。现改为副本数无关的
   按实例口径 `up{job="eap"} == 0`（warning），`EapAllInstancesDown` 为
   `sum(up{job="eap"}) == 0 or absent(up{job="eap"})`（critical）——单实例部署
   仅在 eap 真正不可达时触发。触发测试可临时停掉后端栈 eap 容器观察两条规则
   先后进入 firing（1m/2m `for` 窗口后），验完 `docker compose ... start` 恢复。
   注意 `deploy/prometheus.yml` scrape 段注释仍描述旧口径（该文件不在 M50-D
   改动范围），以 `deploy/prometheus-alerts.yml` 规则本体为准。

4. Alertmanager 看路由：UI `http://localhost:9093`——critical 应命中 `oncall`、
   warning 命中 `ops-channel`（receiver 为占位，UI 可见但无外发动作）。不开 UI 可
   用 amtool 干跑路由（输出即命中的 receiver 名；Git Bash 需 `MSYS_NO_PATHCONV=1`
   防容器内路径被转换）：

   ```bash
   MSYS_NO_PATHCONV=1 docker run --rm \
     -v ${PWD}/deploy/observability/alertmanager.yml:/tmp/am.yml:ro \
     --entrypoint amtool prom/alertmanager:v0.27.0 \
     config routes test --config.file=/tmp/am.yml --verify.receivers=oncall \
     alertname=EapAllInstancesDown severity=critical
   ```

5. 占位 receiver 换真实渠道：`deploy/observability/alertmanager.yml` 三个 receiver
   均为占位（告警到达后止步，不通知）。邮件（SMTP）/企业微信应用消息/飞书·钉钉·
   企微群机器人的配置样例见该文件 receivers 段注释——群机器人消息体与 Alertmanager
   webhook 载荷格式不匹配（且有关键词/加签校验），**必须经格式转换中转服务**（如
   prometheus-webhook-dingtalk），不能直连；凭据经 env/secrets 注入，勿提交入库。
   改后先 `amtool check-config`（同上容器方式）再
   `docker compose -f deploy/observability/docker-compose.observability.yml restart alertmanager`。

6. Loki 保留期调优入口：`loki-config.yml` 的 `limits_config.retention_period`
   （默认 720h=30d，按磁盘/合规在 7d~30d+ 调整）。镜像内置默认实为 **0s=永久保留
   且 compactor 不清理**（经 `-print-config-stderr` 实测；旧注释所称「默认 15d」
   有误），真正删除依赖 `compactor.retention_enabled: true`。摄入/查询限额
   （`ingestion_rate_mb`、`max_query_series`、`max_global_streams_per_user`）同文件；
   改后 `docker compose -f deploy/observability/docker-compose.observability.yml restart loki`。
   注意 promtail 曾把 trace_id 提升为标签放大流基数（M49-D 时期被迫放宽
   `max_global_streams_per_user` 的原因），M54-C 已根治（structured metadata），
   查询方式变化与兼容性见下节。

#### trace_id 查询方式变化（M54-C：标签 → structured metadata）

**为什么**：M47-C 起 promtail 把 EAP 结构化日志的 `trace_id` 与 `level` 一并提炼为
Loki **标签**。`level` 低基数无妨；`trace_id` 每请求唯一，是高基数标签——每条 trace
一个独立流，索引基数随流量线性增长（Loki anti-pattern：TSDB 索引膨胀、查询变慢，
`max_global_streams_per_user` 也因此被迫放宽到 20000）。M54-C 根治：

- [deploy/observability/promtail-config.yml](../deploy/observability/promtail-config.yml)：
  `trace_id` 从 `labels` stage 迁到 **`structured_metadata` stage**（metadata 不进索引，
  无流基数成本）；`level` 保留为标签。
- [deploy/observability/loki-config.yml](../deploy/observability/loki-config.yml)：
  显式 `limits_config.allow_structured_metadata: true`（Loki 3.x 默认已为 true，写出防
  上游漂移；为 false 时含 metadata 的推送会被拒收）。
- `scripts/check_observability.py` 新增双向断言（`trace_id` 不得出现在 labels stage、
  必须出现在 structured_metadata stage，`level` 必须保留标签），防配置回退。

**查询方式**：Loki 3.x 的 LogQL 行过滤语法对标签与 structured metadata 同样生效，
写法不变——按请求追踪在查询末尾接 `| trace_id="..."` 即可：

```logql
{service="eap"} | trace_id="a1b2c3d4e5f6"          # 精确匹配某请求的全链路日志
{service="eap"} | trace_id=~"a1b2.*"               # 正则同样可用
{service="eap", level="ERROR"} | trace_id="a1b2c3d4e5f6"   # level 仍是标签，选择器内组合
```

promtail 采 Docker 日志实际产出的索引标签为 `container` / `compose_project` /
`service` / `level`（生产栈后端服务为 `eap`，故按 `{service="eap"}` 或
`{container="..."}` 圈定范围；3.2.0 的 docker_sd 不产生 `job` 标签，勿按
`{job="docker"}` 写选择器）。另注意：查询**响应**的流标签映射在 Loki 3.x 会把
structured metadata（trace_id/detected_level 等）合并展示出来，肉眼易误以为
trace_id 仍是标签——以 `GET /loki/api/v1/labels`（索引标签列表）为准，其中不含
trace_id。

Grafana Explore（Loki 数据源）中旧的 `| trace_id="..."` 查询**无需任何修改**即可继续
命中；唯一可见差异是 `trace_id` 不再出现在流标签列表/label 自动补全里，而在
detected fields（metadata）中。当前 `grafana-dashboard-eap.json` 全部面板为 Prometheus
指标查询，无 trace_id/LogQL 用点，故仪表盘无需迁移。

**兼容性与诚实局限**：

- 查询语法完全兼容：`| trace_id="..."` 行过滤器对标签形态与 metadata 形态都能命中，
  迁移不要求改任何既有查询/面板。
- 迁移只对**新流入**日志生效：已入库的存量索引里 trace_id 仍是标签形态（旧流继续按
  标签可查），按 30d 保留期（`retention_period: 720h`）滚动过期后彻底消失；过渡期
  标签基数不再增长，但旧流仍在索引中。
- 升级既有观测栈时先重启 loki（拿到 `allow_structured_metadata: true`）再重启
  promtail：顺序颠倒的话，promtail 会向未开此项的 Loki 推 metadata 而被拒收（4xx）。
  另外把新 promtail 指向 Loki 2.x 老栈同理会被拒收（2.x 无 metadata 概念）。

**诚实局限**：真实通知触达（SMTP 凭据、IM 机器人 webhook/中转服务）属外部条件，
仓库内只给注释样例与联调步骤，未做真实外发验证；本小节链路验证止于 Alertmanager
路由命中（UI/API/amtool 可证）。

### 9.6 依赖漏洞扫描

CI `dependency-scan` job（[.github/workflows/ci.yml](../.github/workflows/ci.yml)）：
`pip-audit`（后端锁文件）+ `pnpm audit --prod --audit-level=high`（控制台），高危即红。
