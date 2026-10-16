#!/usr/bin/env bash
# One monitoring run: (optionally) bump SDKs, run every scenario on every
# subject, compare, publish. Runs in the runner image on Cloud Build, or
# locally from the repo root.
#
# Environment:
#   CHANNEL             release (pinned locks, default) | latest (bump first)
#   RUN_ID              defaults to a UTC timestamp
#   TRIGGER             free text recorded with the run (e.g. scheduler, manual)
#   OUT                 output dir (default ./out)
#   BAZEL_REMOTE_CACHE  e.g. https://storage.googleapis.com/<bucket>; optional
#   GCP_PROJECT, RESULTS_BUCKET, FIRESTORE_DATABASE  publish when all set
#
# Test failures are results, not errors: the script exits non-zero only when
# it cannot produce or publish a report.
set -euo pipefail
cd "$(dirname "$0")/.."

CHANNEL="${CHANNEL:-release}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-${CHANNEL}}"
TRIGGER="${TRIGGER:-manual}"
OUT="${OUT:-$PWD/out}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$OUT"

bazel_flags=(--keep_going --test_output=errors --nocache_test_results)
if [[ -n "${BAZEL_REMOTE_CACHE:-}" ]]; then
  bazel_flags+=(--remote_cache="$BAZEL_REMOTE_CACHE" --google_default_credentials)
fi

echo "=== run ${RUN_ID} (channel=${CHANNEL})"
if [[ "$CHANNEL" == "latest" ]]; then
  python3 tools/bump_sdks.py --out "$OUT/bump.json"
else
  echo '{}' > "$OUT/bump.json"
fi
python3 tools/sdk_versions.py > "$OUT/versions.json"

set +e
bazel test //... "${bazel_flags[@]}"
bazel_exit=$?
set -e
echo "=== bazel exit code: ${bazel_exit} (3 = some tests failed; results are still published)"

testlogs="$(bazel info bazel-testlogs)"
python3 - "$OUT" "$RUN_ID" "$CHANNEL" "$TRIGGER" "$STARTED_AT" "$bazel_exit" <<'PY'
import json, os, sys, datetime
out, run_id, channel, trigger, started, code = sys.argv[1:]
source = {}
if os.path.exists("SOURCE_INFO"):
  for line in open("SOURCE_INFO"):
    if "=" in line:
      k, v = line.strip().split("=", 1)
      source[k] = v
meta = {
    "run_id": run_id, "channel": channel, "trigger": trigger, "started_at": started,
    "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "bazel_exit_code": int(code), "source": source,
    "build_id": os.environ.get("CLOUD_BUILD_ID", ""),
    "versions": json.load(open(os.path.join(out, "versions.json"))),
    "bump": json.load(open(os.path.join(out, "bump.json"))),
}
json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=2, sort_keys=True)
PY

python3 -m harness.compare --in "$testlogs" --out "$OUT/report" --meta "$OUT/meta.json"

if [[ -n "${GCP_PROJECT:-}" && -n "${RESULTS_BUCKET:-}" ]]; then
  python3 tools/publish_results.py --project "$GCP_PROJECT" \
    --database "${FIRESTORE_DATABASE:-adk-monitor}" --bucket "$RESULTS_BUCKET" \
    --run-id "$RUN_ID" --report "$OUT/report" --transcripts "$testlogs"
else
  echo "=== publishing skipped (GCP_PROJECT/RESULTS_BUCKET not set); report in $OUT/report"
fi
