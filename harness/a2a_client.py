"""Minimal A2A v1 client over raw HTTP (stdlib only).

Like ``adk_client``, this never uses an A2A SDK: every server is driven with
the same raw JSON-RPC and HTTP+JSON requests, so no SDK's client bugs can bias
a verdict. Requests carry ``A2A-Version: 1.0`` unless a step overrides it.

Bindings (A2A spec section 5.3):

==================  ======================  ==========================
operation           JSON-RPC method         HTTP+JSON (REST)
==================  ======================  ==========================
send_message        SendMessage             POST /message:send
stream              SendStreamingMessage    POST /message:stream
get_task            GetTask                 GET  /tasks/{id}
cancel_task         CancelTask              POST /tasks/{id}:cancel
list_tasks          ListTasks               GET  /tasks
subscribe           SubscribeToTask         POST /tasks/{id}:subscribe
==================  ======================  ==========================
"""

from __future__ import annotations

import http.client
import itertools
import json
import socket
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

A2A_VERSION = "1.0"
CARD_PATH = "/.well-known/agent-card.json"
JSONRPC = "JSONRPC"
REST = "HTTP+JSON"

METHODS = {
    "send_message": ("SendMessage", "POST", "/message:send", False),
    "stream": ("SendStreamingMessage", "POST", "/message:stream", True),
    "get_task": ("GetTask", "GET", "/tasks/{id}", False),
    "cancel_task": ("CancelTask", "POST", "/tasks/{id}:cancel", False),
    "list_tasks": ("ListTasks", "GET", "/tasks", False),
    "subscribe": ("SubscribeToTask", "POST", "/tasks/{id}:subscribe", True),
}

_ids = itertools.count(1)


@dataclass
class Exchange:
  """One HTTP exchange, kept verbatim for the transcript."""

  method: str
  url: str
  request_headers: dict[str, str]
  request_body: Any
  status: int = 0
  headers: dict[str, str] = field(default_factory=dict)
  text: str = ""
  json: Any = None
  sse: list[Any] | None = None  # parsed SSE data payloads, for streams
  sse_framing: str = ""
  sse_line_ending: str = ""  # "CRLF", "LF" or "mixed": SSE allows all, clients must too
  closed: bool = True  # False when a stream hit the timeout without closing
  duration_ms: int = 0
  error: str = ""

  def to_transcript(self) -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": self.status,
        "content_type": self.headers.get("content-type", ""),
        "headers": {k: v for k, v in self.headers.items()
                    if k in ("content-type", "a2a-version", "transfer-encoding")},
    }
    if self.sse is not None:
      response["events"] = self.sse
      response["closed"] = self.closed
      response["sse_framing"] = self.sse_framing
      response["sse_line_ending"] = self.sse_line_ending
    elif self.json is not None:
      response["body"] = self.json
    else:
      response["text"] = self.text[:4000]
    if self.error:
      response["error"] = self.error
    return {
        "request": {"method": self.method, "url": self.url, "headers": self.request_headers,
                    "body": self.request_body},
        "response": response,
        "duration_ms": self.duration_ms,
    }


def parse_sse(text: str) -> tuple[list[Any], str]:
  """SSE body -> (JSON payloads, framing). Non-JSON payloads stay strings."""
  events: list[Any] = []
  buffer: list[str] = []
  spaced = unspaced = 0

  def flush() -> None:
    if not buffer:
      return
    payload = "\n".join(buffer)
    buffer.clear()
    try:
      events.append(json.loads(payload))
    except json.JSONDecodeError:
      events.append(payload)

  for line in text.replace("\r\n", "\n").split("\n"):
    if not line.strip():
      flush()
    elif line.startswith("data:"):
      value = line[len("data:"):]
      if value.startswith(" "):
        spaced += 1
        value = value[1:]
      else:
        unspaced += 1
      buffer.append(value)
  flush()
  framing = "mixed" if spaced and unspaced else ("data: " if spaced else ("data:" if unspaced else "none"))
  return events, framing


def line_ending(text: str) -> str:
  crlf = text.count("\r\n")
  lf = text.count("\n") - crlf
  if crlf and lf:
    return "mixed"
  return "CRLF" if crlf else ("LF" if lf else "none")


