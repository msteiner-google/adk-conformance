"""Attribution experiments 2 and 3: the latest A2A SDK release.

* **Version probe (2):** the bare A2A host built against the latest release
  of its A2A SDK.
* **Dependency swap (3):** the ADK host rebuilt against that release.

For every language whose pinned A2A SDK (the version the ADK resolves) is not
the latest release, this copies the workspace to a scratch directory, moves
only the A2A SDK to the latest release there, and runs the A2A server
scenarios on the ADK host and its bare host. Transcripts land in
``<out>/transcripts``; ``<out>/latest.json`` records what was swapped and why
not, for the attribution engine.

The checkout itself is never modified. The scratch workspace lives under
``~/.cache/adk-monitor/latest-a2a`` and keeps its Bazel output base between
runs, so later runs only rebuild what changed.

  python3 -m attribution.latest --out out/latest [--dry-run] [--latest python=1.2.1]

Needs network access (package registries) and the toolchains bump_sdks.py
needs (uv, go, cargo).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.expanduser("~/.cache/adk-monitor/latest-a2a")
SCENARIO_PACKAGES = ["//scenarios/a2a_srv/..."]

# ADK subject -> (A2A SDK, bare host subject)
SDKS = {
    "python": ("a2a-sdk", "a2a-python"),
    "go": ("github.com/a2aproject/a2a-go/v2", "a2a-go"),
    "rust": ("a2a-protocol-types", "a2a-rs"),
}


def _get_json(url: str) -> dict[str, Any]:
  req = urllib.request.Request(url, headers={"User-Agent": "adk-monitor (attribution)"})
  with urllib.request.urlopen(req, timeout=30) as resp:
    return json.load(resp)


def pinned_versions(root: str = ROOT) -> dict[str, str | None]:
  def read(rel: str) -> str:
    path = os.path.join(root, rel)
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""
  py = re.search(r"^a2a-sdk==([^\s\\]+)", read("third_party/python/requirements_lock.txt"), re.M)
  go = re.search(r"^\s*github\.com/a2aproject/a2a-go/v2\s+(v\S+)", read("go.mod"), re.M)
  rs = re.search(r'name = "a2a-protocol-types"\nversion = "([^"]+)"', read("hosts/rust/Cargo.lock"))
  return {"python": py and py.group(1), "go": go and go.group(1), "rust": rs and rs.group(1)}


def latest_versions() -> dict[str, str | None]:
  out: dict[str, str | None] = {}
  for lang, fetch in (
      ("python", lambda: _get_json("https://pypi.org/pypi/a2a-sdk/json")["info"]["version"]),
      ("go", lambda: _get_json("https://proxy.golang.org/github.com/a2aproject/a2a-go/v2/@latest")["Version"]),
      ("rust", lambda: _get_json("https://crates.io/api/v1/crates/a2a-protocol-types")["crate"]["max_stable_version"]),
  ):
    try:
      out[lang] = fetch()
    except Exception as e:  # pylint: disable=broad-except
      print(f"latest {lang}: {e}", file=sys.stderr)
      out[lang] = None
  return out


def semver_compatible(pinned: str, latest: str) -> bool:
  """Cargo caret rule: same major, or same minor while the major is 0."""
  p = [int(x) for x in re.findall(r"\d+", pinned)[:3]]
  l = [int(x) for x in re.findall(r"\d+", latest)[:3]]
  if not p or not l:
    return False
  return p[0] == l[0] and (p[0] != 0 or p[1] == l[1])


def _run(cmd: list[str], cwd: str) -> None:
  print("+", " ".join(cmd), flush=True)
  subprocess.run(cmd, cwd=cwd, check=True)


def prepare_workspace(scratch: str = SCRATCH) -> str:
  """Copies the checkout (tracked + untracked, not ignored files) to scratch/ws."""
  ws = os.path.join(scratch, "ws")
  files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT,
                         check=True, capture_output=True).stdout.decode().split("\0")
  keep = set()
  for rel in filter(None, files):
    src = os.path.join(ROOT, rel)
    if not os.path.isfile(src):
      continue
    dst = os.path.join(ws, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    keep.add(rel)
  for dirpath, _, names in os.walk(ws):
    if "/bazel-" in dirpath or "/.git" in dirpath:
      continue
    for name in names:
      rel = os.path.relpath(os.path.join(dirpath, name), ws)
      if rel not in keep and not rel.startswith("bazel-"):
        os.remove(os.path.join(dirpath, name))
  return ws


def bump(lang: str, version: str, ws: str) -> None:
  if lang == "python":
    path = os.path.join(ws, "third_party/python/requirements.in")
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"^a2a-sdk(\[[^\]]*\])?==\S+$", lambda m: f"a2a-sdk{m.group(1) or ''}=={version}",
                  text, flags=re.M)
    open(path, "w", encoding="utf-8").write(text)
    _run(["uv", "pip", "compile", "third_party/python/requirements.in", "--generate-hashes",
          "--python-version", "3.13", "--python-platform", "x86_64-unknown-linux-gnu",
          "--upgrade-package", "a2a-sdk", "--quiet", "-o", "third_party/python/requirements_lock.txt"], ws)
  elif lang == "go":
    adk_before = re.search(r"google\.golang\.org/adk/v2 (v\S+)", open(os.path.join(ws, "go.mod")).read())
    _run(["go", "get", f"github.com/a2aproject/a2a-go/v2@{version}"], ws)
    _run(["go", "mod", "tidy"], ws)
    adk_after = re.search(r"google\.golang\.org/adk/v2 (v\S+)", open(os.path.join(ws, "go.mod")).read())
    if adk_before and adk_after and adk_before.group(1) != adk_after.group(1):
      raise RuntimeError(f"moving a2a-go to {version} also moved adk-go "
                         f"{adk_before.group(1)} -> {adk_after.group(1)}; not a dependency swap")
  elif lang == "rust":
    _run(["cargo", "update", "--manifest-path", "hosts/rust/Cargo.toml", "-p", "a2a-protocol-types",
          "--precise", version], ws)
  else:
    raise ValueError(lang)


def run_tests(ws: str, langs: list[str], out_dir: str) -> int:
  tags = ",".join(f"subject-{t}" for lang in langs for t in (lang, SDKS[lang][1]))
  cmd = ["bazel", "test", *SCENARIO_PACKAGES, "--keep_going", "--build_tests_only", "--test_output=errors",
         f"--test_tag_filters={tags}",
         f"--disk_cache={os.path.join(os.path.dirname(ws), 'disk-cache')}"]
  print("+", " ".join(cmd), flush=True)
  code = subprocess.run(cmd, cwd=ws, env=dict(os.environ, CARGO_BAZEL_REPIN="1"), check=False).returncode
  testlogs = subprocess.run(["bazel", "info", "bazel-testlogs"], cwd=ws, check=True,
                            capture_output=True, text=True).stdout.strip()
  dest = os.path.join(out_dir, "transcripts")
  shutil.rmtree(dest, ignore_errors=True)
  for dirpath, _, names in os.walk(testlogs, followlinks=True):
    for name in names:
      if name.endswith(".transcript.json"):
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(os.path.join(dirpath, name), os.path.join(dest, name))
  return code


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--out", required=True)
  parser.add_argument("--dry-run", action="store_true", help="only resolve versions")
  parser.add_argument("--latest", action="append", default=[],
                      help="override a latest version, e.g. python=1.2.1 (for testing the swap)")
  args = parser.parse_args(argv)
  os.makedirs(args.out, exist_ok=True)
  pinned = pinned_versions()
  latest = latest_versions()
  for override in args.latest:
    lang, _, version = override.partition("=")
    latest[lang] = version
  sdks: dict[str, Any] = {}
  to_swap = []
  for lang, (sdk, bare) in SDKS.items():
    info: dict[str, Any] = {"sdk": sdk, "bare": bare, "pinned": pinned.get(lang),
                            "latest": latest.get(lang), "swapped": False}
    if not info["pinned"] or not info["latest"]:
      info["reason"] = "version unknown"
    elif info["pinned"] == info["latest"]:
      info["reason"] = "pinned is the latest release"
    elif lang == "rust" and not semver_compatible(info["pinned"], info["latest"]):
      info["reason"] = (f"adk-server requires ^{info['pinned']}; {info['latest']} is not "
                        "semver-compatible, so it cannot be swapped in without patching adk-server")
    else:
      to_swap.append(lang)
    sdks[lang] = info
  code = 0
  if to_swap and not args.dry_run:
    ws = prepare_workspace()
    for lang in to_swap:
      try:
        bump(lang, sdks[lang]["latest"], ws)
        sdks[lang]["swapped"] = True
      except Exception as e:  # pylint: disable=broad-except
        sdks[lang]["reason"] = f"bump failed: {type(e).__name__}: {e}"
    swapped = [lang for lang in to_swap if sdks[lang]["swapped"]]
    if swapped:
      code = run_tests(ws, swapped, args.out)
  elif to_swap:
    for lang in to_swap:
      sdks[lang]["reason"] = "dry run"
  result = {"sdks": sdks, "bazel_exit_code": code}
  with open(os.path.join(args.out, "latest.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, sort_keys=True)
  print(json.dumps(sdks, indent=2, sort_keys=True))
  return 0


if __name__ == "__main__":
  sys.exit(main())
