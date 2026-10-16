"""Prints the SDK versions the workspace resolves to, as JSON.

Reads lock files only (no network), so the result describes exactly what a
run was built against:

  python3 tools/sdk_versions.py
"""

from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
  path = os.path.join(ROOT, rel)
  return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


def python_versions() -> dict[str, str]:
  lock = _read("third_party/python/requirements_lock.txt")
  out = {}
  for name in ("google-adk", "a2a-sdk", "google-genai"):
    match = re.search(rf"^{re.escape(name)}==([^\s\\]+)", lock, re.M)
    if match:
      out[name] = match.group(1)
  return out


def go_versions() -> dict[str, str]:
  mod = _read("go.mod")
  out = {}
  for path in ("google.golang.org/adk/v2", "github.com/a2aproject/a2a-go/v2", "google.golang.org/genai"):
    match = re.search(rf"^\s*{re.escape(path)}\s+(v\S+)", mod, re.M)
    if match:
      out[path] = match.group(1)
  return out


def rust_versions() -> dict[str, str]:
  lock = _read("hosts/rust/Cargo.lock")
  out = {}
  for name in ("adk-server", "adk-agent", "adk-gemini", "a2a-protocol-types"):
    match = re.search(rf'name = "{re.escape(name)}"\nversion = "([^"]+)"', lock)
    if match:
      out[name] = match.group(1)
  return out


def versions() -> dict[str, dict[str, str]]:
  return {"python": python_versions(), "go": go_versions(), "rust": rust_versions()}


if __name__ == "__main__":
  json.dump(versions(), sys.stdout, indent=2, sort_keys=True)
  print()
