"""Runs one scenario against one subject (an SDK host) and records a transcript.

Flow:
  1. Start the scripted model server with the scenario's model script.
  2. Start the host process, pointed at the model server.
  3. Wait until the host's ADK API answers.
  4. Execute the steps, evaluating each step's expectations.
  5. Evaluate LLM-request expectations, write the transcript, stop everything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from harness import adk_client, paths
from model_server.server import ModelScript, ScriptedModelServer

_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_READY_TIMEOUT_S = 120


@dataclass
class Subject:
  """One SDK host under test."""

  name: str
  host_cmd: list[str]
  base_path: str = ""


@dataclass
class Check:
  path: str
  expected: Any
  actual: Any = None
  ok: bool = False
  error: str = ""

  def to_dict(self) -> dict[str, Any]:
    out = {"path": self.path, "expected": self.expected, "actual": self.actual, "ok": self.ok}
    if self.error:
      out["error"] = self.error
    return out


@dataclass
class StepRecord:
  name: str
  kind: str
  exchanges: list[dict[str, Any]] = field(default_factory=list)
  result: Any = None
  checks: list[Check] = field(default_factory=list)
  error: str = ""

  @property
  def ok(self) -> bool:
    return not self.error and all(c.ok for c in self.checks)

  def to_dict(self) -> dict[str, Any]:
    return {
        "name": self.name,
        "kind": self.kind,
        "ok": self.ok,
        "error": self.error,
        "exchanges": self.exchanges,
        "checks": [c.to_dict() for c in self.checks],
    }


def load_scenario(path: str) -> dict[str, Any]:
  with open(path, encoding="utf-8") as f:
    scenario = yaml.safe_load(f)
  for key in ("id", "steps"):
    if key not in scenario:
      raise ValueError(f"{path}: scenario is missing '{key}'")
  return scenario


def free_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


def check_value(expected: Any, actual: Any) -> tuple[bool, str]:
  """Compares with optional operators: {contains|regex|exists|len|eq: v}."""
  if isinstance(expected, dict) and len(expected) == 1:
    (op, value), = expected.items()
    if op == "contains":
      return str(value) in str(actual), ""
    if op == "regex":
      return re.search(str(value), str(actual)) is not None, ""
    if op == "len":
      return hasattr(actual, "__len__") and len(actual) == value, ""
    if op == "eq":
      return actual == value, ""
  return actual == expected, ""


def evaluate(expectations: dict[str, Any] | None, data: Any) -> list[Check]:
  checks = []
  for path, expected in (expectations or {}).items():
    check = Check(path=path, expected=expected)
    if isinstance(expected, dict) and set(expected) == {"exists"}:
      try:
        check.actual = paths.lookup(data, path)
        check.ok = bool(expected["exists"])
      except KeyError:
        check.ok = not expected["exists"]
      checks.append(check)
      continue
    try:
      check.actual = paths.lookup(data, path)
      check.ok, check.error = check_value(expected, check.actual)
    except KeyError as e:
      check.error = str(e)
    checks.append(check)
  return checks


class ScenarioRun:
  """State for one (scenario, subject) execution."""

  def __init__(self, scenario: dict[str, Any], subject: Subject, workdir: str):
    self.scenario = scenario
    self.subject = subject
    self.workdir = workdir
    self.vars: dict[str, Any] = {}
    self.steps: list[StepRecord] = []
    self.model = ScriptedModelServer(ModelScript(scenario.get("model_script")))
    self.port = free_port()
    self.base_url = f"http://127.0.0.1:{self.port}{subject.base_path}"
    self.host: subprocess.Popen | None = None
    self.host_log = os.path.join(workdir, f"host-{subject.name}.log")
    self.drivers: dict[str, Callable[[dict[str, Any], StepRecord], None]] = {
        "adk.create_session": self._create_session,
        "adk.run_sse": self._run_sse,
    }

  # Templating -----------------------------------------------------------------

  def render(self, value: Any) -> Any:
    if isinstance(value, str):
      full = _TEMPLATE_RE.fullmatch(value)
      if full:
        return paths.lookup(self.vars, full.group(1))
      return _TEMPLATE_RE.sub(lambda m: str(paths.lookup(self.vars, m.group(1))), value)
    if isinstance(value, list):
      return [self.render(v) for v in value]
    if isinstance(value, dict):
      return {k: self.render(v) for k, v in value.items()}
    return value

  # Lifecycle --------------------------------------------------------------------

  def start(self) -> None:
    model_url = self.model.start()
    env = dict(os.environ)
    env.update({
        "GOOGLE_API_KEY": "scripted-key",
        "GEMINI_API_KEY": "scripted-key",
        "GOOGLE_GENAI_USE_VERTEXAI": "false",
        "HOME": self.workdir,
    })
    cmd = self.subject.host_cmd + ["--http-port", str(self.port), "--model-base-url", model_url]
    log = open(self.host_log, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    self.host = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=self.workdir)
    self._wait_ready()

  def _wait_ready(self) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    url = f"{self.base_url}/list-apps"
    while time.monotonic() < deadline:
      if self.host.poll() is not None:
        raise RuntimeError(f"host exited early with code {self.host.returncode}; see {self.host_log}")
      try:
        if adk_client.request("GET", url, timeout=2).status == 200:
          return
      except OSError:
        pass
      time.sleep(0.25)
    raise RuntimeError(f"host not ready after {_READY_TIMEOUT_S}s at {url}; see {self.host_log}")

  def stop(self) -> None:
    if self.host and self.host.poll() is None:
      self.host.terminate()
      try:
        self.host.wait(timeout=10)
      except subprocess.TimeoutExpired:
        self.host.kill()
    self.model.stop()

  # Steps --------------------------------------------------------------------------

  def run_steps(self) -> None:
    for index, raw in enumerate(self.scenario["steps"]):
      step = dict(raw)
      expect = step.pop("expect", None)
      save_as = step.pop("save_as", None)
      name = step.pop("name", None)
      if len(step) != 1:
        raise ValueError(f"step {index} must have exactly one driver key, got {sorted(step)}")
      (kind, params), = step.items()
      record = StepRecord(name=name or f"{index}:{kind}", kind=kind)
      self.steps.append(record)
      driver = self.drivers.get(kind)
      if driver is None:
        record.error = f"unknown step driver {kind!r}"
        continue
      try:
        driver(self.render(params or {}), record)
      except Exception as e:  # pylint: disable=broad-except
        record.error = f"{type(e).__name__}: {e}"
        continue
      record.checks = evaluate(expect, record.result)
      if save_as:
        self.vars[save_as] = record.result

  def _url(self, path: str) -> str:
    return f"{self.base_url}{path}"

  def _create_session(self, params: dict[str, Any], record: StepRecord) -> None:
    body: dict[str, Any] = {}
    if "state" in params:
      body["state"] = params["state"]
    res = adk_client.request(
        "POST", self._url(f"/apps/{params['app']}/users/{params['user']}/sessions"), body)
    record.exchanges.append(res.to_transcript())
    record.result = {"status": res.status, "body": res.json}

  def _run_sse(self, params: dict[str, Any], record: StepRecord) -> None:
    body = {
        "appName": params["app"],
        "userId": params["user"],
        "sessionId": params["session"],
        "newMessage": {"role": "user", "parts": [{"text": params["text"]}]},
        "streaming": bool(params.get("streaming", False)),
    }
    res = adk_client.request("POST", self._url("/run_sse"), body)
    record.exchanges.append(res.to_transcript())
    record.result = {
        "status": res.status,
        "content_type": res.headers.get("content-type", ""),
        "events": res.events,
        "event_count": len(res.events),
        "final": adk_client.final_event(res.events),
    }

  # Results -------------------------------------------------------------------------

  def llm_view(self) -> dict[str, Any]:
    requests = self.model.recorded_requests()
    return {"request_count": len(requests), "requests": requests}

  def transcript(self, llm_checks: list[Check], error: str) -> dict[str, Any]:
    passed = not error and all(s.ok for s in self.steps) and all(c.ok for c in llm_checks)
    return {
        "scenario": self.scenario["id"],
        "behaviors": self.scenario.get("behaviors", []),
        "subject": self.subject.name,
        "passed": passed,
        "error": error,
        "steps": [s.to_dict() for s in self.steps],
        "llm": {**self.llm_view(), "checks": [c.to_dict() for c in llm_checks]},
    }


def run(scenario_path: str, subject: Subject, out_dir: str) -> dict[str, Any]:
  scenario = load_scenario(scenario_path)
  os.makedirs(out_dir, exist_ok=True)
  workdir = tempfile.mkdtemp(prefix=f"{subject.name}-", dir=os.environ.get("TEST_TMPDIR"))
  run_ = ScenarioRun(scenario, subject, workdir)
  error = ""
  llm_checks: list[Check] = []
  try:
    run_.start()
    run_.run_steps()
    llm_checks = evaluate(scenario.get("expect_llm"), run_.llm_view())
  except Exception as e:  # pylint: disable=broad-except
    error = f"{type(e).__name__}: {e}"
  finally:
    run_.stop()
  transcript = run_.transcript(llm_checks, error)
  name = f"{scenario['id']}.{subject.name}"
  with open(os.path.join(out_dir, f"{name}.transcript.json"), "w", encoding="utf-8") as f:
    json.dump(transcript, f, indent=2, sort_keys=True)
  if os.path.exists(run_.host_log):
    with open(run_.host_log, encoding="utf-8", errors="replace") as src:
      log_text = src.read()
    with open(os.path.join(out_dir, f"{name}.host.log"), "w", encoding="utf-8") as dst:
      dst.write(log_text)
    transcript["_host_log_tail"] = log_text[-4000:]
  return transcript


def summarize(transcript: dict[str, Any]) -> str:
  lines = [f"{transcript['scenario']} on {transcript['subject']}: "
           f"{'PASS' if transcript['passed'] else 'FAIL'}"]
  if transcript["error"]:
    lines.append(f"  error: {transcript['error']}")
  for step in transcript["steps"]:
    mark = "ok " if step["ok"] else "BAD"
    lines.append(f"  [{mark}] {step['name']}{'  ' + step['error'] if step['error'] else ''}")
    for c in step["checks"]:
      if not c["ok"]:
        lines.append(f"        {c['path']}: expected {c['expected']!r}, got {c['actual']!r} {c.get('error', '')}")
  for c in transcript["llm"]["checks"]:
    if not c["ok"]:
      lines.append(f"  [BAD] llm {c['path']}: expected {c['expected']!r}, got {c['actual']!r} {c.get('error', '')}")
  if not transcript["passed"] and transcript.get("_host_log_tail"):
    lines.append("  --- host log (tail) ---")
    lines.append(transcript["_host_log_tail"])
  return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Run one adk-monitor scenario against one subject.")
  parser.add_argument("--scenario", required=True)
  parser.add_argument("--subject", required=True, help="subject name, e.g. python")
  parser.add_argument("--host", required=True, help="host executable")
  parser.add_argument("--base-path", default="", help="ADK API base path, e.g. /api")
  parser.add_argument("--out", default=os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/adk-monitor-out"))
  args = parser.parse_args(argv)
  host = os.path.abspath(args.host)
  transcript = run(args.scenario, Subject(args.subject, [host], args.base_path), args.out)
  print(summarize(transcript))
  return 0 if transcript["passed"] else 1


if __name__ == "__main__":
  sys.exit(main())
