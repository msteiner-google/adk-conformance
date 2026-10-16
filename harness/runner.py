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
import datetime
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

from harness import adk_client, normalize, paths
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


_TYPES = {"number": (int, float), "integer": int, "string": str, "boolean": bool,
          "list": list, "object": dict, "null": type(None)}
OPERATORS = ("contains", "not_contains", "regex", "len", "eq", "ne", "in", "all_equal",
             "same_as", "type")


def _strings(value: Any) -> list[str]:
  """Every string leaf (and dict key) inside ``value``."""
  if isinstance(value, str):
    return [value]
  if isinstance(value, dict):
    return [s for k, v in value.items() for s in [str(k), *_strings(v)]]
  if isinstance(value, list):
    return [s for v in value for s in _strings(v)]
  return [] if value is None else [str(value)]


def _contains(container: Any, value: Any) -> bool:
  """Substring match against any string inside ``container`` (or list membership)."""
  if isinstance(container, list) and value in container:
    return True
  return any(str(value) in s for s in _strings(container))


def check_value(expected: Any, actual: Any, data: Any = None) -> tuple[bool, str]:
  """Compares ``actual`` with ``expected``, a literal or a one-key operator dict.

  Operators: contains, not_contains, regex, len, eq, ne, in (list of allowed
  values), all_equal (every item of a list is equal), same_as (another path in
  the same step result), type (number|integer|string|boolean|list|object|null).
  """
  if isinstance(expected, dict) and len(expected) == 1 and next(iter(expected)) in OPERATORS:
    (op, value), = expected.items()
    if op == "contains":
      return _contains(actual, value), ""
    if op == "not_contains":
      return not _contains(actual, value), ""
    if op == "regex":
      return actual is not None and re.search(str(value), str(actual)) is not None, ""
    if op == "len":
      return hasattr(actual, "__len__") and len(actual) == value, ""
    if op == "eq":
      return actual == value, ""
    if op == "ne":
      return actual != value, ""
    if op == "in":
      return actual in value, ""
    if op == "all_equal":
      ok = isinstance(actual, list) and len(actual) > 0 and all(a == actual[0] for a in actual)
      return ok == bool(value), ""
    if op == "same_as":
      try:
        other = paths.lookup(data, value)
      except KeyError as e:
        return False, str(e)
      return actual == other and actual not in (None, ""), ""
    if op == "type":
      kind = _TYPES[value]
      if value in ("number", "integer") and isinstance(actual, bool):
        return False, ""
      return isinstance(actual, kind), ""
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
      check.ok, check.error = check_value(expected, check.actual, data)
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
        "adk.list_apps": self._list_apps,
        "adk.create_session": self._create_session,
        "adk.get_session": self._get_session,
        "adk.list_sessions": self._list_sessions,
        "adk.delete_session": self._delete_session,
        "adk.run": self._run,
        "adk.run_sse": self._run_sse,
        "http": self._http,
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
      if save_as:
        self.vars[save_as] = record.result
      try:
        rendered = self.render(expect)
      except KeyError as e:
        record.error = f"expectation template: {e}"
        continue
      record.checks = evaluate(rendered, record.result)

  def _url(self, path: str) -> str:
    return f"{self.base_url}{path}"

  def _record_json(self, res: adk_client.HttpResult, record: StepRecord) -> dict[str, Any]:
    record.exchanges.append(res.to_transcript())
    body = res.json
    return {
        "status": res.status,
        "content_type": res.headers.get("content-type", ""),
        "body": normalize.camelize(body),
        "key_style": normalize.key_style(body),
        "text": res.text if body is None else None,
    }

  def _session_path(self, params: dict[str, Any], session: bool = True) -> str:
    path = f"/apps/{params['app']}/users/{params['user']}/sessions"
    return f"{path}/{params['session']}" if session else path

  def _list_apps(self, params: dict[str, Any], record: StepRecord) -> None:
    record.result = self._record_json(adk_client.request("GET", self._url("/list-apps")), record)

  def _create_session(self, params: dict[str, Any], record: StepRecord) -> None:
    body: dict[str, Any] = {}
    if "session_id" in params:
      body["sessionId"] = params["session_id"]
    if "state" in params:
      body["state"] = params["state"]
    res = adk_client.request("POST", self._url(self._session_path(params, session=False)), body)
    record.result = self._record_json(res, record)

  def _get_session(self, params: dict[str, Any], record: StepRecord) -> None:
    res = adk_client.request("GET", self._url(self._session_path(params)))
    result = self._record_json(res, record)
    body = result["body"] if isinstance(result["body"], dict) else {}
    raw_events = ((res.json or {}).get("events") or []) if isinstance(res.json, dict) else []
    events = normalize.canonicalize_events(raw_events)
    if body:
      body["events"] = events
    result["part_shapes"] = normalize.part_shapes(raw_events)
    result.update(normalize.event_views(events))
    record.result = result

  def _list_sessions(self, params: dict[str, Any], record: StepRecord) -> None:
    res = adk_client.request("GET", self._url(self._session_path(params, session=False)))
    result = self._record_json(res, record)
    body = result["body"]
    sessions = body if isinstance(body, list) else (body or {}).get("sessions", []) if isinstance(body, dict) else []
    result["session_ids"] = [s.get("id") for s in sessions if isinstance(s, dict)]
    record.result = result

  def _delete_session(self, params: dict[str, Any], record: StepRecord) -> None:
    res = adk_client.request("DELETE", self._url(self._session_path(params)))
    record.result = self._record_json(res, record)

  def _http(self, params: dict[str, Any], record: StepRecord) -> None:
    res = adk_client.request(params.get("method", "GET"), self._url(params["path"]), params.get("body"))
    record.result = self._record_json(res, record)

  def _run_body(self, params: dict[str, Any]) -> dict[str, Any]:
    parts = params.get("parts") or [{"text": params["text"]}]
    body: dict[str, Any] = {
        "appName": params["app"],
        "userId": params["user"],
        "sessionId": params["session"],
        "newMessage": {"role": "user", "parts": parts},
    }
    if "streaming" in params:
      body["streaming"] = bool(params["streaming"])
    if "state_delta" in params:
      body["stateDelta"] = params["state_delta"]
    return body

  def _events_result(self, res: adk_client.HttpResult, raw_events: list[Any]) -> dict[str, Any]:
    events = normalize.canonicalize_events(raw_events)
    result = {
        "status": res.status,
        "content_type": res.headers.get("content-type", ""),
        "events": events,
        "event_count": len(events),
        "final": adk_client.final_event(events),
        "key_style": normalize.key_style(raw_events),
        "part_shapes": normalize.part_shapes(raw_events),
        "body": normalize.camelize(res.json) if not raw_events else None,
        "text": res.text if res.json is None and not raw_events else None,
    }
    result.update(normalize.event_views(events))
    return result

  def _run(self, params: dict[str, Any], record: StepRecord) -> None:
    res = adk_client.request("POST", self._url("/run"), self._run_body(params))
    record.exchanges.append(res.to_transcript())
    raw = res.json if isinstance(res.json, list) else []
    record.result = self._events_result(res, raw)

  def _run_sse(self, params: dict[str, Any], record: StepRecord) -> None:
    body = self._run_body(params)
    body.setdefault("streaming", False)
    res = adk_client.request("POST", self._url("/run_sse"), body)
    record.exchanges.append(res.to_transcript())
    result = self._events_result(res, res.events)
    result["sse_framing"] = adk_client.sse_framing(res.text) if res.events else None
    record.result = result

  # Results -------------------------------------------------------------------------

  def llm_view(self) -> dict[str, Any]:
    requests = self.model.recorded_requests()
    return {"request_count": len(requests), "requests": requests}

  def failures(self, llm_checks: list[Check], error: str) -> list[str]:
    """IDs of everything that went wrong, e.g. 'one turn :: final.author'."""
    out = []
    if error:
      out.append(f"run :: {error}")
    for step in self.steps:
      if step.error:
        out.append(f"{step.name} :: error")
      out.extend(f"{step.name} :: {c.path}" for c in step.checks if not c.ok)
    out.extend(f"llm :: {c.path}" for c in llm_checks if not c.ok)
    return out

  def transcript(self, llm_checks: list[Check], error: str,
                 known: list[dict[str, Any]]) -> dict[str, Any]:
    failures = self.failures(llm_checks, error)
    status, applied, note = classify(failures, known, harness_error=bool(error and not self.steps))
    return {
        "scenario": self.scenario["id"],
        "area": self.scenario.get("area", ""),
        "behaviors": self.scenario.get("behaviors", []),
        "subject": self.subject.name,
        "status": status,
        "passed": status in ("pass", "xfail", "xpass"),
        "failures": failures,
        "known_divergence": applied,
        "note": note,
        "error": error,
        "steps": [s.to_dict() for s in self.steps],
        "llm": {**self.llm_view(), "checks": [c.to_dict() for c in llm_checks]},
    }


