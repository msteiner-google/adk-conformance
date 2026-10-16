"""Scripted, Gemini-compatible model server.

Every ADK host points its model client at this server instead of the real
Gemini API. Each scenario supplies a *model script*: an ordered list of rules,
each with an optional ``when`` matcher and a ``reply``. The first matching rule
answers the request. Every request is recorded so the harness can compare the
LLM requests that different SDKs send.

Supported endpoints (any API version prefix, e.g. ``v1beta``):

* ``POST /{version}/models/{model}:generateContent``
* ``POST /{version}/models/{model}:streamGenerateContent`` (``?alt=sse`` gives
  server-sent events; otherwise a JSON array of chunks)
* ``GET /_scripted/requests``: the recorded requests, as JSON
* ``GET /_scripted/health``: readiness probe

Rule format::

    - when: {turn: 1}                     # optional; all keys must match
      reply: {text: "Hello!"}

``when`` keys:
  * ``turn``: 1-based index of the LLM request within the run.
  * ``agent``: the calling agent, read from the ``AGENT=<name>.`` marker that
    fixture instructions start with (see fixtures/FIXTURES.md). Looked up in
    ``systemInstruction`` first, then in the request contents, because some
    SDKs send the instruction as a user turn.
  * ``call``: 1-based index of the request among that agent's requests.
  * ``has_function_response``: ``true`` or a tool name; the newest content
    must carry a function response (for that tool).
  * ``last_user_text_contains``: substring of the last user text part.
  * ``newest_content_contains``: substring of the newest content, serialized
    as JSON (matches text and function-response payloads alike).

``reply`` keys (one of ``text``, ``function_calls``, ``empty`` or ``error``):
  * ``text``: model text. ``chunks`` optionally splits it for streaming.
  * ``function_calls``: list of ``{name, args, id?}``; emitted as parallel
    calls. Without ``id`` the SDK has to generate one.
  * ``empty``: ``true`` returns a candidate with no parts.
  * ``error``: ``{code, message}``; returned as an HTTP error.
  * ``finish_reason``: overrides ``STOP`` on the final chunk.
  * ``delay_ms``: waits this long before answering (keeps a task running, for
    cancel and resubscribe scenarios).

The module only uses the standard library, so it runs without Bazel too::

    python3 model_server/server.py --script script.json --port 0
"""

from __future__ import annotations

import argparse
import copy
import http.server
import json
import re
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

_PATH_RE = re.compile(
    r"^/(?P<version>v1[a-z0-9]*)/models/(?P<model>[^/:]+)"
    r":(?P<method>generateContent|streamGenerateContent)$"
)
_STATUS_NAMES = {
    400: "INVALID_ARGUMENT",
    403: "PERMISSION_DENIED",
    404: "NOT_FOUND",
    429: "RESOURCE_EXHAUSTED",
    500: "INTERNAL",
    503: "UNAVAILABLE",
}


_MARKER_RE = re.compile(r"AGENT=([A-Za-z0-9_\-]+)\.")


class ScriptError(Exception):
  """Raised when no rule in the model script matches a request."""


def _texts(content: dict[str, Any] | None) -> list[str]:
  return [p["text"] for p in (content or {}).get("parts") or [] if isinstance(p, dict) and "text" in p]


def detect_agent(body: dict[str, Any]) -> str | None:
  """Returns the agent named by the first ``AGENT=<name>.`` marker, if any."""
  system = body.get("systemInstruction") or body.get("system_instruction")
  for text in _texts(system):
    match = _MARKER_RE.search(text)
    if match:
      return match.group(1)
  for content in body.get("contents") or []:
    for text in _texts(content):
      match = _MARKER_RE.search(text)
      if match:
        return match.group(1)
  return None


def _newest_function_responses(body: dict[str, Any]) -> list[str]:
  contents = body.get("contents") or []
  if not contents:
    return []
  names = []
  for part in contents[-1].get("parts") or []:
    response = part.get("functionResponse") or part.get("function_response")
    if response is not None:
      names.append(response.get("name", ""))
  return names


