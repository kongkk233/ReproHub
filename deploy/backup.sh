#!/usr/bin/env bash
# SQLite 在线一致性备份（WAL 安全），保留最近 14 份。
# cron 示例：15 3 * * * /home/ubuntu/ReproHub/deploy/backup.sh >> /home/ubuntu/ReproHub/data/backup.log 2>&1
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA="${REPROHUB_DATA_DIR:-$ROOT/data}"
DEST="${REPROHUB_BACKUP_DIR:-$DATA/backups}"
KEEP="${REPROHUB_BACKUP_KEEP:-14}"
mkdir -p "$DEST"
out="$DEST/reprohub-$(date +%Y%m%d-%H%M%S).db"
"$ROOT/.venv/bin/python" - "$DATA/reprohub.db" "$out" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
gzip -f "$out"
ls -1t "$DEST"/reprohub-*.db.gz | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "$(date -Is) backup ok: $out.gz"
