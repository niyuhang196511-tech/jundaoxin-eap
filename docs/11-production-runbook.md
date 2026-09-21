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
API 实例（`job="eap"`），`rule_files` 挂载该文件；规则含：实例宕机（`sum(up{job="eap"}) < 2`）、
5xx 占比 > 5%、平均延迟（histogram 桶导出前为均值代理）、任务失败增速、熔断跳闸、网关在途。

