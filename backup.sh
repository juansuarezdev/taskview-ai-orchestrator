#!/bin/bash
# Daily dump of the TaskView DB; keeps the last 14.
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$HOME/Backups/taskview"
mkdir -p "$DIR"
OUT="$DIR/taskview-$(date +%F).sql.gz"; TMP="$OUT.tmp"
docker exec taskview-db pg_dump -U taskview_db_user -d taskviewdb | gzip > "$TMP"
# never keep an empty dump (e.g. docker missing from PATH)
[ "$(gunzip -c "$TMP" | head -c 4096 | wc -c)" -ge 4096 ] || { rm -f "$TMP"; echo "FAIL empty dump" >&2; exit 1; }
mv "$TMP" "$OUT"
ls -1t "$DIR"/taskview-*.sql.gz | tail -n +15 | xargs -I{} rm -- {} 2>/dev/null || true
