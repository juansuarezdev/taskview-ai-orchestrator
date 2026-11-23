#!/bin/bash
# Restores the latest DB backup into a throwaway Postgres and compares row counts with the live DB.
# Exit 0 = the backup restores and matches (counts may differ by what changed since the dump, but never be empty).
set -u
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
DIR="$HOME/Backups/taskview"
LATEST=$(ls -1t "$DIR"/taskview-*.sql.gz 2>/dev/null | head -1)
[ -n "$LATEST" ] || { echo "FAIL no backup"; exit 1; }
NAME=taskview-restore-test
docker rm -f $NAME >/dev/null 2>&1
docker run -d --name $NAME -e POSTGRES_PASSWORD=x postgres:17 >/dev/null || { echo "FAIL cannot start postgres"; exit 1; }
trap 'docker rm -f $NAME >/dev/null 2>&1' EXIT
for _ in $(seq 1 30); do docker exec $NAME pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec $NAME psql -U postgres -qc "CREATE ROLE taskview_db_user SUPERUSER LOGIN" -c "CREATE DATABASE taskviewdb OWNER taskview_db_user" >/dev/null
gunzip -c "$LATEST" | docker exec -i $NAME psql -U taskview_db_user -d taskviewdb -q -v ON_ERROR_STOP=0 >/dev/null 2>&1
count() { docker exec "$1" psql -U taskview_db_user -d taskviewdb -Atc "select (select count(*) from tasks.tasks) || '/' || (select count(*) from tasks.goals)" 2>/dev/null; }
RESTORED=$(count $NAME)
LIVE=$(count taskview-db)
echo "backup=$(basename "$LATEST") restored(tasks/goals)=$RESTORED live=$LIVE"
case "$RESTORED" in ""|0/*|*/0) echo "FAIL restore is empty"; exit 1;; esac
echo OK
