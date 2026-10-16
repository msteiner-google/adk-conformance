"""Publishes one run's results: artifacts to GCS, summary documents to Firestore.

Standard library only (REST APIs), so it runs in the Cloud Build runner image
and on a workstation alike. Credentials, in order: $ACCESS_TOKEN, the GCE/Cloud
Build metadata server, then `gcloud auth print-access-token`.

Firestore layout (database --database, default "adk-monitor"):

  runs/{run_id}                      run metadata, SDK versions, status counts
  runs/{run_id}/results/{test}       one document per (scenario, subject)
  findings/{fingerprint}             one document per divergence, updated
                                     every run it is seen (first/last seen)

GCS layout: gs://{bucket}/runs/{run_id}/{report.md,report.json,transcripts/...}

  python3 tools/publish_results.py --project P --bucket B --run-id R \
      --report out/report --transcripts bazel-testlogs [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_METADATA_TOKEN = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                   "service-accounts/default/token")
_MAX_STRING = 20_000  # keep documents far below Firestore's 1 MiB limit


def access_token() -> str:
  if os.environ.get("ACCESS_TOKEN"):
    return os.environ["ACCESS_TOKEN"]
  try:
    req = urllib.request.Request(_METADATA_TOKEN, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=3) as resp:
      return json.load(resp)["access_token"]
  except OSError:
    pass
  return subprocess.run(["gcloud", "auth", "print-access-token"], check=True,
                        capture_output=True, text=True).stdout.strip()


class Client:
  """Minimal authenticated JSON-over-HTTP client with retries."""

  def __init__(self, token: str, dry_run: bool = False):
    self.token = token
    self.dry_run = dry_run

  def call(self, method: str, url: str, body: Any = None, data: bytes | None = None,
           content_type: str = "application/json") -> Any:
    if self.dry_run:
      print(f"[dry-run] {method} {url}")
      return {}
    payload = data if data is not None else (json.dumps(body).encode() if body is not None else None)
    for attempt in range(5):
      req = urllib.request.Request(url, data=payload, method=method)
      req.add_header("Authorization", f"Bearer {self.token}")
      if payload is not None:
        req.add_header("Content-Type", content_type)
      try:
        with urllib.request.urlopen(req, timeout=60) as resp:
          text = resp.read().decode()
          return json.loads(text) if text else {}
      except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if e.code in (429, 500, 502, 503, 504) and attempt < 4:
          time.sleep(2 ** attempt)
          continue
        raise RuntimeError(f"{method} {url} -> {e.code}: {detail[:500]}") from e
    raise RuntimeError("unreachable")


# Firestore value encoding -------------------------------------------------------


def to_value(value: Any) -> dict[str, Any]:
  if value is None:
    return {"nullValue": None}
  if isinstance(value, bool):
    return {"booleanValue": value}
  if isinstance(value, int):
    return {"integerValue": str(value)}
  if isinstance(value, float):
    return {"doubleValue": value}
  if isinstance(value, datetime.datetime):
    return {"timestampValue": value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
  if isinstance(value, str):
    return {"stringValue": value if len(value) <= _MAX_STRING else value[:_MAX_STRING] + "…"}
  if isinstance(value, (list, tuple)):
    return {"arrayValue": {"values": [to_value(v) for v in value]}}
  if isinstance(value, dict):
    return {"mapValue": {"fields": {str(k): to_value(v) for k, v in value.items()}}}
  return to_value(str(value))


def doc_id(text: str) -> str:
  return text.replace("/", "_")


class Publisher:
  """Writes a run's artifacts and documents."""

  def __init__(self, client: Client, project: str, database: str, bucket: str, run_id: str):
    self.client = client
    self.project = project
    self.bucket = bucket
    self.run_id = run_id
    self.db_root = f"projects/{project}/databases/{database}/documents"
    self.writes: list[dict[str, Any]] = []

  def upload(self, local: str, name: str, content_type: str) -> str:
    with open(local, "rb") as f:
      data = f.read()
    url = (f"https://storage.googleapis.com/upload/storage/v1/b/{self.bucket}/o?uploadType=media&name="
           + urllib.parse.quote(name, safe=""))
    self.client.call("POST", url, data=data, content_type=content_type)
    return f"gs://{self.bucket}/{name}"

  def set_doc(self, path: str, fields: dict[str, Any], merge: bool = False,
              transforms: list[dict[str, Any]] | None = None) -> None:
    write: dict[str, Any] = {
        "update": {"name": f"{self.db_root}/{path}",
                   "fields": {k: to_value(v) for k, v in fields.items()}},
    }
    if merge:
      write["updateMask"] = {"fieldPaths": [f"`{k}`" if not k.isidentifier() else k for k in fields]}
    if transforms:
      write["updateTransforms"] = transforms
    self.writes.append(write)

  def flush(self) -> None:
    url = f"https://firestore.googleapis.com/v1/{self.db_root.rsplit('/documents', 1)[0]}/documents:commit"
    for i in range(0, len(self.writes), 400):
      self.client.call("POST", url, {"writes": self.writes[i:i + 400]})
    self.writes.clear()


