#!/bin/bash
# Daily dump of the TaskView DB; keeps the last 14.
set -e
DIR="$HOME/Backups/taskview"
mkdir -p "$DIR"
docker exec taskview-db pg_dump -U taskview_db_user -d taskviewdb | gzip > "$DIR/taskview-$(date +%F).sql.gz"
ls -1t "$DIR"/taskview-*.sql.gz | tail -n +15 | xargs -I{} rm -- {} 2>/dev/null || true
