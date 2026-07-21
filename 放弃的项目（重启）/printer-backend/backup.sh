#!/bin/bash
# ============================================================
# HN 云打印 — 数据库备份脚本
# 用法: bash backup.sh
# 建议: 添加到 crontab 每天执行
#       0 3 * * * /opt/printer-backend/backup.sh >> /var/log/printer-backup.log 2>&1
# ============================================================

set -e

# 配置
PROJECT_DIR="/opt/printer-backend"
DB_FILE="$PROJECT_DIR/orders.db"
BACKUP_DIR="$PROJECT_DIR/backups"
RETENTION_DAYS=7

# 创建备份目录
mkdir -p "$BACKUP_DIR"

# 生成备份文件名（带时间戳）
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/orders_$TIMESTAMP.db"

# 备份数据库
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始备份: $DB_FILE → $BACKUP_FILE"
cp "$DB_FILE" "$BACKUP_FILE"

# 压缩备份
gzip "$BACKUP_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份已压缩: ${BACKUP_FILE}.gz"

# 删除超过保留天数的旧备份
DELETED=$(find "$BACKUP_DIR" -name "orders_*.db.gz" -mtime +$RETENTION_DAYS -delete -print | wc -l)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 清理旧备份: 删除 $DELETED 个"

# 显示当前备份大小
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 备份完成，当前备份目录:"
ls -lh "$BACKUP_DIR"