def load_known_divergences(path: str | None, scenario_id: str, subject: str) -> list[dict[str, Any]]:
  if not path or not os.path.exists(path):
    return []
  with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}
  return [e for e in data.get("divergences") or []
          if e.get("scenario") == scenario_id and e.get("subject") == subject]


def _matches(failure: str, pattern: str) -> bool:
  """Pattern match where only '*' is a wildcard (failure ids contain brackets)."""
  regex = re.escape(pattern).replace(r"\*", ".*")
  return re.fullmatch(regex, failure) is not None


def classify(failures: list[str], known: list[dict[str, Any]],
             harness_error: bool = False) -> tuple[str, dict[str, Any] | None, str]:
  """Returns (status, applied known-divergence entry, note).

  status is one of: pass, fail, xfail (failed exactly as a known divergence
  says), xpass (passed although a known divergence is listed: remove it),
  error (the run could not execute, e.g. the host did not start).
  """
  today = datetime.date.today().isoformat()
  live = [e for e in known if str(e.get("expires", "9999-12-31")) >= today]
  expired = [e for e in known if e not in live]
  if not failures:
    if known:
      return "xpass", known[0], "passes despite a known divergence entry; remove it"
    return "pass", None, ""
  for entry in live:
    patterns = entry.get("checks") or ["*"]
    if all(any(_matches(f, p) for p in patterns) for f in failures):
      return "xfail", entry, entry.get("reason", "")
  note = "known divergence expired; re-check it" if expired else ""
  return ("error" if harness_error else "fail"), None, note


