#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# One-time (and safely re-runnable) creation of everything the API runs on.
#
#   PROJECT_ID=my-proj BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX infra/gcp/bootstrap.sh          # print the plan
#   PROJECT_ID=my-proj BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX infra/gcp/bootstrap.sh --apply  # do it
#
# Idempotent: every resource is checked first and only created if missing, so
# a run that failed halfway can simply be repeated. Needs gcloud (logged in)
# and Docker (to copy the Postgres image into Artifact Registry).
# Expected cost: $0/month inside the free tier; see NOTES.md, "Step 7".
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=config.sh
. "$HERE/config.sh"

G=(gcloud --project "$PROJECT_ID" --quiet)

step "APIs"
run "${G[@]}" services enable \
  compute.googleapis.com run.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com \
  iam.googleapis.com billingbudgets.googleapis.com

step "Service accounts"
for sa in "$SA_API_NAME" "$SA_BACKUP_NAME" "$SA_DB_NAME" "$SA_SCHED_NAME"; do
  exists "${G[@]}" iam service-accounts describe "$(sa_email "$sa")" ||
    run "${G[@]}" iam service-accounts create "$sa" --display-name "depgraph: $sa"
done
# The VM writes its logs and metrics, and pulls images. Nothing else.
for role in roles/logging.logWriter roles/monitoring.metricWriter roles/artifactregistry.reader; do
  run "${G[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:$SA_DB" --role "$role" --condition None >/dev/null
done
# Scheduler may start Cloud Run jobs (roles/run.invoker includes run.jobs.run).
run "${G[@]}" projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:$SA_SCHED" --role roles/run.invoker --condition None >/dev/null

step "Artifact Registry (keeps the 3 newest versions of each image; free tier is 0.5 GB)"
exists "${G[@]}" artifacts repositories describe "$AR_REPO" --location "$REGION" ||
  run "${G[@]}" artifacts repositories create "$AR_REPO" --location "$REGION" \
    --repository-format docker --description "depgraph images"
POLICY=$(mktemp)
cat >"$POLICY" <<'JSON'
[
  {"name": "keep-newest", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
  {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "7d"}}
]
JSON
run "${G[@]}" artifacts repositories set-cleanup-policies "$AR_REPO" --location "$REGION" \
  --policy "$POLICY" --no-dry-run

step "Postgres image -> Artifact Registry (the VM has no route to Docker Hub)"
if ! exists "${G[@]}" artifacts docker images describe "$PG_IMAGE"; then
  run gcloud auth configure-docker "$AR_HOST" --quiet
  run docker pull "$PG_IMAGE_SRC"
  run docker tag "$PG_IMAGE_SRC" "$PG_IMAGE"
  run docker push "$PG_IMAGE"
fi

step "Database password (generated once, stored only in Secret Manager)"
if ! exists "${G[@]}" secrets describe "$SECRET"; then
  printf '+ <random 32-char password> | gcloud secrets create %s --data-file=-\n' "$SECRET" >&2
  if [ "$APPLY" = 1 ]; then
    # URL-safe alphabet: the password is substituted into a connection URL.
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 |
      "${G[@]}" secrets create "$SECRET" --replication-policy automatic --data-file=-
  fi
fi
for sa in "$SA_API" "$SA_BACKUP" "$SA_DB"; do
  run "${G[@]}" secrets add-iam-policy-binding "$SECRET" \
    --member "serviceAccount:$sa" --role roles/secretmanager.secretAccessor >/dev/null
done

step "Network: private subnet with Private Google Access, no NAT, no public IPs"
exists "${G[@]}" compute networks describe "$NETWORK" ||
  run "${G[@]}" compute networks create "$NETWORK" --subnet-mode custom
exists "${G[@]}" compute networks subnets describe "$SUBNET" --region "$REGION" ||
  run "${G[@]}" compute networks subnets create "$SUBNET" --network "$NETWORK" \
    --region "$REGION" --range "$SUBNET_RANGE" --enable-private-ip-google-access
# Postgres: only from this subnet (Cloud Run's Direct VPC egress addresses).
exists "${G[@]}" compute firewall-rules describe depgraph-allow-postgres ||
  run "${G[@]}" compute firewall-rules create depgraph-allow-postgres --network "$NETWORK" \
    --direction INGRESS --allow tcp:5432 --source-ranges "$SUBNET_RANGE" --target-tags depgraph-db
