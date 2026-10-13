#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Build, migrate, deploy. Run after bootstrap.sh; CI runs it on merge to main.
#
#   PROJECT_ID=my-proj WEB_ORIGIN=https://depgraph.vercel.app VERCEL_TEAM=my-team \
#     infra/gcp/deploy.sh [--apply]
#
# Order matters: migrations run (as a Cloud Run job, inside the VPC) *before*
# the new API revision takes traffic. Migrations are therefore written to be
# compatible with the previous revision still serving meanwhile.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
# shellcheck source=config.sh
. "$HERE/config.sh"

TAG=${TAG:-$(git -C "$ROOT" rev-parse --short=12 HEAD)}
API_IMAGE="$AR/api:$TAG"
BACKUP_IMAGE="$AR/backup:$TAG"
G=(gcloud --project "$PROJECT_ID" --quiet)

step "Images ($TAG)"
if [ "${SKIP_BUILD:-0}" != 1 ]; then
  run gcloud auth configure-docker "$AR_HOST" --quiet
  run docker build --target runtime -t "$API_IMAGE" "$ROOT"
  run docker push "$API_IMAGE"
  run docker build --build-arg "PG_IMAGE=$PG_IMAGE" -t "$BACKUP_IMAGE" "$HERE/backup"
  run docker push "$BACKUP_IMAGE"
fi

# Settings shared by the API and its jobs. A YAML file rather than
# --set-env-vars, because values (CORS_ORIGINS is a JSON list) contain commas.
ENV_FILE=$(mktemp)
ORIGINS='[]'
[ -n "$WEB_ORIGIN" ] && ORIGINS="[\"$WEB_ORIGIN\"]"
ORIGIN_REGEX=""
[ -n "$VERCEL_TEAM" ] && ORIGIN_REGEX="^https://$WEB_PROJECT-[a-z0-9-]+-$VERCEL_TEAM\\.vercel\\.app\$"
cat >"$ENV_FILE" <<YAML
APP_ENV: production
LOG_JSON: "true"
DATABASE_URL: postgresql+asyncpg://$DB_USER@$DB_IP:5432/$DB_NAME
REDIS_URL: ""
CORS_ORIGINS: '$ORIGINS'
CORS_ORIGIN_REGEX: '$ORIGIN_REGEX'
MAX_LOCKFILE_BYTES: "$MAX_LOCKFILE_BYTES"
UPLOAD_LIMIT_PER_IP: "10"
UPLOAD_LIMIT_IP_WINDOW_S: "600"
UPLOAD_LIMIT_GLOBAL: "300"
UPLOAD_LIMIT_GLOBAL_WINDOW_S: "3600"
CLIENT_IP_PROXY_HOPS: "1"
SCAN_RETENTION_DAYS: "$SCAN_RETENTION_DAYS"
DB_POOL_SIZE: "5"
DB_MAX_OVERFLOW: "5"
YAML
echo "  env:" >&2
sed 's/^/    /' "$ENV_FILE" >&2

# Every workload: private egress through the VPC (to reach the database),
# public traffic (npm registry, OSV) straight out; the password from Secret
# Manager, never in an env var file or the image.
NET=(--network "$NETWORK" --subnet "$SUBNET" --vpc-egress private-ranges-only)
SECRETS=(--set-secrets "DATABASE_PASSWORD=$SECRET:latest")

job() { # name, image, service-account, memory, timeout, extra flags...
  local name=$1 image=$2 sa=$3 mem=$4 timeout=$5
  shift 5
  run "${G[@]}" run jobs deploy "$name" --region "$REGION" --image "$image" \
    --service-account "$sa" --memory "$mem" --cpu 1 --task-timeout "$timeout" \
    --max-retries 1 "${NET[@]}" "$@"
}
APP=(--env-vars-file "$ENV_FILE" "${SECRETS[@]}")

step "Migrate"
job "$JOB_MIGRATE" "$API_IMAGE" "$SA_API" 512Mi 600s "${APP[@]}" \
  --command alembic --args upgrade,head
run "${G[@]}" run jobs execute "$JOB_MIGRATE" --region "$REGION" --wait

step "API service"
# Scale to zero; at most 2 instances (caps cost and database connections).
# Request-based billing: CPU only while handling a request.
run "${G[@]}" run deploy "$SERVICE" --region "$REGION" --image "$API_IMAGE" \
  --service-account "$SA_API" --allow-unauthenticated \
  --min-instances 0 --max-instances 2 --concurrency 40 \
  --memory 512Mi --cpu 1 --timeout 120s --cpu-boost \
  --env-vars-file "$ENV_FILE" "${SECRETS[@]}" "${NET[@]}"

step "Jobs: advisory sync (+ retention), backup"
# The first sync is a full OSV load (~230k records): give it time and memory.
job "$JOB_SYNC" "$API_IMAGE" "$SA_API" 2Gi 3600s "${APP[@]}" \
  --command python --args=-m,depgraph.advisories.cli,sync
# libpq's own env vars; the dump is written to the in-memory /tmp, hence 1 GiB.
job "$JOB_BACKUP" "$BACKUP_IMAGE" "$SA_BACKUP" 1Gi 1800s \
  --set-env-vars "PGHOST=$DB_IP,PGUSER=$DB_USER,PGDATABASE=$DB_NAME,BUCKET=$BUCKET" \
  --set-secrets "PGPASSWORD=$SECRET:latest"

URL=$("${G[@]}" run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)' 2>/dev/null || true)
step "Done: ${URL:-<service not deployed yet>}"
if [ "$APPLY" != 1 ]; then echo "Dry run: nothing was changed. Re-run with --apply." >&2; fi
