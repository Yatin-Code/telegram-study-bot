#!/usr/bin/env bash
# Nightly SQLite mirror backup — dump to a dated .sql file under backups/.
# Intended to run from cron or systemd timer. Keeps the last 7 dumps and
# removes older ones automatically.
#
# Usage: bash backup_mirror.sh [db_path] [backup_dir]
#   defaults: db_path=sqlite_mirror.db  backup_dir=backups/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DB_PATH="${1:-$SCRIPT_DIR/sqlite_mirror.db}"
BACKUP_DIR="${2:-$SCRIPT_DIR/backups}"

mkdir -p "$BACKUP_DIR"

STAMP="$(date +%Y%m%d_%H%M%S)"
DEST="$BACKUP_DIR/mirror_$STAMP.sql"

PYTHON_BIN="${PYTHON_BIN:-${PREFIX:-/data/data/com.termux/files/usr}/bin/python}"
"$PYTHON_BIN" - "$DB_PATH" "$DEST" <<'PY'
import sqlite3
import sys

source, destination = sys.argv[1:]
with sqlite3.connect(source) as conn, open(destination, "w", encoding="utf-8") as out:
    for line in conn.iterdump():
        out.write(line)
        out.write("\n")
PY
echo "backup: $DEST ($(wc -c < "$DEST") bytes)"

# Rotate: keep last 7, delete older.
ls -1t "$BACKUP_DIR"/mirror_*.sql 2>/dev/null | tail -n +8 | xargs -r rm --
