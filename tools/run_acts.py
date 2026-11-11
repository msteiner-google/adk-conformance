"""Runs ACTS, the A2A conformance corpus, through a2a-itk against each A2A SDK.

Behaviors A2A-SDK-001 (the ACTS MUST corpus passes at the version the ADK
pins) and A2A-SDK-004 (which MUST failures an upgrade to the latest release
would fix). For each A2A SDK the tool checks out the SDK repository at the
pinned release and at the latest release, and runs a2a-itk's ``run_acts.py``
against the SDK's own ``itk/`` agent on every binding:

  a2a-python  pinned from third_party/python/requirements_lock.txt
  a2a-go      pinned from go.mod (the version adk-go resolves)
  a2a-rs      reference SDK for adk-rust (which implements A2A itself);
              the a2a-server-lf version in hosts/a2a-rs/Cargo.lock

a2a-itk is pinned to ``A2A_ITK_REF``. Checkouts are cached under
``~/.cache/adk-monitor/acts``. Needs network access, uv, and the SDK's
toolchain (go, cargo); a failed SDK is recorded, not fatal.

  python3 tools/run_acts.py --out out/acts [--sdk a2a-go] [--no-latest] [--transport jsonrpc]

Output: ``<out>/acts.json`` (summary, MUST failures per binding, the
pinned-vs-latest delta) and the raw section-13 reports under
``<out>/<sdk>-<version>/``.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from harness import subjects  # pylint: disable=wrong-import-position
CACHE = os.path.expanduser("~/.cache/adk-monitor/acts")
A2A_ITK_REPO = "https://github.com/a2aproject/a2a-itk"
A2A_ITK_REF = "065fbb86ce0c367012d81353937e8123b24f9f40"  # main on 2026-10-15
# a2a-itk generates the ITK agent's proto stubs before starting it (Go needs
# protoc and its Go plugins on PATH). Fetched into the cache when missing.
PROTOC_VERSION = "36.2"
GO_PLUGINS = ("google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12",
              "google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2")

SDKS = {
    "a2a-python": {"repo": "https://github.com/a2aproject/a2a-python", "language": "python",
                   "subject": "python", "tag": "v{version}"},
    "a2a-go": {"repo": "https://github.com/a2aproject/a2a-go", "language": "go",
               "subject": "go", "tag": "{version}"},
    "a2a-rs": {"repo": "https://github.com/a2aproject/a2a-rs", "language": "rust",
               "subject": "rust", "tag": "a2a-server-lf-v{version}"},  # per-crate tags
}


def _read(rel: str) -> str:
  path = os.path.join(ROOT, rel)
  return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def pinned(sdk: str) -> str | None:
  if sdk == "a2a-python":
    m = re.search(r"^a2a-sdk==([^\s\\]+)", _read("third_party/python/requirements_lock.txt"), re.M)
  elif sdk == "a2a-go":
    m = re.search(r"^\s*github\.com/a2aproject/a2a-go/v2\s+(v\S+)", _read("go.mod"), re.M)
  else:
    m = re.search(r'name = "a2a-server-lf"\nversion = "([^"]+)"', _read("hosts/a2a-rs/Cargo.lock"))
  return m.group(1) if m else None


def latest(sdk: str) -> str | None:
  def get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "adk-monitor (run_acts)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
      return json.load(resp)
  try:
    if sdk == "a2a-python":
      return get("https://pypi.org/pypi/a2a-sdk/json")["info"]["version"]
    if sdk == "a2a-go":
      return get("https://proxy.golang.org/github.com/a2aproject/a2a-go/v2/@latest")["Version"]
    if sdk == "a2a-rs":
      return get("https://crates.io/api/v1/crates/a2a-server-lf")["crate"]["max_stable_version"]
  except Exception as e:  # pylint: disable=broad-except
    print(f"latest {sdk}: {e}", file=sys.stderr)
  return None


def _git(args: list[str], cwd: str | None = None) -> str:
  return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def checkout(repo: str, ref: str, dest: str) -> None:
  """Shallow checkout of ``ref`` (tag, branch or commit) into ``dest``."""
  if not os.path.isdir(os.path.join(dest, ".git")):
    os.makedirs(dest, exist_ok=True)
    _git(["init", "-q"], dest)
    _git(["remote", "add", "origin", repo], dest)
  _git(["fetch", "-q", "--depth", "1", "origin", ref], dest)
  _git(["checkout", "-q", "--force", "FETCH_HEAD"], dest)


def resolve_tag(sdk: str, version: str | None) -> str:
  info = SDKS[sdk]
  if version and info["tag"]:
    return info["tag"].format(version=version)
  if version:
    tags = _git(["ls-remote", "--tags", info["repo"]])
    for line in tags.splitlines():
      tag = line.split("refs/tags/")[-1].removesuffix("^{}")
      if tag.endswith(f"v{version}") or tag == version:
        return tag
  return "main"


def ensure_codegen_tools() -> str:
  """A bin/ dir with protoc and the Go protoc plugins; returned for PATH."""
  import io  # pylint: disable=import-outside-toplevel
  import shutil  # pylint: disable=import-outside-toplevel
  import zipfile  # pylint: disable=import-outside-toplevel
  bin_dir = os.path.join(CACHE, "bin")
  os.makedirs(bin_dir, exist_ok=True)
  if not shutil.which("protoc") and not os.path.exists(os.path.join(bin_dir, "protoc")):
    url = (f"https://github.com/protocolbuffers/protobuf/releases/download/v{PROTOC_VERSION}/"
           f"protoc-{PROTOC_VERSION}-linux-x86_64.zip")
    req = urllib.request.Request(url, headers={"User-Agent": "adk-monitor (run_acts)"})
    with urllib.request.urlopen(req, timeout=120) as resp:
      archive = zipfile.ZipFile(io.BytesIO(resp.read()))
    root = os.path.join(CACHE, f"protoc-{PROTOC_VERSION}")
    archive.extractall(root)
    os.chmod(os.path.join(root, "bin", "protoc"), 0o755)
    os.symlink(os.path.join(root, "bin", "protoc"), os.path.join(bin_dir, "protoc"))
  for plugin in GO_PLUGINS:
    name = plugin.split("/")[-1].split("@")[0]
    if not shutil.which(name) and not os.path.exists(os.path.join(bin_dir, name)):
      subprocess.run(["go", "install", plugin], check=True, env=dict(os.environ, GOBIN=bin_dir))
  return bin_dir


def summarize(report: dict[str, Any]) -> dict[str, Any]:
  tests = [t for suite in report.get("suites") or [] for t in suite.get("tests") or []]
  must = (report.get("summary") or {}).get("by_level", {}).get("must", {})
  return {
      "conformant": must.get("failed", 1) == 0 and must.get("errors", 1) == 0,
      "summary": report.get("summary"),
      "must_failures": sorted(t["id"] for t in tests
                              if t.get("level") == "must" and t.get("result") in ("fail", "error")),
      "failures": {t["id"]: (t.get("failure") or {}).get("message", t.get("result"))
                   for t in tests if t.get("result") in ("fail", "error")},
  }


def run_one(sdk: str, version: str | None, out_dir: str, transports: list[str], itk_dir: str) -> dict[str, Any]:
  ref = resolve_tag(sdk, version)
  src = os.path.join(CACHE, "sdks", f"{sdk}-{ref}")
  entry: dict[str, Any] = {"version": version, "ref": ref}
  try:
    checkout(SDKS[sdk]["repo"], ref, src)
  except subprocess.CalledProcessError as e:
    entry["error"] = f"checkout {ref} failed: {e.stderr.strip()[:300]}"
    return entry
  if not os.path.isdir(os.path.join(src, "itk")):
    entry["error"] = f"{sdk}@{ref} has no itk/ agent"
    return entry
  dest = os.path.join(out_dir, f"{sdk}-{version or ref}")
  os.makedirs(dest, exist_ok=True)
  for old in glob.glob(os.path.join(dest, "acts-report-*.json")):
    os.remove(old)
  cmd = ["uv", "run", "run_acts.py", "--mount", os.path.join(src, "itk"), "--sdk", sdk,
         "--sdk-version", version or ref, "--language", SDKS[sdk]["language"],
         "--repository", SDKS[sdk]["repo"], "--out", dest]
  for transport in transports:
    cmd += ["--transport", transport]
  print("+", " ".join(cmd), flush=True)
  # UV_NO_CONFIG: honor each repo's uv.lock as published. A machine-level uv
  # config (e.g. a corporate default index) makes `uv run --locked` in the SDK's
  # itk/ agent reject the lock and the agent never starts.
  env = dict(os.environ, PATH=ensure_codegen_tools() + os.pathsep + os.environ.get("PATH", ""),
             UV_NO_CONFIG="1")
  proc = subprocess.run(cmd, cwd=itk_dir, capture_output=True, text=True, check=False, env=env)
  with open(os.path.join(dest, "run_acts.log"), "w", encoding="utf-8") as f:
    f.write(proc.stdout + "\n" + proc.stderr)
  entry["exit_code"] = proc.returncode
  entry["bindings"] = {}
  for path in sorted(glob.glob(os.path.join(dest, "acts-report-*.json"))):
    with open(path, encoding="utf-8") as f:
      report = json.load(f)
    entry["bindings"][report.get("transport", os.path.basename(path))] = summarize(report)
  if not entry["bindings"]:
    entry["error"] = f"no report (exit {proc.returncode}); see {dest}/run_acts.log"
  else:
    entry["conformant"] = all(b["conformant"] for b in entry["bindings"].values())
  return entry


def delta(pinned_run: dict[str, Any], latest_run: dict[str, Any]) -> dict[str, Any]:
  """A2A-SDK-004: MUST failures an upgrade would fix, and new ones it would add."""
  out = {}
  for binding, before in (pinned_run.get("bindings") or {}).items():
    after = (latest_run.get("bindings") or {}).get(binding)
    if after is None:
      continue
    out[binding] = {
        "fixed_by_upgrade": sorted(set(before["must_failures"]) - set(after["must_failures"])),
        "new_in_latest": sorted(set(after["must_failures"]) - set(before["must_failures"])),
    }
  return out


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--out", required=True)
  parser.add_argument("--sdk", action="append", choices=sorted(SDKS), help="default: all")
  parser.add_argument("--transport", action="append", help="jsonrpc, grpc, rest or all (default all)")
  parser.add_argument("--no-latest", action="store_true", help="skip the latest-release run")
  parser.add_argument("--itk-ref", default=A2A_ITK_REF)
  args = parser.parse_args(argv)
  out_dir = os.path.abspath(args.out)
  os.makedirs(out_dir, exist_ok=True)
  itk_dir = os.path.join(CACHE, "a2a-itk")
  checkout(A2A_ITK_REPO, args.itk_ref, itk_dir)
  summary_path = os.path.join(out_dir, "acts.json")
  previous: dict[str, Any] = {}
  if args.sdk and os.path.exists(summary_path):  # a subset run updates the other SDKs' results
    with open(summary_path, encoding="utf-8") as f:
      previous = json.load(f).get("sdks", {})
  result: dict[str, Any] = {"a2a_itk": {"repo": A2A_ITK_REPO, "ref": args.itk_ref},
                            "sdks": previous}
  for sdk in args.sdk or subjects.active(sorted(SDKS)):
    version = pinned(sdk)
    entry: dict[str, Any] = {"subject": SDKS[sdk]["subject"],
                             "pinned": run_one(sdk, version, out_dir, args.transport or ["all"], itk_dir)}
    newest = None if args.no_latest else latest(sdk)
    if newest and newest != version:
      entry["latest"] = run_one(sdk, newest, out_dir, args.transport or ["all"], itk_dir)
      entry["delta"] = delta(entry["pinned"], entry["latest"])
    elif newest:
      entry["latest_note"] = f"pinned {version} is the latest release"
    result["sdks"][sdk] = entry
    pin = entry["pinned"]
    status = pin.get("error") or ("conformant" if pin.get("conformant") else "NOT conformant: " + ", ".join(
        sorted({i for b in pin["bindings"].values() for i in b["must_failures"]})))
    print(f"{sdk}@{pin.get('ref')}: {status}", flush=True)
  with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, sort_keys=True)
  print(f"wrote {out_dir}/acts.json")
  return 0


if __name__ == "__main__":
  sys.exit(main())
