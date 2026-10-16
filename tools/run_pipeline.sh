#!/usr/bin/env bash
# Runs the whole monitor locally: every scenario on every subject, then the
# cross-SDK comparison. Read the result in out/report/report.md (or parse
# out/report/report.json).
#
#   tools/run_pipeline.sh [--latest] [--fresh] [--out DIR]
#
#   --latest  first move every SDK to its newest release (tools/bump_sdks.py).
#             This rewrites the lock files in place; review with `git diff`,
#             keep them to adopt the new versions, or `git restore` them.
#   --fresh   re-run conformance tests even if Bazel has cached results.
#   --out     output directory (default: ./out).
#
# Test failures are results, not errors: the script exits non-zero only when
# it cannot produce a report.
set -euo pipefail
cd "$(dirname "$0")/.."

channel="release"
out="$PWD/out"
bazel_flags=(--keep_going --test_output=errors)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --latest) channel="latest" ;;
    --fresh) bazel_flags+=(--nocache_test_results) ;;
    --out) out="$(realpath -m "$2")"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

run_id="$(date -u +%Y%m%dT%H%M%SZ)-${channel}"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
mkdir -p "$out"

echo "=== run ${run_id} (channel=${channel})"
if [[ "$channel" == "latest" ]]; then
  python3 tools/bump_sdks.py --out "$out/bump.json"
else
  echo '{}' > "$out/bump.json"
fi
python3 tools/sdk_versions.py > "$out/versions.json"

set +e
bazel test //... "${bazel_flags[@]}"
bazel_exit=$?
set -e
echo "=== bazel exit code: ${bazel_exit} (3 = some tests failed; the report covers them)"

python3 - "$out" "$run_id" "$channel" "$started_at" "$bazel_exit" <<'PY'
import datetime, json, os, subprocess, sys
out, run_id, channel, started, code = sys.argv[1:]
def git(*args):
  try:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
  except (OSError, subprocess.CalledProcessError):
    return ""
meta = {
    "run_id": run_id, "channel": channel, "started_at": started,
    "finished_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "bazel_exit_code": int(code),
    "source": {"commit": git("rev-parse", "--short", "HEAD"),
               "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))},
    "versions": json.load(open(os.path.join(out, "versions.json"))),
    "bump": json.load(open(os.path.join(out, "bump.json"))),
}
json.dump(meta, open(os.path.join(out, "meta.json"), "w"), indent=2, sort_keys=True)
PY

python3 -m harness.compare --in "$(bazel info bazel-testlogs)" --out "$out/report" --meta "$out/meta.json"
echo "=== report: $out/report/report.md"
if [[ "$channel" == "latest" ]]; then
  echo "=== lock files were updated for the latest SDKs; see: git diff --stat"
fi
