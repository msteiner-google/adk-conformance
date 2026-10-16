"""Static checks for scenario files: schema basics, drivers, matchers, paths.

Runs as a Bazel test per scenario package (see tools/bazel/conformance.bzl),
so a broken scenario fails fast instead of at run time on every SDK.
"""

from __future__ import annotations

import os
import re
import sys

from harness import paths, runner
from model_server.server import ModelScript

DRIVERS = {"adk.list_apps", "adk.create_session", "adk.get_session", "adk.list_sessions",
           "adk.delete_session", "adk.run", "adk.run_sse", "http",
           "a2a.get_card", "a2a.send_message", "a2a.stream", "a2a.get_task", "a2a.cancel_task",
           "a2a.list_tasks", "a2a.subscribe", "a2a.raw", "a2a.check_interfaces"}
A2A_DRIVERS = {d for d in DRIVERS if d.startswith("a2a.")}
WHEN_KEYS = {"turn", "agent", "call", "has_function_response", "last_user_text_contains",
             "newest_content_contains"}
STEP_KEYS = {"expect", "save_as", "name"}


def lint(path: str) -> list[str]:
  problems = []
  try:
    scenario = runner.load_scenario(path)
  except Exception as e:  # pylint: disable=broad-except
    return [f"{path}: cannot load: {e}"]
  stem = os.path.basename(path).rsplit(".", 1)[0]
  if scenario["id"] != stem:
    problems.append(f"{path}: id {scenario['id']!r} does not match file name {stem!r}")
  if not scenario.get("behaviors"):
    problems.append(f"{path}: no behaviors listed")
  try:
    ModelScript(scenario.get("model_script"))
  except ValueError as e:
    problems.append(f"{path}: model_script: {e}")
  for rule in scenario.get("model_script") or []:
    for key in rule.get("when") or {}:
      if key not in WHEN_KEYS:
        problems.append(f"{path}: unknown model_script 'when' key {key!r}")
  if scenario.get("a2a") and scenario.get("remote"):
    problems.append(f"{path}: a scenario is either an A2A server ('a2a') or client ('remote') one")
  for key in ("a2a", "remote"):
    if scenario.get(key) is not None and not (scenario[key] or {}).get("app"):
      problems.append(f"{path}: '{key}' needs an 'app' (the fixture to serve over A2A)")
  names = set()
  for i, step in enumerate(scenario["steps"]):
    drivers = [k for k in step if k not in STEP_KEYS]
    if len(drivers) != 1 or drivers[0] not in DRIVERS:
      problems.append(f"{path}: step {i}: expected one driver from {sorted(DRIVERS)}, got {drivers}")
    if drivers and drivers[0] in A2A_DRIVERS and not scenario.get("a2a"):
      problems.append(f"{path}: step {i}: {drivers[0]} needs 'a2a: {{app: ...}}' in the scenario")
    name = step.get("name", f"{i}:{drivers[0] if drivers else '?'}")
    if name in names:
      problems.append(f"{path}: duplicate step name {name!r}")
    names.add(name)
    for expr in step.get("expect") or {}:
      try:
        paths.parse(expr)
      except ValueError as e:
        problems.append(f"{path}: step {name!r}: {e}")
  for expr in scenario.get("expect_llm") or {}:
    try:
      paths.parse(expr)
    except ValueError as e:
      problems.append(f"{path}: expect_llm: {e}")
  return problems


def lint_known_divergences(path: str, scenario_ids: set[str] | None = None) -> list[str]:
  """Validates known_divergences.yaml (structure, dates, referenced scenarios)."""
  import yaml  # pylint: disable=import-outside-toplevel
  try:
    with open(path, encoding="utf-8") as f:
      data = yaml.safe_load(f) or {}
  except Exception as e:  # pylint: disable=broad-except
    return [f"{path}: cannot parse: {e}"]
  problems = []
  seen = set()
  for i, entry in enumerate(data.get("divergences") or []):
    where = f"{path}: entry {i}"
    for key in ("scenario", "subject", "reason", "expires"):
      if not entry.get(key):
        problems.append(f"{where}: missing {key!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(entry.get("expires", ""))):
      problems.append(f"{where}: expires must be a quoted YYYY-MM-DD string")
    key = (entry.get("scenario"), entry.get("subject"))
    if key in seen:
      problems.append(f"{where}: duplicate entry for {key}")
    seen.add(key)
    if scenario_ids is not None and entry.get("scenario") not in scenario_ids:
      problems.append(f"{where}: unknown scenario {entry.get('scenario')!r}")
  return problems


def main(argv: list[str]) -> int:
  divergence_files = [f for f in argv if os.path.basename(f) == "known_divergences.yaml"]
  scenarios = [f for f in argv if f not in divergence_files]
  problems = [p for f in scenarios for p in lint(f)]
  ids = {os.path.basename(f).rsplit(".", 1)[0] for f in scenarios} if scenarios else None
  for f in divergence_files:
    problems.extend(lint_known_divergences(f, ids))
  for p in problems:
    print(p)
  print(f"linted {len(scenarios)} scenario(s), {len(divergence_files)} divergence file(s), "
        f"{len(problems)} problem(s)")
  return 1 if problems else 0


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