def _last_user_text(body: dict[str, Any]) -> str:
  for content in reversed(body.get("contents") or []):
    if content.get("role", "user") != "user":
      continue
    texts = [p["text"] for p in content.get("parts") or [] if "text" in p]
    if texts:
      return "\n".join(texts)
  return ""


class ModelScript:
  """Ordered rules that map an LLM request to a scripted reply."""

  def __init__(self, rules: list[dict[str, Any]] | None):
    self.rules = list(rules or [])
    for i, rule in enumerate(self.rules):
      if "reply" not in rule:
        raise ValueError(f"model_script rule {i} has no 'reply'")

  def reply_for(self, body: dict[str, Any], turn: int, agent: str | None = None,
                agent_call: int = 0) -> dict[str, Any]:
    for rule in self.rules:
      if self._matches(rule.get("when") or {}, body, turn, agent, agent_call):
        return rule["reply"]
    raise ScriptError(
        f"no model_script rule matches LLM request #{turn} (agent={agent}, call={agent_call})")

  @staticmethod
  def _matches(when: dict[str, Any], body: dict[str, Any], turn: int, agent: str | None,
               agent_call: int) -> bool:
    for key, expected in when.items():
      if key == "turn":
        if int(expected) != turn:
          return False
      elif key == "agent":
        if expected != agent:
          return False
      elif key == "call":
        if int(expected) != agent_call:
          return False
      elif key == "has_function_response":
        names = _newest_function_responses(body)
        if expected is True and not names:
          return False
        if expected is False and names:
          return False
        if isinstance(expected, str) and expected not in names:
          return False
      elif key == "last_user_text_contains":
        if str(expected) not in _last_user_text(body):
          return False
      elif key == "newest_content_contains":
        contents = body.get("contents") or []
        if not contents or str(expected) not in json.dumps(contents[-1]):
          return False
      else:
        raise ValueError(f"unknown 'when' key in model_script: {key!r}")
    return True


def _parts_for(reply: dict[str, Any]) -> list[dict[str, Any]]:
  if reply.get("empty"):
    return []
  if "function_calls" in reply:
    parts = []
    for call in reply["function_calls"]:
      function_call = {"name": call["name"], "args": call.get("args") or {}}
      if call.get("id"):
        function_call["id"] = call["id"]
      parts.append({"functionCall": function_call})
    return parts
  return [{"text": str(reply.get("text", ""))}]


def _response(parts: list[dict[str, Any]], final: bool,
              finish_reason: str = "STOP") -> dict[str, Any]:
  candidate: dict[str, Any] = {
      "content": {"role": "model", "parts": parts},
      "index": 0,
  }
  if final:
    candidate["finishReason"] = finish_reason
  response: dict[str, Any] = {
      "candidates": [candidate],
      "modelVersion": "scripted-model",
      "responseId": "scripted",
  }
  if final:
    response["usageMetadata"] = {
        "promptTokenCount": 1,
        "candidatesTokenCount": 1,
        "totalTokenCount": 2,
    }
  return response


def _stream_chunks(reply: dict[str, Any]) -> list[dict[str, Any]]:
  finish = reply.get("finish_reason", "STOP")
  if "function_calls" in reply or reply.get("empty"):
    return [_response(_parts_for(reply), final=True, finish_reason=finish)]
  chunks = reply.get("chunks") or [str(reply.get("text", ""))]
  out = []
  for i, chunk in enumerate(chunks):
    out.append(_response([{"text": chunk}], final=i == len(chunks) - 1, finish_reason=finish))
  return out


