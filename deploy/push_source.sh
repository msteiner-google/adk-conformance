#!/usr/bin/env bash
# Uploads the committed tree (HEAD) as the source the nightly run builds.
# The depot repo is not reachable from GCP, so this tarball is the hand-off:
# re-run it after every change you want the cloud runs to pick up.
#
#   deploy/push_source.sh <source-bucket> [object]
set -euo pipefail
cd "$(dirname "$0")/.."
bucket="$1"
object="${2:-source/adk-monitor.tar.gz}"
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "warning: uncommitted changes are NOT included (git archive HEAD)" >&2
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
{
  echo "commit=$(git rev-parse HEAD)"
  echo "commit_date=$(git show -s --format=%cI HEAD)"
  echo "subject=$(git show -s --format=%s HEAD)"
  echo "remote=$(git remote get-url origin 2>/dev/null || true)"
} > "$tmp/SOURCE_INFO"
git archive --format=tar.gz --add-file="$tmp/SOURCE_INFO" -o "$tmp/src.tar.gz" HEAD
gcloud storage cp "$tmp/src.tar.gz" "gs://${bucket}/${object}"
echo "uploaded $(git rev-parse --short HEAD) to gs://${bucket}/${object}"
