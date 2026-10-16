variable "project_id" {
  description = "GCP project that hosts adk-monitor."
  type        = string
}

variable "region" {
  description = "Region for Cloud Build, Artifact Registry, buckets and the scheduler."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Prefix for every resource name."
  type        = string
  default     = "adk-monitor"
}

variable "firestore_database" {
  description = "Firestore database id (a named database, so it never collides with (default))."
  type        = string
  default     = "adk-monitor"
}

variable "firestore_location" {
  description = "Firestore location (region or multi-region such as nam5)."
  type        = string
  default     = "us-central1"
}

variable "schedule" {
  description = "Cron schedule for the nightly run."
  type        = string
  default     = "0 4 * * *"
}

variable "time_zone" {
  description = "Time zone of the schedule."
  type        = string
  default     = "Etc/UTC"
}

variable "channel" {
  description = "release = pinned lock files; latest = move every SDK to its newest release first."
  type        = string
  default     = "latest"
  validation {
    condition     = contains(["release", "latest"], var.channel)
    error_message = "channel must be release or latest."
  }
}

variable "machine_type" {
  description = "Cloud Build machine type for the run (Rust builds dominate a cold run)."
  type        = string
  default     = "E2_HIGHCPU_32"
}

variable "build_timeout" {
  description = "Cloud Build timeout for one run."
  type        = string
  default     = "7200s"
}

variable "cache_retention_days" {
  description = "Days to keep Bazel remote-cache entries."
  type        = number
  default     = 30
}
