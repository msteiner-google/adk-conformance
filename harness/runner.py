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
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from harness import a2a_client, a2a_view, adk_client, normalize, paths, proxy
from model_server.server import ModelScript, ScriptedModelServer

_TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
_READY_TIMEOUT_S = 120


@dataclass
class Subject:
  """One host under test.

  ``kind`` is ``adk`` for an ADK host (ADK REST API on ``--http-port``, and
  A2A on ``--a2a-port`` when a scenario asks for it) or ``bare`` for a bare
  A2A host (an A2A SDK with a fixed executor and no ADK; A2A only).
  """

  name: str
  host_cmd: list[str]
  base_path: str = ""
  kind: str = "adk"


class Unsupported(LookupError):
  """The subject does not offer what a step needs (e.g. a binding its card lacks).

  Reported as status ``unsupported``: it feeds the capability matrix and is
  not a failure (design doc: missing features give a not-implemented result).
  """


class HostProcess:
  """One host subprocess with its log file."""

  def __init__(self, subject: Subject, workdir: str, role: str = ""):
    self.subject = subject
    self.workdir = workdir
    suffix = f"-{role}" if role else ""
    self.log_path = os.path.join(workdir, f"host-{subject.name}{suffix}.log")
    self.proc: subprocess.Popen | None = None

  def start(self, args: list[str], env: dict[str, str]) -> None:
    log = open(self.log_path, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    self.proc = subprocess.Popen(self.subject.host_cmd + args, stdout=log, stderr=subprocess.STDOUT,
                                 env=env, cwd=self.workdir)

  def wait_ready(self, urls: list[str], headers: dict[str, str] | None = None) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    pending = list(urls)
    while pending and time.monotonic() < deadline:
      if self.proc.poll() is not None:
        raise RuntimeError(f"host {self.subject.name} exited early with code {self.proc.returncode}; "
                           f"see {self.log_path}")
      try:
        if adk_client.request("GET", pending[0], timeout=2, headers=headers).status == 200:
          pending.pop(0)
          continue
      except OSError:
        pass
      time.sleep(0.25)
    if pending:
      raise RuntimeError(f"host {self.subject.name} not ready after {_READY_TIMEOUT_S}s at "
                         f"{pending[0]}; see {self.log_path}")

  def stop(self) -> None:
    if self.proc and self.proc.poll() is None:
      self.proc.terminate()
      try:
        self.proc.wait(timeout=10)
      except subprocess.TimeoutExpired:
        self.proc.kill()

  def log_text(self) -> str:
    if not os.path.exists(self.log_path):
      return ""
    with open(self.log_path, encoding="utf-8", errors="replace") as f:
      return f.read()


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
  binding: str = ""  # A2A binding used by an a2a.* step
  a2a_traffic: list[dict[str, Any]] | None = None  # client scenarios: proxied A2A exchanges

  @property
  def ok(self) -> bool:
    return not self.error and all(c.ok for c in self.checks)

  def to_dict(self) -> dict[str, Any]:
    out = {
        "name": self.name,
        "kind": self.kind,
        "ok": self.ok,
        "error": self.error,
        "exchanges": self.exchanges,
        "checks": [c.to_dict() for c in self.checks],
    }
    if self.binding:
      out["binding"] = self.binding
    if self.a2a_traffic is not None:
      out["a2a_traffic"] = self.a2a_traffic
    return out


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
  """State for one (scenario, subject) execution.

  Topologies:

  * ADK scenario: the subject's host serves the fixtures over the ADK REST API.
  * A2A server scenario (``a2a: {app: X}``): the subject's host also serves
    fixture X over A2A v1 on its own port; ``a2a.*`` steps talk to it.
  * A2A client scenario (``remote: {app: X}``): a second host, ``server``,
    serves fixture X over A2A behind a recording proxy; the subject's host
    gets ``--remote-a2a-url`` (the proxy) and is driven over its ADK REST API.
  """

  def __init__(self, scenario: dict[str, Any], subject: Subject, workdir: str,
               server: Subject | None = None):
    self.scenario = scenario
    self.subject = subject
    self.server = server
    self.workdir = workdir
    self.vars: dict[str, Any] = {}
    self.steps: list[StepRecord] = []
    self.model = ScriptedModelServer(ModelScript(scenario.get("model_script")))
    self.port = free_port()
    self.base_url = f"http://127.0.0.1:{self.port}{subject.base_path}"
    self.a2a_app = (scenario.get("a2a") or {}).get("app")
    self.remote_app = (scenario.get("remote") or {}).get("app")
    if self.remote_app and server is None:
      raise ValueError("scenario has 'remote' but no server subject was given (--server-subject)")
    self.a2a_port = free_port() if self.a2a_app else None
    self.a2a_base = f"http://127.0.0.1:{self.a2a_port}" if self.a2a_port else None
    self._a2a_card: dict[str, Any] | None = None
    self.host = HostProcess(subject, workdir)
    self.server_host = HostProcess(server, workdir, role="server") if server else None
    self.proxy: proxy.RecordingProxy | None = None
    self.host_log = self.host.log_path
    self.drivers: dict[str, Callable[[dict[str, Any], StepRecord], None]] = {
        "adk.list_apps": self._list_apps,
        "adk.create_session": self._create_session,
        "adk.get_session": self._get_session,
        "adk.list_sessions": self._list_sessions,
        "adk.delete_session": self._delete_session,
        "adk.run": self._run,
        "adk.run_sse": self._run_sse,
        "http": self._http,
        "a2a.get_card": self._a2a_get_card,
        "a2a.send_message": self._a2a_operation("send_message"),
        "a2a.stream": self._a2a_operation("stream"),
        "a2a.get_task": self._a2a_operation("get_task"),
        "a2a.cancel_task": self._a2a_operation("cancel_task"),
        "a2a.list_tasks": self._a2a_operation("list_tasks"),
        "a2a.subscribe": self._a2a_operation("subscribe"),
        "a2a.raw": self._a2a_raw,
        "a2a.check_interfaces": self._a2a_check_interfaces,
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

  def _env(self) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GOOGLE_API_KEY": "scripted-key",
        "GEMINI_API_KEY": "scripted-key",
        "GOOGLE_GENAI_USE_VERTEXAI": "false",
        "HOME": self.workdir,
    })
    return env

  def start(self) -> None:
    model_url = self.model.start()
    env = self._env()
    if self.server_host:
      server_port, server_a2a = free_port(), free_port()
      args = ["--model-base-url", model_url, "--a2a-port", str(server_a2a),
              "--a2a-app", self.remote_app]
      if self.server.kind == "adk":
        args += ["--http-port", str(server_port)]
      self.server_host.start(args, env)
      self.server_host.wait_ready([f"http://127.0.0.1:{server_a2a}{a2a_client.CARD_PATH}"],
                                  headers=a2a_client.default_headers())
      self.proxy = proxy.RecordingProxy(f"http://127.0.0.1:{server_a2a}")
      self.proxy.start()
    args = ["--model-base-url", model_url]
    ready = []
    if self.subject.kind == "adk":
      args += ["--http-port", str(self.port)]
      ready.append(f"{self.base_url}/list-apps")
    if self.a2a_app:
      args += ["--a2a-port", str(self.a2a_port), "--a2a-app", self.a2a_app]
      ready.append(f"{self.a2a_base}{a2a_client.CARD_PATH}")
    if self.proxy:
      args += ["--remote-a2a-url", self.proxy.base_url]
    self.host.start(args, env)
    self.host.wait_ready(ready, headers=a2a_client.default_headers())

  def stop(self) -> None:
    self.host.stop()
    if self.server_host:
      self.server_host.stop()
    if self.proxy:
      self.proxy.stop()
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
      seen = self.proxy.count() if self.proxy else 0
      try:
        driver(self.render(params or {}), record)
      except Unsupported as e:
        record.error = f"unsupported: {e}"
        continue
      except Exception as e:  # pylint: disable=broad-except
        record.error = f"{type(e).__name__}: {e}"
        continue
      finally:
        if self.proxy:
          record.a2a_traffic = self.proxy.recorded(seen)
      if self.proxy and isinstance(record.result, dict):
        record.result["a2a"] = proxy.traffic_view(record.a2a_traffic)
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

  # A2A drivers -----------------------------------------------------------------

  def _require_a2a(self) -> str:
    if not self.a2a_base:
      raise ValueError("a2a.* steps need 'a2a: {app: <fixture>}' in the scenario")
    return self.a2a_base

  def _card(self) -> dict[str, Any]:
    if self._a2a_card is None:
      res = a2a_client.http_request("GET", self._require_a2a() + a2a_client.CARD_PATH,
                                    headers=a2a_client.default_headers())
      self._a2a_card = res.json if isinstance(res.json, dict) else {}
    return self._a2a_card

  def _endpoint(self, binding: str) -> str:
    """URL of ``binding`` from the card, with the host pinned to 127.0.0.1.

    The card decides the port and path. Only the host name is pinned: hosts
    bind 127.0.0.1, and an advertised ``localhost``/``0.0.0.0`` would
    otherwise turn one card quirk into a failure of every scenario (the card
    scenarios still see the advertised URL).
    """
    url = a2a_client.interfaces(self._card()).get(binding)
    if not url:
      raise Unsupported(f"the agent card declares no {binding} interface")
    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("localhost", "0.0.0.0", "::", "[::]"):
      netloc = f"127.0.0.1:{parts.port}" if parts.port else "127.0.0.1"
      url = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
    return url

  @staticmethod
  def _binding(params: dict[str, Any]) -> str:
    binding = str(params.get("binding") or a2a_client.JSONRPC).upper()
    return a2a_client.REST if binding in ("REST", "HTTP+JSON", "HTTP_JSON") else binding

  @staticmethod
  def _headers(params: dict[str, Any]) -> dict[str, str]:
    version = params["version"] if "version" in params else a2a_client.A2A_VERSION
    return a2a_client.default_headers(None if version is None else str(version), params.get("headers"))

  def _a2a_get_card(self, params: dict[str, Any], record: StepRecord) -> None:
    url = self._require_a2a() + params.get("path", a2a_client.CARD_PATH)
    res = a2a_client.http_request("GET", url, headers=self._headers(params))
    record.exchanges.append(res.to_transcript())
    if res.error:
      raise ConnectionError(res.error)
    record.result = a2a_view.card_view(res.status, res.headers.get("content-type", ""), res.json)
    if isinstance(res.json, dict) and "version" not in params:
      self._a2a_card = res.json

  @staticmethod
  def message(params: dict[str, Any]) -> dict[str, Any]:
    """A v1 Message from step params (text or raw parts, optional ids)."""
    msg: dict[str, Any] = {
        "messageId": params.get("message_id") or f"msg-{uuid.uuid4()}",
        "role": params.get("role", "ROLE_USER"),
        "parts": params.get("parts") or [{"text": params.get("text", "")}],
    }
    for key, field_name in (("context_id", "contextId"), ("task_id", "taskId"),
                            ("metadata", "metadata"), ("reference_task_ids", "referenceTaskIds")):
      if params.get(key) is not None:
        msg[field_name] = params[key]
    return msg

  def _operation_params(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
    if "params" in params:  # raw request params, sent as written
      return params["params"]
    if operation in ("send_message", "stream"):
      out: dict[str, Any] = {"message": self.message(params)}
      config = dict(params.get("configuration") or {})
      for key, field_name in (("history_length", "historyLength"),
                              ("return_immediately", "returnImmediately"),
                              ("accepted_output_modes", "acceptedOutputModes")):
        if key in params:
          config[field_name] = params[key]
      if config:
        out["configuration"] = config
      if params.get("request_metadata") is not None:
        out["metadata"] = params["request_metadata"]
      return out
    if operation in ("get_task", "cancel_task", "subscribe"):
      out = {"id": params["id"]}
      if operation == "get_task" and params.get("history_length") is not None:
        out["historyLength"] = params["history_length"]
      return out
    if operation == "list_tasks":
      return {k: v for k, v in (("contextId", params.get("context_id")),
                                ("pageSize", params.get("page_size"))) if v is not None}
    raise ValueError(f"unknown A2A operation {operation!r}")

  def _a2a_operation(self, operation: str) -> Callable[[dict[str, Any], StepRecord], None]:
    def driver(params: dict[str, Any], record: StepRecord) -> None:
      binding = self._binding(params)
      record.binding = binding
      url = self._endpoint(binding)
      streaming = a2a_client.METHODS[operation][3]
      res = a2a_client.call(operation, self._operation_params(operation, params), binding=binding,
                            url=url, headers=self._headers(params),
                            stream_timeout=float(params.get("timeout", 30)))
      record.exchanges.append(res.to_transcript())
      if res.error and not res.status:
        raise ConnectionError(res.error)
      content_type = res.headers.get("content-type", "")
      if res.sse is not None:
        record.result = a2a_view.stream_view(binding, res.status, content_type, res.sse,
                                             res.closed, res.sse_framing)
        record.result["sse_line_ending"] = res.sse_line_ending
      else:
        result, error = a2a_client.unwrap(binding, res.json)
        if res.json is None and res.status >= 400 and error is None:
          error = {"status": res.status, "text": res.text[:500]}
        record.result = a2a_view.response_view(binding, res.status, content_type, res.json,
                                               result, error)
        if streaming:
          record.result["streamed"] = False
    return driver

  def _a2a_raw(self, params: dict[str, Any], record: StepRecord) -> None:
    """Sends one HTTP request as written (no envelope): error-handling tests."""
    binding = self._binding(params)
    record.binding = binding
    base = self._endpoint(binding) if not params.get("url") else params["url"]
    url = base if not params.get("path") else base.rstrip("/") + params["path"]
    res = a2a_client.http_request(params.get("method", "POST"), url, body=params.get("body"),
                                  headers=self._headers(params), raw_body=params.get("raw_body"),
                                  stream_timeout=float(params.get("timeout", 30)))
    record.exchanges.append(res.to_transcript())
    if res.error and not res.status:
      raise ConnectionError(res.error)
    content_type = res.headers.get("content-type", "")
    if res.sse is not None:
      record.result = a2a_view.stream_view(binding, res.status, content_type, res.sse,
                                           res.closed, res.sse_framing)
      return
    result, error = a2a_client.unwrap(binding, res.json)
    record.result = a2a_view.response_view(binding, res.status, content_type, res.json, result, error)
    record.result["text"] = res.text[:2000] if res.json is None else None

  def _a2a_check_interfaces(self, params: dict[str, Any], record: StepRecord) -> None:
    """Probes every interface the card declares with GetTask on an unknown id.

    A reachable binding answers TaskNotFoundError; anything else (connection
    refused, 404 on the path, a non-A2A body) means the declared interface
    is not actually served.
    """
    card = self._card()
    probe_id = params.get("id", "adk-monitor-no-such-task")
    out: dict[str, Any] = {}
    for binding in sorted(a2a_client.interfaces(card)):
      if binding not in (a2a_client.JSONRPC, a2a_client.REST):
        out[binding] = {"probed": False}
        continue
      try:
        url = self._endpoint(binding)
      except LookupError as e:
        out[binding] = {"probed": False, "error": str(e)}
        continue
      res = a2a_client.call("get_task", {"id": probe_id}, binding=binding, url=url,
                            headers=self._headers(params))
      record.exchanges.append(res.to_transcript())
      _, error = a2a_client.unwrap(binding, res.json)
      code = error.get("code") if isinstance(error, dict) else None
      out[binding] = {"probed": True, "status": res.status, "error_code": code,
                      "reachable": bool(res.status) and (error is not None or res.status == 404)
                      and not res.error}
    record.result = {
        "interfaces": out,
        "bindings": sorted(out),
        "all_reachable": all(v.get("reachable") for v in out.values() if v.get("probed")),
    }

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
      if step.error.startswith("unsupported:"):
        out.append(f"{step.name} :: unsupported")
      elif step.error:
        out.append(f"{step.name} :: error")
      out.extend(f"{step.name} :: {c.path}" for c in step.checks if not c.ok)
    out.extend(f"llm :: {c.path}" for c in llm_checks if not c.ok)
    return out

  @property
  def subject_id(self) -> str:
    """Test subject: the host, or ``<client>-to-<server>`` in client scenarios."""
    return pair_id(self.subject.name, self.server.name) if self.server else self.subject.name

  def transcript(self, llm_checks: list[Check], error: str,
                 known: list[dict[str, Any]]) -> dict[str, Any]:
    failures = self.failures(llm_checks, error)
    status, applied, note = classify(failures, known, harness_error=bool(error and not self.steps))
    topology: dict[str, Any] = {"kind": self.subject.kind}
    if self.a2a_app:
      topology["a2a_app"] = self.a2a_app
    if self.server:
      topology.update({"client": self.subject.name, "server": self.server.name,
                       "server_kind": self.server.kind, "remote_app": self.remote_app})
    return {
        "scenario": self.scenario["id"],
        "area": self.scenario.get("area", ""),
        "behaviors": self.scenario.get("behaviors", []),
        "subject": self.subject_id,
        "topology": topology,
        "status": status,
        "passed": status in ("pass", "xfail", "xpass", "unsupported"),
        "failures": failures,
        "known_divergence": applied,
        "note": note,
        "error": error,
        "steps": [s.to_dict() for s in self.steps],
        "llm": {**self.llm_view(), "checks": [c.to_dict() for c in llm_checks]},
    }


def pair_id(client: str, server: str) -> str:
  return f"{client}-to-{server}"


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
  unsupported (the subject lacks a feature the scenario needs, e.g. a
  binding; capability matrix, not a bug), error (the run could not execute,
  e.g. the host did not start).
  """
  today = datetime.date.today().isoformat()
  live = [e for e in known if str(e.get("expires", "9999-12-31")) >= today]
  expired = [e for e in known if e not in live]
  if failures and all(f.endswith(" :: unsupported") for f in failures):
    return "unsupported", None, "the subject lacks a feature this scenario needs"
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
        known_divergences: str | None = None, server: Subject | None = None) -> dict[str, Any]:
  scenario = load_scenario(scenario_path)
  os.makedirs(out_dir, exist_ok=True)
  workdir = tempfile.mkdtemp(prefix=f"{subject.name}-", dir=os.environ.get("TEST_TMPDIR"))
  run_ = ScenarioRun(scenario, subject, workdir, server=server)
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
  known = load_known_divergences(known_divergences, scenario["id"], run_.subject_id)
  transcript = run_.transcript(llm_checks, error, known)
  name = f"{scenario['id']}.{run_.subject_id}"
  with open(os.path.join(out_dir, f"{name}.transcript.json"), "w", encoding="utf-8") as f:
    json.dump(transcript, f, indent=2, sort_keys=True)
  tails = []
  for host, suffix in ((run_.host, "host"), (run_.server_host, "server.host")):
    if host is None:
      continue
    log_text = host.log_text()
    if log_text:
      with open(os.path.join(out_dir, f"{name}.{suffix}.log"), "w", encoding="utf-8") as dst:
        dst.write(log_text)
      tails.append(f"[{suffix} {host.subject.name}]\n{log_text[-3000:]}")
  transcript["_host_log_tail"] = "\n".join(tails)
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
  parser.add_argument("--kind", default="adk", choices=["adk", "bare"])
  parser.add_argument("--server-subject", default=None, help="client scenarios: server subject")
  parser.add_argument("--server-host", default=None, help="client scenarios: server host executable")
  parser.add_argument("--server-kind", default="adk", choices=["adk", "bare"])
  parser.add_argument("--known-divergences", default=None, help="known_divergences.yaml")
  parser.add_argument("--out", default=os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/adk-monitor-out"))
  args = parser.parse_args(argv)
  subject = Subject(args.subject, [os.path.abspath(args.host)], args.base_path, args.kind)
  server = None
  if args.server_subject:
    if not args.server_host:
      parser.error("--server-subject needs --server-host")
    server = Subject(args.server_subject, [os.path.abspath(args.server_host)], "", args.server_kind)
  transcript = run(args.scenario, subject, args.out, args.known_divergences, server=server)
  print(summarize(transcript))
  return 0 if transcript["passed"] else 1


if __name__ == "__main__":
  sys.exit(main())
