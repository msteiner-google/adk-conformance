output "runner_image" {
  description = "Runner image path (build it with deploy/deploy.sh)."
  value       = local.runner_image
}

output "runner_service_account" {
  value = google_service_account.runner.email
}

output "source_bucket" {
  value = google_storage_bucket.source.name
}

output "source_object" {
  value = local.source_object
}

output "results_bucket" {
  value = google_storage_bucket.results.name
}

output "firestore_database" {
  value = google_firestore_database.db.name
}

output "scheduler_job" {
  value = google_cloud_scheduler_job.nightly.name
}

output "run_now" {
  description = "Command that starts a run immediately."
  value       = "gcloud scheduler jobs run ${google_cloud_scheduler_job.nightly.name} --project ${var.project_id} --location ${var.region}"
}
