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
