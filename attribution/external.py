"""Attribution experiment 5: a2a-itk's nightly ITK and ACTS results.

Each A2A SDK repository publishes its nightly results as release assets
(``nightly-metrics``), the same files the a2a-itk dashboard renders:

  https://github.com/a2aproject/<repo>/releases/download/nightly-metrics/acts_<lang>.json
  https://github.com/a2aproject/<repo>/releases/download/nightly-metrics/itk_<lang>.json

Each file is a rolling list of nights; the newest entry is used. Nightlies
test the SDK's main branch (``commit_sha``), not a release, so a match is
corroborating evidence, not proof. Needs network access; a failed fetch is
recorded, not fatal.

  python3 -m attribution.external --out out/external.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from typing import Any

from harness import subjects

BASE = "https://github.com/a2aproject/{repo}/releases/download/nightly-metrics/{asset}"
# ADK subject -> (A2A SDK repo, asset suffix, same SDK as the ADK uses?)
SDKS = {
    "python": ("a2a-python", "python", True),
    "go": ("a2a-go", "go", True),
    "rust": ("a2a-rs", "rust", False),
}
# Behavior catalog prefixes -> ACTS test-id prefixes that test the same thing.
BEHAVIOR_TO_ACTS = {
    "A2A-SRV-001": ["CARD-DISC"], "A2A-SRV-002": ["CARD-DISC", "CARD-SCHEMA"],
    "A2A-SRV-005": ["CORE-CAP"], "A2A-SRV-006": ["CARD-DISC", "TRANS-"],
    "A2A-SRV-007": ["VER-NEG"], "A2A-SRV-008": ["CORE-SEND"],
    "A2A-SRV-010": ["CORE-SEND", "STREAM-"], "A2A-SRV-011": ["STREAM-"],
    "A2A-SRV-012": ["STREAM-"], "A2A-SRV-014": ["MULTI-"], "A2A-SRV-013": ["MULTI-"],
    "A2A-SRV-016": ["MULTI-"], "A2A-SRV-019": ["CORE-SEND", "ERR-"],
    "A2A-SRV-025": ["CORE-GET", "CORE-HIST"], "A2A-SRV-026": ["CORE-GET", "ERR-"],
    "A2A-SRV-027": ["CORE-CANCEL"], "A2A-SRV-028": ["CORE-LIST"], "A2A-SRV-029": ["STREAM-SUB"],
    "A2A-SRV-031": ["JSONRPC-", "ERR-"], "A2A-SRV-032": ["DM-FMT", "WIRE-"],
}


def _fetch(url: str) -> Any:
  req = urllib.request.Request(url, headers={"User-Agent": "adk-monitor (attribution)"})
  with urllib.request.urlopen(req, timeout=30) as resp:
    return json.load(resp)


def summarize_acts(night: dict[str, Any]) -> dict[str, Any]:
  must, other = {}, {}
  for transport, result in (night.get("results") or {}).items():
    failed = [t for t in result.get("tests") or [] if t.get("result") in ("fail", "error")]
    must[transport] = sorted(t["id"] for t in failed if t.get("level") == "must")
    other[transport] = sorted(t["id"] for t in failed if t.get("level") != "must")
  return {
      "timestamp": night.get("timestamp"),
      "commit": (night.get("commit_sha") or "")[:12],
      "sdk_version": night.get("sdk_version"),
      "conformant": night.get("conformant"),
      "must_failures": must,
      "other_failures": other,
  }


def summarize_itk(night: dict[str, Any]) -> dict[str, Any]:
  failed = [s.get("name") for s in night.get("scenarios") or [] if not s.get("passed")]
  return {"timestamp": night.get("timestamp"), "commit": (night.get("commit_sha") or "")[:12],
          "all_passed": night.get("all_passed"), "failed": failed}


def collect(fetch=_fetch) -> dict[str, Any]:
  out: dict[str, Any] = {}
  for subject in subjects.active(SDKS):
    repo, suffix, same_sdk = SDKS[subject]
    entry: dict[str, Any] = {"sdk": repo, "same_sdk_as_adk": same_sdk}
    for kind in ("acts", "itk"):
      url = BASE.format(repo=repo, asset=f"{kind}_{suffix}.json")
      try:
        nights = fetch(url)
        if nights:
          entry[kind] = (summarize_acts if kind == "acts" else summarize_itk)(nights[-1])
        else:
          entry[kind] = {"error": "no nights published"}
      except Exception as e:  # pylint: disable=broad-except
        entry[kind] = {"error": f"{type(e).__name__}: {e}", "url": url}
    acts, itk = entry.get("acts") or {}, entry.get("itk") or {}
    parts = []
    if "conformant" in acts:
      must = sorted({i for ids in acts["must_failures"].values() for i in ids})
      parts.append(f"ACTS {'conformant' if acts['conformant'] else 'NOT conformant'} at main@"
                   f"{acts['commit']} (MUST failures: {', '.join(must) or 'none'})")
    if "all_passed" in itk:
      parts.append(f"ITK {'all passed' if itk['all_passed'] else str(len(itk['failed'])) + ' failed'}")
    entry["summary"] = "; ".join(parts) or "no nightly data"
    out[subject] = entry
  return out


def matches(external: dict[str, Any] | None, behaviors: list[str]) -> list[str]:
  """Failing nightly ACTS test ids that test one of ``behaviors``."""
  if not external or "acts" not in external or "must_failures" not in external["acts"]:
    return []
  failing = set()
  for ids in list(external["acts"]["must_failures"].values()) + list(
      external["acts"]["other_failures"].values()):
    failing.update(ids)
  prefixes = [p for b in behaviors for p in BEHAVIOR_TO_ACTS.get(b, [])]
  return sorted(i for i in failing if any(i.startswith(p) for p in prefixes))


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--out", required=True)
  args = parser.parse_args(argv)
  result = {"sdks": collect(), "behavior_to_acts": BEHAVIOR_TO_ACTS}
  with open(args.out, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, sort_keys=True)
  for subject, entry in result["sdks"].items():
    print(f"{subject}: {entry['sdk']}: {entry['summary']}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