# SSH only through Identity-Aware Proxy (`gcloud compute ssh --tunnel-through-iap`).
exists "${G[@]}" compute firewall-rules describe depgraph-allow-iap-ssh ||
  run "${G[@]}" compute firewall-rules create depgraph-allow-iap-ssh --network "$NETWORK" \
    --direction INGRESS --allow tcp:22 --source-ranges 35.235.240.0/20 --target-tags depgraph-db
exists "${G[@]}" compute addresses describe depgraph-db-ip --region "$REGION" ||
  run "${G[@]}" compute addresses create depgraph-db-ip --region "$REGION" \
    --subnet "$SUBNET" --addresses "$DB_IP"

step "Database VM: e2-micro, Container-Optimized OS, no external IP"
exists "${G[@]}" compute disks describe "$DATA_DISK" --zone "$ZONE" ||
  run "${G[@]}" compute disks create "$DATA_DISK" --zone "$ZONE" \
    --size "${DATA_DISK_GB}GB" --type pd-standard
exists "${G[@]}" compute instances describe "$VM" --zone "$ZONE" ||
  run "${G[@]}" compute instances create "$VM" --zone "$ZONE" \
    --machine-type "$VM_TYPE" \
    --network-interface "subnet=$SUBNET,private-network-ip=$DB_IP,no-address" \
    --image-family cos-stable --image-project cos-cloud \
    --boot-disk-size "${BOOT_DISK_GB}GB" --boot-disk-type pd-standard \
    --disk "name=$DATA_DISK,device-name=pgdata,mode=rw,boot=no,auto-delete=no" \
    --service-account "$SA_DB" --scopes cloud-platform \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --tags depgraph-db \
    --metadata "pg-image=$PG_IMAGE,pg-secret=$SECRET,pg-db=$DB_NAME,pg-user=$DB_USER,google-logging-enabled=true" \
    --metadata-from-file "startup-script=$HERE/vm-startup.sh"

step "Backup bucket: 7-day lifecycle, no soft-delete (it would bill deleted dumps)"
exists "${G[@]}" storage buckets describe "gs://$BUCKET" ||
  run "${G[@]}" storage buckets create "gs://$BUCKET" --location "$REGION" \
    --default-storage-class STANDARD --uniform-bucket-level-access \
    --public-access-prevention --soft-delete-duration 0
LIFECYCLE=$(mktemp)
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 7}}]}' >"$LIFECYCLE"
run "${G[@]}" storage buckets update "gs://$BUCKET" --lifecycle-file "$LIFECYCLE"
run "${G[@]}" storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member "serviceAccount:$SA_BACKUP" --role roles/storage.objectCreator >/dev/null

step "Schedules: hourly advisory sync + retention, nightly backup (2 of 3 free jobs)"
schedule() { # name, cron, cloud-run-job
  local uri="https://run.googleapis.com/v2/projects/$PROJECT_ID/locations/$REGION/jobs/$3:run"
  local verb=create
  exists "${G[@]}" scheduler jobs describe "$1" --location "$REGION" && verb=update
  run "${G[@]}" scheduler jobs "$verb" http "$1" --location "$REGION" \
    --schedule "$2" --time-zone UTC --uri "$uri" --http-method POST \
    --oauth-service-account-email "$SA_SCHED"
}
schedule depgraph-sync-hourly "17 * * * *" "$JOB_SYNC"
schedule depgraph-backup-nightly "43 3 * * *" "$JOB_BACKUP"

step "Budget alerts at \$1 and \$$BUDGET_USD"
if [ -z "$BILLING_ACCOUNT" ]; then
  echo "  skipped: set BILLING_ACCOUNT (gcloud billing accounts list)" >&2
elif ! gcloud billing budgets list --billing-account "$BILLING_ACCOUNT" \
  --format 'value(displayName)' 2>/dev/null | grep -qx depgraph; then
  run gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" \
    --display-name depgraph --budget-amount "${BUDGET_USD}USD" \
    --filter-projects "projects/$PROJECT_ID" \
    --threshold-rule "percent=$(awk "BEGIN{print 1/$BUDGET_USD}")" --threshold-rule percent=1.0
fi

step "Done"
if [ "$APPLY" != 1 ]; then echo "Dry run: nothing was changed. Re-run with --apply." >&2; fi
echo "Next: infra/gcp/deploy.sh (builds images, migrates, deploys the API and jobs)." >&2