def run(scenario_path: str, subject: Subject, out_dir: str,
        known_divergences: str | None = None) -> dict[str, Any]:
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
  known = load_known_divergences(known_divergences, scenario["id"], subject.name)
  transcript = run_.transcript(llm_checks, error, known)
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
  lines = [f"{transcript['scenario']} on {transcript['subject']}: {transcript['status'].upper()}"]
  if transcript.get("note"):
    lines.append(f"  note: {transcript['note']}")
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
  if transcript["status"] in ("fail", "error") and transcript.get("_host_log_tail"):
    lines.append("  --- host log (tail) ---")
    lines.append(transcript["_host_log_tail"])
  return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Run one adk-monitor scenario against one subject.")
  parser.add_argument("--scenario", required=True)
  parser.add_argument("--subject", required=True, help="subject name, e.g. python")
  parser.add_argument("--host", required=True, help="host executable")
  parser.add_argument("--base-path", default="", help="ADK API base path, e.g. /api")
  parser.add_argument("--known-divergences", default=None, help="known_divergences.yaml")
  parser.add_argument("--out", default=os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/adk-monitor-out"))
  args = parser.parse_args(argv)
  host = os.path.abspath(args.host)
  transcript = run(args.scenario, Subject(args.subject, [host], args.base_path), args.out,
                   args.known_divergences)
  print(summarize(transcript))
  return 0 if transcript["passed"] else 1


if __name__ == "__main__":
  sys.exit(main())
