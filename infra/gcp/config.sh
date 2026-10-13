# shellcheck shell=bash disable=SC2034  # sourced: variables are used by the callers
# Shared settings for bootstrap.sh / deploy.sh. Sourced, not executed.
# Override any of these in the environment; PROJECT_ID is required.
#
# Everything lives in us-central1: one of the three regions where an e2-micro
# VM, 30 GB of standard disk and 5 GB of Cloud Storage are always free.

: "${PROJECT_ID:?set PROJECT_ID to your GCP project id}"
REGION=${REGION:-us-central1}
ZONE=${ZONE:-us-central1-a}

# Artifact Registry (images for the API, the backup job, and Postgres itself:
# the database VM has no internet access, so it can't pull from Docker Hub).
AR_REPO=${AR_REPO:-depgraph}
AR_HOST="$REGION-docker.pkg.dev"
AR="$AR_HOST/$PROJECT_ID/$AR_REPO"
PG_IMAGE_SRC=${PG_IMAGE_SRC:-postgres:17-alpine}
PG_IMAGE="$AR/postgres:17-alpine"

# Private network. Cloud Run reaches the database over Direct VPC egress,
# using addresses from this subnet; nothing else can.
NETWORK=depgraph-vpc
SUBNET=depgraph-subnet
SUBNET_RANGE=10.10.0.0/24
DB_IP=10.10.0.10

# Database VM: free-tier shape. Data lives on its own disk, so the boot disk
# (OS) can be recreated from a newer image without touching the database.
VM=depgraph-db
VM_TYPE=e2-micro
BOOT_DISK_GB=10
DATA_DISK=depgraph-pgdata
DATA_DISK_GB=20
DB_NAME=depgraph
DB_USER=depgraph
SECRET=depgraph-db-password

# Cloud Run.
SERVICE=depgraph-api
JOB_MIGRATE=depgraph-migrate
JOB_SYNC=depgraph-sync
JOB_BACKUP=depgraph-backup

# Backups: nightly pg_dump, deleted after 7 days by a bucket lifecycle rule.
BUCKET=${BUCKET:-$PROJECT_ID-depgraph-backups}

# Service accounts: one per workload, each with only what it needs.
SA_API_NAME=depgraph-api          # Cloud Run service + migrate/sync jobs
SA_BACKUP_NAME=depgraph-backup    # backup job
SA_DB_NAME=depgraph-db            # database VM
SA_SCHED_NAME=depgraph-scheduler  # Cloud Scheduler -> run jobs
sa_email() { echo "$1@$PROJECT_ID.iam.gserviceaccount.com"; }
SA_API=$(sa_email "$SA_API_NAME")
SA_BACKUP=$(sa_email "$SA_BACKUP_NAME")
SA_DB=$(sa_email "$SA_DB_NAME")
SA_SCHED=$(sa_email "$SA_SCHED_NAME")

# Frontend origins for CORS. WEB_ORIGIN: the production Vercel URL.
# VERCEL_TEAM: your Vercel team slug, to admit that team's preview deploys only.
WEB_ORIGIN=${WEB_ORIGIN:-}
VERCEL_TEAM=${VERCEL_TEAM:-}
WEB_PROJECT=${WEB_PROJECT:-depgraph}

# Public-demo limits.
MAX_LOCKFILE_BYTES=${MAX_LOCKFILE_BYTES:-5242880}
SCAN_RETENTION_DAYS=${SCAN_RETENTION_DAYS:-7}

# Budget alerts (needs BILLING_ACCOUNT, e.g. 0X0X0X-0X0X0X-0X0X0X).
BILLING_ACCOUNT=${BILLING_ACCOUNT:-}
BUDGET_USD=${BUDGET_USD:-5}

# --- helpers ---------------------------------------------------------------
# Dry run unless --apply: every mutating command is printed, not run, so the
# exact commands can be reviewed first. Read-only checks always run.
APPLY=0
for arg in "$@"; do [ "$arg" = "--apply" ] && APPLY=1; done

run() {
  printf '+' >&2
  printf ' %q' "$@" >&2
  printf '\n' >&2
  if [ "$APPLY" = 1 ]; then "$@"; fi
}

# True if a read-only describe/list command succeeds (the resource exists).
exists() { "$@" >/dev/null 2>&1; }

step() { printf '\n== %s\n' "$*" >&2; }
