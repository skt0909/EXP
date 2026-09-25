#!/bin/sh
# Daily database backup to a Google Cloud Storage bucket. Install with
# `sudo crontab -u fpl -e` and a line like:
#   15 4 * * * /opt/fpl/deploy/scripts/backup_db.sh gs://your-bucket/fpl
# Restore: gsutil cp gs://.../fpl-YYYY-MM-DD.dump . && pg_restore -d fpl_game --clean fpl-YYYY-MM-DD.dump
set -e
DEST="$1"
[ -n "$DEST" ] || { echo "usage: $0 gs://bucket/prefix"; exit 1; }
FILE="/tmp/fpl-$(date -u +%F).dump"
pg_dump -Fc fpl_game -f "$FILE"
gsutil -q cp "$FILE" "$DEST/"
rm -f "$FILE"
