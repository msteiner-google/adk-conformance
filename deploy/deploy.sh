#!/usr/bin/env bash
# One-time (and on-change) deployment of adk-monitor to a GCP project:
#   1. terraform apply (APIs, buckets, Firestore, service accounts, scheduler)
#   2. build and push the runner image with Cloud Build
#   3. upload the source tarball the nightly run builds
# After this, runs happen on GCP on schedule; nothing depends on this machine.
#
#   deploy/deploy.sh <project-id> [region]
set -euo pipefail
cd "$(dirname "$0")/.."
project="$1"
region="${2:-us-central1}"
tf=(terraform -chdir=deploy/terraform)

"${tf[@]}" init -input=false
"${tf[@]}" apply -input=false -var "project_id=${project}" -var "region=${region}"

image="$("${tf[@]}" output -raw runner_image)"
runner_sa="$("${tf[@]}" output -raw runner_service_account)"
source_bucket="$("${tf[@]}" output -raw source_bucket)"

gcloud builds submit deploy/runner \
  --project "$project" --region "$region" \
  --config deploy/runner/cloudbuild.yaml \
  --substitutions "_IMAGE=${image}" \
  --service-account "projects/${project}/serviceAccounts/${runner_sa}" \
  --gcs-source-staging-dir "gs://${source_bucket}/staging"

deploy/push_source.sh "$source_bucket"

echo
echo "Deployed. Start a run now with:"
echo "  $("${tf[@]}" output -raw run_now)"
