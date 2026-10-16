"""Recording HTTP proxy between an A2A client host and an A2A server host.

Client scenarios (A2A-CLI) point a host's RemoteA2aAgent at this proxy, which
forwards every request to the real server and records both sides, so the
harness can check what the *client* SDK put on the wire: the ``A2A-Version``
header, the JSON-RPC method, message parts, contextId/taskId reuse.

Agent cards are rewritten on the way back (the server's origin becomes the
proxy's), so a client that follows the card's ``supportedInterfaces`` URL
keeps talking through the proxy. Streams are passed through chunk by chunk.
"""

from __future__ import annotations

import copy
import http.client
import http.server
import json
import threading
import time
import urllib.parse
from typing import Any

from harness import a2a_client

_HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailers", "transfer-encoding", "upgrade", "host", "content-length",
               "accept-encoding"}
_RECORDED_REQUEST_HEADERS = ("a2a-version", "content-type", "accept", "authorization",
                             "a2a-extensions", "user-agent", "x-a2a-version")


class RecordingProxy:
  """Threaded reverse proxy to ``upstream`` (``http://host:port``)."""

  def __init__(self, upstream: str, host: str = "127.0.0.1", port: int = 0):
    self.upstream = upstream.rstrip("/")
    parsed = urllib.parse.urlsplit(self.upstream)
    self._up_host, self._up_port = parsed.hostname, parsed.port or 80
    self.exchanges: list[dict[str, Any]] = []
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

  def recorded(self, since: int = 0) -> list[dict[str, Any]]:
    with self._lock:
      return copy.deepcopy(self.exchanges[since:])

  def count(self) -> int:
    with self._lock:
      return len(self.exchanges)

  def _record(self, entry: dict[str, Any]) -> None:
    with self._lock:
      entry["seq"] = len(self.exchanges) + 1
      self.exchanges.append(entry)

  def _handler(self):
    proxy = self

    class Handler(http.server.BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"

      def log_message(self, fmt, *args):
        pass

      def _forward(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        entry: dict[str, Any] = {
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()
                        if k.lower() in _RECORDED_REQUEST_HEADERS},
            "body": _maybe_json(body),
        }
        start = time.monotonic()
        conn = http.client.HTTPConnection(proxy._up_host, proxy._up_port, timeout=120)
        try:
          conn.request(self.command, self.path, body=body, headers=headers)
          resp = conn.getresponse()
        except OSError as e:
          entry["error"] = f"{type(e).__name__}: {e}"
          proxy._record(entry)
          self.send_error(502, f"upstream unreachable: {e}")
          return
        content_type = resp.getheader("Content-Type", "")
        entry["status"] = resp.status
        entry["content_type"] = content_type
        is_stream = "text/event-stream" in content_type
        self.send_response(resp.status)
        for key, value in resp.getheaders():
          if key.lower() not in _HOP_BY_HOP:
            self.send_header(key, value)
        if is_stream:
          self.send_header("Transfer-Encoding", "chunked")
          self.end_headers()
          chunks = []
          while True:
            chunk = resp.read1(65536)
            if not chunk:
              break
            chunks.append(chunk)
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
          text = b"".join(chunks).decode("utf-8", errors="replace")
          entry["events"], entry["sse_framing"] = a2a_client.parse_sse(text)
          entry["sse_line_ending"] = a2a_client.line_ending(text)
          entry["duration_ms"] = int((time.monotonic() - start) * 1000)
          # Record before the terminating chunk: the client must not see the
          # end of the exchange before the harness can.
          proxy._record(entry)
          self.wfile.write(b"0\r\n\r\n")
        else:
          data = resp.read()
          if self.path.split("?")[0].endswith("agent-card.json"):
            data = data.replace(proxy.upstream.encode(), proxy.base_url.encode())
            up_localhost = proxy.upstream.replace("127.0.0.1", "localhost").encode()
            data = data.replace(up_localhost, proxy.base_url.encode())
          entry["response"] = _maybe_json(data)
          entry["duration_ms"] = int((time.monotonic() - start) * 1000)
          proxy._record(entry)
          self.send_header("Content-Length", str(len(data)))
          self.end_headers()
          self.wfile.write(data)
        conn.close()

      do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _forward

    return Handler


def _maybe_json(data: bytes | None) -> Any:
  if not data:
    return None
  text = data.decode("utf-8", errors="replace")
  try:
    return json.loads(text)
  except json.JSONDecodeError:
    return text[:4000]


def traffic_view(exchanges: list[dict[str, Any]]) -> dict[str, Any]:
  """Derived view of what a client sent: methods, versions, messages, ids."""
  from harness import a2a_view  # pylint: disable=import-outside-toplevel
  rpc = []
  for ex in exchanges:
    body = ex.get("body") if isinstance(ex.get("body"), dict) else {}
    method = body.get("method") if "jsonrpc" in body else None
    if method is None and not ex["path"].split("?")[0].endswith("agent-card.json"):
      method = f"{ex['method']} {ex['path'].split('?')[0]}"
    params = body.get("params") if "jsonrpc" in body else body
    rpc.append({"method": method, "params": params, "status": ex.get("status"),
                "version": ex["headers"].get("a2a-version"), "path": ex["path"]})
  calls = [r for r in rpc if r["method"]]
  response_context_ids = []
  for ex in exchanges:
    payloads = ex.get("events") if ex.get("events") is not None else [ex.get("response")]
    for payload in payloads or []:
      result = payload.get("result", payload) if isinstance(payload, dict) else None
      event = a2a_view.canonical_event(result) if isinstance(result, dict) else None
      obj = (event or {}).get((event or {}).get("kind")) or {}
      if isinstance(obj, dict) and obj.get("contextId") and obj["contextId"] not in response_context_ids:
        response_context_ids.append(obj["contextId"])
  messages = [a2a_view.canonical_message((r["params"] or {}).get("message"))
              for r in calls if isinstance(r["params"], dict) and (r["params"] or {}).get("message")]
  return {
      "exchange_count": len(exchanges),
      "card_fetches": sum(1 for ex in exchanges if ex["path"].split("?")[0].endswith("agent-card.json")),
      "methods": [r["method"] for r in calls],
      "versions": [r["version"] for r in calls],
      "messages": messages,
      "message_texts": [a2a_view.part_texts(m.get("parts")) for m in messages],
      "context_ids": [m.get("contextId") for m in messages],
      "task_ids": [m.get("taskId") for m in messages],
      "roles": [m.get("role") for m in messages],
      "response_context_ids": response_context_ids,
      "requests": calls,
  }
