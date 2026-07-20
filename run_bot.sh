#!/data/data/com.termux/files/usr/bin/sh
set -eu

PROJECT_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_ROOT"
exec "${PREFIX:-/data/data/com.termux/files/usr}/bin/python" "$PROJECT_ROOT/bot.py"