def find_files(root: str, suffixes: tuple[str, ...]) -> list[str]:
  out = []
  for dirpath, _, files in os.walk(root, followlinks=True):
    out.extend(os.path.join(dirpath, f) for f in files if f.endswith(suffixes))
  return sorted(out)


def publish(args: argparse.Namespace) -> dict[str, Any]:
  with open(os.path.join(args.report, "report.json"), encoding="utf-8") as f:
    report = json.load(f)
  pub = Publisher(Client(access_token() if not args.dry_run else "", args.dry_run),
                  args.project, args.database, args.bucket, args.run_id)
  prefix = f"runs/{args.run_id}"
  now = datetime.datetime.now(datetime.timezone.utc)

  report_uri = pub.upload(os.path.join(args.report, "report.md"), f"{prefix}/report.md", "text/markdown")
  pub.upload(os.path.join(args.report, "report.json"), f"{prefix}/report.json", "application/json")
  uris: dict[str, str] = {}
  for path in find_files(args.transcripts, (".transcript.json", ".host.log")):
    name = os.path.basename(path)
    kind = "application/json" if name.endswith(".json") else "text/plain"
    uri = pub.upload(path, f"{prefix}/transcripts/{name}", kind)
    if name.endswith(".transcript.json"):
      uris[name[: -len(".transcript.json")]] = uri

  meta = report.get("meta") or {}
  totals: dict[str, int] = {}
  for counts in report["counts"].values():
    for status, n in counts.items():
      totals[status] = totals.get(status, 0) + n
  pub.set_doc(f"runs/{doc_id(args.run_id)}", {
      "run_id": args.run_id,
      "published_at": now,
      "channel": meta.get("channel", ""),
      "trigger": meta.get("trigger", ""),
      "source": meta.get("source", {}),
      "versions": meta.get("versions", {}),
      "bump": meta.get("bump", {}),
      "bazel_exit_code": meta.get("bazel_exit_code"),
      "subjects": report["subjects"],
      "counts": report["counts"],
      "totals": totals,
      "finding_count": len(report["findings"]),
      "finding_fingerprints": [f["fingerprint"] for f in report["findings"]],
      "report_uri": report_uri,
  })
  for scenario, row in report["matrix"].items():
    for subject, cell in row["subjects"].items():
      key = f"{scenario}.{subject}"
      pub.set_doc(f"runs/{doc_id(args.run_id)}/results/{doc_id(key)}", {
          "scenario": scenario,
          "subject": subject,
          "area": row["area"],
          "behaviors": row["behaviors"],
          "status": cell["status"],
          "failures": cell["failures"][:20],
          "not_implemented": cell["not_implemented"],
          "known_divergence": (cell.get("known_divergence") or {}).get("reason", ""),
          "transcript_uri": uris.get(key, ""),
          "run_id": args.run_id,
          "published_at": now,
      })
  epoch = int(now.timestamp())
  for finding in report["findings"]:
    pub.set_doc(f"findings/{finding['fingerprint']}", {
        "fingerprint": finding["fingerprint"],
        "scenario": finding["scenario"],
        "aspect": finding["aspect"],
        "kind": finding["kind"],
        "outliers": finding["outliers"],
        "majority": finding["majority"],
        "groups": [{"subjects": g} for g in finding["groups"]],
        "details_json": json.dumps(finding["details"], ensure_ascii=False, default=str),
        "last_seen_run": args.run_id,
        "last_seen_at": now,
    }, merge=True, transforms=[
        {"fieldPath": "first_seen_epoch", "minimum": {"integerValue": str(epoch)}},
        {"fieldPath": "seen_count", "increment": {"integerValue": "1"}},
    ])
  pub.flush()
  return {"run_id": args.run_id, "report_uri": report_uri, "results": len(report["matrix"]),
          "findings": len(report["findings"]), "transcripts": len(uris)}


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--project", required=True)
  parser.add_argument("--database", default="adk-monitor")
  parser.add_argument("--bucket", required=True, help="GCS bucket for run artifacts")
  parser.add_argument("--run-id", required=True)
  parser.add_argument("--report", required=True, help="directory with report.json and report.md")
  parser.add_argument("--transcripts", required=True, help="directory tree with *.transcript.json")
  parser.add_argument("--dry-run", action="store_true")
  args = parser.parse_args(argv)
  print(json.dumps(publish(args), indent=2))
  return 0


if __name__ == "__main__":
  sys.exit(main())