def http_request(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
                 raw_body: str | bytes | None = None, timeout: float = 60,
                 stream_timeout: float = 30) -> Exchange:
  """Sends one request. Streams (text/event-stream) are read until close or timeout.

  ``raw_body`` is sent verbatim (malformed-JSON tests); otherwise ``body`` is
  JSON-encoded.
  """
  parsed = urllib.parse.urlsplit(url)
  path = parsed.path or "/"
  if parsed.query:
    path += "?" + parsed.query
  hdrs = dict(headers or {})
  data: bytes | None = None
  if raw_body is not None:
    data = raw_body.encode() if isinstance(raw_body, str) else raw_body
  elif body is not None:
    data = json.dumps(body).encode()
  if data is not None:
    hdrs.setdefault("Content-Type", "application/json")
  ex = Exchange(method=method, url=url, request_headers=hdrs,
                request_body=raw_body if raw_body is not None else body)
  start = time.monotonic()
  conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
  try:
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    ex.status = resp.status
    ex.headers = {k.lower(): v for k, v in resp.getheaders()}
    if "text/event-stream" in ex.headers.get("content-type", ""):
      chunks: list[bytes] = []
      deadline = time.monotonic() + stream_timeout
      conn.sock.settimeout(1.0)
      while True:
        try:
          chunk = resp.read1(65536)
        except (socket.timeout, TimeoutError):
          if time.monotonic() > deadline:
            ex.closed = False
            break
          continue
        if not chunk:
          break
        chunks.append(chunk)
        if time.monotonic() > deadline:
          ex.closed = False
          break
      ex.text = b"".join(chunks).decode("utf-8", errors="replace")
      ex.sse, ex.sse_framing = parse_sse(ex.text)
      ex.sse_line_ending = line_ending(ex.text)
    else:
      ex.text = resp.read().decode("utf-8", errors="replace")
      try:
        ex.json = json.loads(ex.text) if ex.text.strip() else None
      except json.JSONDecodeError:
        ex.json = None
  except (OSError, http.client.HTTPException) as e:
    ex.error = f"{type(e).__name__}: {e}"
  finally:
    conn.close()
    ex.duration_ms = int((time.monotonic() - start) * 1000)
  return ex


def default_headers(version: str | None = A2A_VERSION, extra: dict[str, str] | None = None) -> dict[str, str]:
  headers = {"Accept": "application/json, text/event-stream"}
  if version is not None:
    headers["A2A-Version"] = version
  headers.update(extra or {})
  return headers


def interfaces(card: Any) -> dict[str, str]:
  """{binding: url} from a v1 card's supportedInterfaces (first 1.x entry wins)."""
  out: dict[str, str] = {}
  if not isinstance(card, dict):
    return out
  entries = card.get("supportedInterfaces") or card.get("supported_interfaces") or []
  ranked = sorted(
      (e for e in entries if isinstance(e, dict)),
      key=lambda e: 0 if str(e.get("protocolVersion") or e.get("protocol_version") or "").startswith("1") else 1)
  for entry in ranked:
    binding = str(entry.get("protocolBinding") or entry.get("protocol_binding") or "").upper()
    url = entry.get("url")
    if binding and url and binding not in out:
      out[binding] = url
  return out


def jsonrpc_envelope(method: str, params: dict[str, Any], request_id: Any = None) -> dict[str, Any]:
  return {"jsonrpc": "2.0", "id": request_id if request_id is not None else next(_ids),
          "method": method, "params": params}


def call(operation: str, params: dict[str, Any], *, binding: str, url: str,
         headers: dict[str, str], stream_timeout: float = 30) -> Exchange:
  """Performs one abstract A2A operation on the given binding endpoint."""
  rpc_method, http_method, rest_path, _ = METHODS[operation]
  if binding == JSONRPC:
    return http_request("POST", url, jsonrpc_envelope(rpc_method, params), headers,
                        stream_timeout=stream_timeout)
  if binding == REST:
    params = dict(params)
    path = rest_path
    if "{id}" in path:
      path = path.replace("{id}", urllib.parse.quote(str(params.pop("id", "")), safe=""))
    target = url.rstrip("/") + path
    if http_method == "GET":
      query = {k: v for k, v in params.items() if v is not None}
      if query:
        target += "?" + urllib.parse.urlencode(query)
      return http_request("GET", target, None, headers, stream_timeout=stream_timeout)
    return http_request(http_method, target, params, headers, stream_timeout=stream_timeout)
  raise ValueError(f"unsupported binding {binding!r} (expected {JSONRPC} or {REST})")


def unwrap(binding: str, payload: Any) -> tuple[Any, Any]:
  """(result, error) of one response payload on either binding.

  JSON-RPC: ``{"result": ...}`` or ``{"error": {code, message, data}}``.
  REST: the body is the result; an error body (``{"error": {...}}`` per
  A2A section 11.6, or RFC 9457 problem details) is returned as the error.
  """
  if not isinstance(payload, dict):
    return None, None if payload is None else {"raw": payload}
  if binding == JSONRPC:
    return payload.get("result"), payload.get("error")
  if "error" in payload and isinstance(payload["error"], dict):
    return None, payload["error"]
  return payload, None
