#!/bin/sh
# pg_dump the database and upload it to gs://$BUCKET/depgraph/<timestamp>.dump.
# Connection from libpq env vars (PGHOST, PGUSER, PGDATABASE, PGPASSWORD).
# The bucket's lifecycle rule deletes dumps after 7 days.
#
# Dump to a file first, then upload: if pg_dump fails halfway, nothing is
# uploaded, rather than a truncated dump that looks like a good one.
set -eu
: "${BUCKET:?}" "${PGHOST:?}"

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT=/tmp/depgraph.dump
# Custom format: compressed, and restorable table by table with pg_restore.
# Rate-limit counters are disposable, so their rows are skipped.
pg_dump --format=custom --no-owner --no-privileges \
  --exclude-table-data=rate_limit_buckets --file="$OUT"
SIZE=$(wc -c <"$OUT")

TOKEN=$(curl -sf -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token |
  sed -E 's/.*"access_token":"([^"]+)".*/\1/')
curl -sf -X POST --data-binary "@$OUT" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/octet-stream' \
  "https://storage.googleapis.com/upload/storage/v1/b/$BUCKET/o?uploadType=media&name=depgraph/$STAMP.dump" \
  >/dev/null
echo "{\"severity\":\"INFO\",\"message\":\"backup uploaded\",\"object\":\"depgraph/$STAMP.dump\",\"bytes\":$SIZE}"
