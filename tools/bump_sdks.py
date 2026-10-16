"""Moves every in-scope ADK SDK to its latest release (the `latest` channel).

Rewrites the pins and re-resolves the lock files in place:

* Python: google-adk pin in third_party/python/requirements.in, then
  `uv pip compile` for requirements_lock.txt.
* Go: `go get google.golang.org/adk/v2@latest` and `go mod tidy`.
* Rust: the `=X.Y.Z` pins of adk-* crates in hosts/rust/Cargo.toml, then
  `cargo update` for Cargo.lock.

Major-version jumps (for example adk-go v3) are not taken automatically,
since they change import paths. Prints a JSON summary of old and new
versions; a failed bump is reported, not fatal, so the run still measures
the SDKs that did resolve.

  python3 tools/bump_sdks.py [--out bump.json] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import sdk_versions  # pylint: disable=wrong-import-position


def _get_json(url: str) -> dict:
  req = urllib.request.Request(url, headers={"User-Agent": "adk-monitor (bump_sdks)"})
  with urllib.request.urlopen(req, timeout=30) as resp:
    return json.load(resp)


def _run(cmd: list[str], cwd: str = ROOT) -> None:
  print("+", " ".join(cmd), flush=True)
  subprocess.run(cmd, cwd=cwd, check=True)


def _rewrite(rel: str, pattern: str, repl: str) -> int:
  path = os.path.join(ROOT, rel)
  with open(path, encoding="utf-8") as f:
    text = f.read()
  new, count = re.subn(pattern, repl, text, flags=re.M)
  with open(path, "w", encoding="utf-8") as f:
    f.write(new)
  return count


def bump_python(dry_run: bool) -> str:
  latest = _get_json("https://pypi.org/pypi/google-adk/json")["info"]["version"]
  if dry_run:
    return latest
  _rewrite("third_party/python/requirements.in", r"^google-adk==\S+$", f"google-adk=={latest}")
  _run(["uv", "pip", "compile", "third_party/python/requirements.in", "--generate-hashes",
        "--python-version", "3.13", "--python-platform", "x86_64-unknown-linux-gnu",
        "--upgrade-package", "google-adk", "-o", "third_party/python/requirements_lock.txt"])
  return latest


def bump_go(dry_run: bool) -> str:
  latest = _get_json("https://proxy.golang.org/google.golang.org/adk/v2/@latest")["Version"]
  if dry_run:
    return latest
  _run(["go", "get", f"google.golang.org/adk/v2@{latest}"])
  _run(["go", "mod", "tidy"])
  return latest


def bump_rust(dry_run: bool) -> str:
  crate = _get_json("https://crates.io/api/v1/crates/adk-server")["crate"]
  latest = crate.get("max_stable_version") or crate["max_version"]
  if dry_run:
    return latest
  count = _rewrite("hosts/rust/Cargo.toml", r'^(adk-[a-z-]+ = \{ version = )"=[^"]+"',
                   rf'\1"={latest}"')
  if not count:
    raise RuntimeError("no adk-* pins found in hosts/rust/Cargo.toml")
  _run(["cargo", "update", "--manifest-path", "hosts/rust/Cargo.toml"])
  return latest


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--out", help="write the JSON summary here too")
  parser.add_argument("--dry-run", action="store_true", help="only report latest versions")
  args = parser.parse_args(argv)
  before = sdk_versions.versions()
  summary = {}
  for subject, bump in (("python", bump_python), ("go", bump_go), ("rust", bump_rust)):
    try:
      summary[subject] = {"latest": bump(args.dry_run), "ok": True}
    except Exception as e:  # pylint: disable=broad-except
      summary[subject] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
  after = sdk_versions.versions()
  for subject in summary:
    summary[subject]["before"] = before.get(subject, {})
    summary[subject]["after"] = after.get(subject, {})
  text = json.dumps(summary, indent=2, sort_keys=True)
  print(text)
  if args.out:
    with open(args.out, "w", encoding="utf-8") as f:
      f.write(text + "\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
