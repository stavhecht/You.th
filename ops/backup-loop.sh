#!/bin/sh
# The `backup` service in docker-compose.prod.yml: one dump a night into
# /backups, KEEP_DAYS kept. ops/backup.sh is the same dump run by hand, and its
# header has the restore steps. Copy the files off this machine: a backup on the
# database's own disk is not one.
set -u
KEEP_DAYS="${KEEP_DAYS:-14}"

while true; do
  file="/backups/youth-$(date +%Y%m%d-%H%M%S).dump"
  if pg_dump --format=custom --no-owner -h db -U youth youth > "$file.part" \
    && [ -s "$file.part" ]; then
    mv "$file.part" "$file"
    find /backups -name 'youth-*.dump' -mtime "+$KEEP_DAYS" -exec rm -f {} +
    echo "wrote $file"
  else
    rm -f "$file.part"
    echo "backup failed: pg_dump wrote no usable archive" >&2
  fi
  sleep 86400
done
