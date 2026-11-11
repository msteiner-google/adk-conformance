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
sys.path.insert(0, ROOT)
from harness import subjects  # pylint: disable=wrong-import-position


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


def a2a_rs_versions() -> dict[str, str]:
  """The bare reference host (hosts/a2a-rs): the official a2aproject/a2a-rs crates."""
  lock = _read("hosts/a2a-rs/Cargo.lock")
  out = {}
  for name in re.findall(r'name = "((?:a2a|agntcy)[a-z0-9-]*)"', lock):
    match = re.search(rf'name = "{re.escape(name)}"\nversion = "([^"]+)"', lock)
    if match:
      out[name] = match.group(1)
  return out


def _maven_versions(lock_rel: str, prefixes: tuple[str, ...]) -> dict[str, str]:
  """group:artifact -> version from a rules_jvm_external lock file."""
  text = _read(lock_rel)
  artifacts = json.loads(text).get("artifacts", {}) if text else {}
  return {name: info["version"] for name, info in sorted(artifacts.items())
          if name in prefixes and "version" in info}


def java_versions() -> dict[str, str]:
  return _maven_versions("hosts/java/maven_install.json", (
      "com.google.adk:google-adk", "com.google.adk:google-adk-dev", "com.google.adk:google-adk-a2a",
      "com.google.genai:google-genai", "io.github.a2asdk:a2a-java-sdk-spec"))


def kotlin_versions() -> dict[str, str]:
  return _maven_versions("hosts/kotlin/maven_install.json", (
      "com.google.adk:google-adk-kotlin-core-jvm", "com.google.adk:google-adk-kotlin-webserver",
      "com.google.adk:google-adk-kotlin-a2a-jvm", "com.google.genai:google-genai-kotlin-jvm",
      "org.a2aproject.sdk:a2a-java-sdk-spec"))


def versions() -> dict[str, dict[str, str]]:
  """Versions of every active subject (paused ones: harness/subjects.py)."""
  readers = {"python": python_versions, "go": go_versions, "java": java_versions,
             "kotlin": kotlin_versions, "rust": rust_versions, "a2a-rs": a2a_rs_versions}
  return {name: readers[name]() for name in subjects.active(readers)}


if __name__ == "__main__":
  json.dump(versions(), sys.stdout, indent=2, sort_keys=True)
  print()