class ScriptedModelServer:
  """Threaded HTTP server that answers Gemini API calls from a model script."""

  def __init__(self, script: ModelScript, host: str = "127.0.0.1", port: int = 0):
    self.script = script
    self.requests: list[dict[str, Any]] = []
    self._lock = threading.Lock()
    self._httpd = http.server.ThreadingHTTPServer((host, port), self._handler())
    self._httpd.daemon_threads = True
    self._thread: threading.Thread | None = None

  @property
  def base_url(self) -> str:
    host, port = self._httpd.server_address[:2]
    return f"http://{host}:{port}"

  def start(self) -> str:
    self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
    self._thread.start()
    return self.base_url

  def stop(self) -> None:
    self._httpd.shutdown()
    self._httpd.server_close()

  def recorded_requests(self) -> list[dict[str, Any]]:
    with self._lock:
      return copy.deepcopy(self.requests)

  def _record(self, entry: dict[str, Any]) -> tuple[int, int]:
    with self._lock:
      entry["turn"] = len(self.requests) + 1
      entry["agent_call"] = 1 + sum(1 for r in self.requests if r.get("agent") == entry.get("agent"))
      self.requests.append(entry)
      return entry["turn"], entry["agent_call"]

  def _handler(self):
    server = self

    class Handler(http.server.BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"

      def log_message(self, fmt, *args):  # Quiet by default; tests read requests.
        pass

      def _send_json(self, code: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

      def _send_error(self, code: int, message: str) -> None:
        status = _STATUS_NAMES.get(code, "UNKNOWN")
        self._send_json(code, {"error": {"code": code, "message": message, "status": status}})

      def do_GET(self):
        path = urlparse(self.path).path
        if path == "/_scripted/health":
          self._send_json(200, {"status": "ok"})
        elif path == "/_scripted/requests":
          self._send_json(200, server.recorded_requests())
        else:
          self._send_error(404, f"unknown path {path}")

      def do_POST(self):
        url = urlparse(self.path)
        match = _PATH_RE.match(url.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not match:
          self._send_error(404, f"unsupported path {url.path}")
          return
        try:
          body = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
          self._send_error(400, f"invalid JSON body: {e}")
          return
        query = parse_qs(url.query)
        agent = detect_agent(body)
        turn, agent_call = server._record({
            "agent": agent,
            "method": match["method"],
            "model": match["model"],
            "api_version": match["version"],
            "query": {k: v[0] for k, v in query.items() if k != "key"},
            "user_agent": self.headers.get("User-Agent", ""),
            "api_client": self.headers.get("x-goog-api-client", ""),
            "body": body,
        })
        try:
          reply = server.script.reply_for(body, turn, agent, agent_call)
        except ScriptError as e:
          self._send_error(500, str(e))
          return
        if reply.get("delay_ms"):
          time.sleep(float(reply["delay_ms"]) / 1000)
        if "error" in reply:
          err = reply["error"]
          self._send_error(int(err.get("code", 500)), str(err.get("message", "scripted error")))
          return
        if match["method"] == "generateContent":
          finish = reply.get("finish_reason", "STOP")
          self._send_json(200, _response(_parts_for(reply), final=True, finish_reason=finish))
          return
        chunks = _stream_chunks(reply)
        if query.get("alt", [""])[0] == "sse":
          data = b"".join(f"data: {json.dumps(c)}\r\n\r\n".encode() for c in chunks)
          self.send_response(200)
          self.send_header("Content-Type", "text/event-stream")
          self.send_header("Content-Length", str(len(data)))
          self.end_headers()
          self.wfile.write(data)
        else:
          self._send_json(200, chunks)

    return Handler


def load_script_file(path: str) -> ModelScript:
  """Loads a model script from a JSON or YAML file (YAML needs PyYAML)."""
  with open(path, encoding="utf-8") as f:
    text = f.read()
  if path.endswith((".yaml", ".yml")):
    import yaml  # pylint: disable=import-outside-toplevel
    data = yaml.safe_load(text)
  else:
    data = json.loads(text)
  if isinstance(data, dict):
    data = data.get("model_script", [])
  return ModelScript(data)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--script", required=True, help="JSON/YAML model script")
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=0)
  args = parser.parse_args(argv)
  server = ScriptedModelServer(load_script_file(args.script), args.host, args.port)
  print(f"READY {server.base_url}", flush=True)
  try:
    server._httpd.serve_forever()
  except KeyboardInterrupt:
    pass
  return 0


if __name__ == "__main__":
  sys.exit(main())
