# adk-monitor on GCP: a nightly Cloud Build run, triggered by Cloud Scheduler,
# that builds the hosts with Bazel (remote cache in GCS), runs every scenario,
# and publishes results to Firestore (documents) and GCS (transcripts, reports).

locals {
  services = [
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "logging.googleapis.com",
    "storage.googleapis.com",
  ]
  runner_image  = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.repo.repository_id}/runner"
  source_object = "source/adk-monitor.tar.gz"
}

resource "google_project_service" "services" {
  for_each           = toset(local.services)
  service            = each.value
  disable_on_destroy = false
}

# --- Storage -----------------------------------------------------------------

resource "google_artifact_registry_repository" "repo" {
  repository_id = var.name
  location      = var.region
  format        = "DOCKER"
  description   = "adk-monitor runner image"
  depends_on    = [google_project_service.services]
}

resource "google_storage_bucket" "source" {
  name                        = "${var.project_id}-${var.name}-source"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning {
    enabled = true
  }
  lifecycle_rule {
    condition {
      num_newer_versions = 10
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.services]
}

resource "google_storage_bucket" "cache" {
  name                        = "${var.project_id}-${var.name}-bazel-cache"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    condition {
      age = var.cache_retention_days
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.services]
}

resource "google_storage_bucket" "results" {
  name                        = "${var.project_id}-${var.name}-results"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  depends_on                  = [google_project_service.services]
}

resource "google_firestore_database" "db" {
  name        = var.firestore_database
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"
  depends_on  = [google_project_service.services]
}

# --- Identities ----------------------------------------------------------------

# Runs the nightly build (and builds the runner image).
resource "google_service_account" "runner" {
  account_id   = "${var.name}-runner"
  display_name = "adk-monitor runner (Cloud Build)"
}

# Used by Cloud Scheduler to start builds that run as the runner.
resource "google_service_account" "scheduler" {
  account_id   = "${var.name}-scheduler"
  display_name = "adk-monitor scheduler"
}

resource "google_project_iam_member" "runner_project" {
  for_each = toset([
    "roles/logging.logWriter",
    "roles/datastore.user",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_artifact_registry_repository_iam_member" "runner_repo" {
  repository = google_artifact_registry_repository.repo.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_storage_bucket_iam_member" "runner_source" {
  bucket = google_storage_bucket.source.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_storage_bucket_iam_member" "runner_rw" {
  for_each = {
    cache   = google_storage_bucket.cache.name
    results = google_storage_bucket.results.name
  }
  bucket = each.value
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "scheduler_builds" {
  project = var.project_id
  role    = "roles/cloudbuild.builds.editor"
  member  = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_service_account_iam_member" "scheduler_acts_as_runner" {
  service_account_id = google_service_account.runner.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.scheduler.email}"
}

# --- Nightly run ------------------------------------------------------------------

locals {
  build = {
    source = {
      storageSource = {
        bucket = google_storage_bucket.source.name
        object = local.source_object
      }
    }
    steps = [{
      id         = "nightly"
      name       = "${local.runner_image}:latest"
      entrypoint = "bash"
      args       = ["ci/nightly.sh"]
      env = [
        "CHANNEL=${var.channel}",
        "TRIGGER=scheduler",
        "CLOUD_BUILD_ID=$BUILD_ID",
        "GCP_PROJECT=${var.project_id}",
        "RESULTS_BUCKET=${google_storage_bucket.results.name}",
        "FIRESTORE_DATABASE=${google_firestore_database.db.name}",
        "BAZEL_REMOTE_CACHE=https://storage.googleapis.com/${google_storage_bucket.cache.name}",
        "OUT=/workspace/out",
      ]
    }]
    serviceAccount = google_service_account.runner.id
    timeout        = var.build_timeout
    tags           = [var.name, "nightly"]
    options = {
      logging     = "CLOUD_LOGGING_ONLY"
      machineType = var.machine_type
      diskSizeGb  = 200
    }
  }
}

resource "google_cloud_scheduler_job" "nightly" {
  name        = "${var.name}-nightly"
  region      = var.region
  description = "Nightly adk-monitor run (channel=${var.channel})."
  schedule    = var.schedule
  time_zone   = var.time_zone

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://cloudbuild.googleapis.com/v1/projects/${var.project_id}/locations/${var.region}/builds"
    headers = {
      "Content-Type" = "application/json"
    }
    body = base64encode(jsonencode(local.build))
    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [
    google_project_service.services,
    google_project_iam_member.scheduler_builds,
    google_service_account_iam_member.scheduler_acts_as_runner,
  ]
}
