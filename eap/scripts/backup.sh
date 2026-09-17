#!/usr/bin/env bash
# EAP 备份脚本（M13）：PostgreSQL 全量 + Milvus 数据卷。
# 用法：./scripts/backup.sh <备份目录> [保留天数]
# 定时：crontab  ->  0 2 * * * /opt/eap/scripts/backup.sh /backup 14
set -euo pipefail

BACKUP_DIR="${1:-/backup}"
KEEP_DAYS="${2:-14}"
STAMP="$(date +%F-%H%M)"
COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$BACKUP_DIR"

echo "== EAP 备份开始 $(date) =="

# 1) PostgreSQL（业务全量数据）
if docker ps --format '{{.Names}}' | grep -q 'eap-postgres'; then
  PG_CONTAINER="$(docker ps --format '{{.Names}}' | grep 'eap-postgres' | head -1)"
  docker exec "$PG_CONTAINER" pg_dump -U eap -Fc eap \
    > "$BACKUP_DIR/eap-db-$STAMP.dump"
  echo "[ok] postgres -> eap-db-$STAMP.dump"
else
  echo "[skip] postgres 容器未运行"
fi

# 2) Milvus（向量数据卷，standalone 形态 = etcd+minio 数据目录）
MILVUS_VOL="$(docker volume ls --format '{{.Name}}' | grep -m1 'milvus' || true)"
if [ -n "$MILVUS_VOL" ]; then
  VOL_PATH="$(docker volume inspect "$MILVUS_VOL" --format '{{.Mountpoint}}')"
  tar czf "$BACKUP_DIR/milvus-$STAMP.tgz" -C "$(dirname "$VOL_PATH")" "$(basename "$VOL_PATH")"
  echo "[ok] milvus -> milvus-$STAMP.tgz"
else
  echo "[skip] milvus 卷未找到"
fi

# 3) 保留期清理
if [ "$KEEP_DAYS" -gt 0 ] 2>/dev/null; then
  find "$BACKUP_DIR" -name "eap-db-*.dump" -mtime +"$KEEP_DAYS" -delete
  find "$BACKUP_DIR" -name "milvus-*.tgz" -mtime +"$KEEP_DAYS" -delete
  echo "[ok] 已清理 ${KEEP_DAYS} 天前的备份"
fi

echo "== 备份完成 $(date) =="
