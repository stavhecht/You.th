#!/bin/bash
# Dump the database (articles, sources, accounts and every stored image: the
# image bytes live in Postgres) to one compressed file, and keep the last N days.
#
#   ops/backup.sh                 dump the compose `db` service into ./backups
#   BACKUP_DIR=/mnt/offsite KEEP_DAYS=30 ops/backup.sh
#   DATABASE_URL=postgresql://user:pass@host/db ops/backup.sh    a managed database
#
# Restore into an EMPTY database (it does not merge):
#   pg_restore --clean --if-exists --no-owner -d postgresql://user:pass@host/db FILE
# or, into the compose database:
#   docker compose exec -T db pg_restore --clean --if-exists --no-owner -U youth -d youth < FILE
#
# A backup on the same disk as the database is not a backup. Point BACKUP_DIR at
# other storage, or copy the file off the machine. Run it nightly (cron/launchd).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
FILE="$BACKUP_DIR/youth-$(date +%Y%m%d-%H%M%S).dump"

mkdir -p "$BACKUP_DIR"
if [ -n "${DATABASE_URL:-}" ]; then
  # pg_dump wants a libpq URL, not SQLAlchemy's `postgresql+asyncpg://`.
  pg_dump --format=custom --no-owner "${DATABASE_URL/+asyncpg/}" > "$FILE.part"
else
  (cd "$ROOT" && docker compose exec -T db pg_dump --format=custom --no-owner -U youth youth) > "$FILE.part"
fi

# An empty or truncated dump must not replace a good one or count as a backup.
if [ ! -s "$FILE.part" ] || [ "$(head -c 5 "$FILE.part")" != "PGDMP" ]; then
  rm -f "$FILE.part"
  echo "backup failed: pg_dump wrote no usable archive" >&2
  exit 1
fi
mv "$FILE.part" "$FILE"
find "$BACKUP_DIR" -name 'youth-*.dump' -mtime "+$KEEP_DAYS" -delete
echo "wrote $FILE ($(du -h "$FILE" | cut -f1)); keeping $KEEP_DAYS days in $BACKUP_DIR"
