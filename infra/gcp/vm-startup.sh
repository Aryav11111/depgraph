#!/bin/bash
# Startup script for the database VM (Container-Optimized OS). Runs as root on
# every boot; safe to run any number of times.
#
# 1. mount the data disk (formatting it only the very first time);
# 2. fetch the database password from Secret Manager, into tmpfs only;
# 3. (re)start Postgres in Docker on the data disk, tuned for 1 GB of RAM.
#
# No internet access: Secret Manager and Artifact Registry are reached through
# Private Google Access, with the VM service account's token from the
# metadata server.
set -euo pipefail

MD=http://metadata.google.internal/computeMetadata/v1
md() { curl -sf -H 'Metadata-Flavor: Google' "$MD/$1"; }
attr() { md "instance/attributes/$1"; }

PROJECT=$(md project/project-id)
PG_IMAGE=$(attr pg-image)
SECRET=$(attr pg-secret)
DB=$(attr pg-db)
USER_=$(attr pg-user)

DEV=/dev/disk/by-id/google-pgdata
MNT=/mnt/disks/pgdata
RUN=/run/depgraph  # tmpfs: the password never touches a disk

# --- 1. data disk --------------------------------------------------------------
if ! blkid "$DEV" >/dev/null 2>&1; then
  echo "formatting $DEV (first boot)"
  mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "$DEV"
fi
mkdir -p "$MNT"
mountpoint -q "$MNT" || mount -o discard,defaults,noatime "$DEV" "$MNT"
# Never start Postgres on the boot disk by accident: it would initdb an empty
# database there and the app would happily use it.
mountpoint -q "$MNT" || { echo "data disk not mounted; refusing to start" >&2; exit 1; }
mkdir -p "$MNT/data"

# --- 2. password -----------------------------------------------------------------
mkdir -p "$RUN"
chmod 700 "$RUN"
TOKEN=$(md instance/service-accounts/default/token | sed -E 's/.*"access_token":"([^"]+)".*/\1/')
curl -sf -H "Authorization: Bearer $TOKEN" \
  "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$SECRET/versions/latest:access" |
  sed -E 's/.*"data":"([^"]+)".*/\1/' | base64 -d >"$RUN/pgpass"
# The postgres image runs as uid 70 (alpine); it reads the file at first init.
chown 70:70 "$RUN/pgpass"
chmod 400 "$RUN/pgpass"

# --- 3. postgres -------------------------------------------------------------------
# COS's / is read-only: Docker's registry credentials go in a writable HOME.
export HOME="$RUN/home"
mkdir -p "$HOME"
docker-credential-gcr configure-docker --registries "${PG_IMAGE%%/*}"
docker pull "$PG_IMAGE"

# Recreated every boot, after the mount above. No restart policy that would
# let Docker start it on boot before the disk is mounted: on-failure covers
# crashes, this script covers reboots.
docker rm -f postgres >/dev/null 2>&1 || true
# Tuning for an e2-micro (2 shared vCPUs, 1 GB RAM, standard HDD-backed disk):
# small shared_buffers and the rest left to the OS page cache; few
# connections (Cloud Run: 2 instances x 10, plus jobs); checkpoints spread
# out so the advisory full load doesn't thrash the disk.
docker run -d --name postgres --restart on-failure \
  --log-driver gcplogs \
  -p 5432:5432 \
  --shm-size 128m \
  -v "$MNT/data:/var/lib/postgresql/data" \
  -v "$RUN/pgpass:/run/secrets/pgpass:ro" \
  -e POSTGRES_DB="$DB" -e POSTGRES_USER="$USER_" \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/pgpass \
  -e POSTGRES_INITDB_ARGS="--auth-host=scram-sha-256" \
  "$PG_IMAGE" \
  -c shared_buffers=128MB \
  -c effective_cache_size=512MB \
  -c work_mem=4MB \
  -c maintenance_work_mem=64MB \
  -c max_connections=40 \
  -c max_wal_size=1GB \
  -c checkpoint_completion_target=0.9 \
  -c log_min_duration_statement=1000 \
  -c password_encryption=scram-sha-256
echo "postgres started from $PG_IMAGE"
