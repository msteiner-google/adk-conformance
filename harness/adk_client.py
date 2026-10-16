"""Minimal HTTP client for the ADK REST API (stdlib only).

The harness deliberately avoids SDK clients: every SDK is driven through the
same raw HTTP calls, so one SDK's client bugs cannot bias another SDK's verdict.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class HttpResult:
  """One HTTP exchange, kept verbatim for the transcript."""

  method: str
  url: str
  request_body: Any
  status: int
  headers: dict[str, str]
  text: str
  json: Any = None
  events: list[Any] = field(default_factory=list)

  def to_transcript(self) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request": {"method": self.method, "url": self.url, "body": self.request_body},
        "response": {
            "status": self.status,
            "content_type": self.headers.get("content-type", ""),
        },
    }
    if self.events:
      out["response"]["events"] = self.events
    elif self.json is not None:
      out["response"]["body"] = self.json
    else:
      out["response"]["text"] = self.text
    return out


def request(method: str, url: str, body: Any = None, timeout: float = 60,
            headers: dict[str, str] | None = None) -> HttpResult:
  data = None if body is None else json.dumps(body).encode()
  req = urllib.request.Request(url, data=data, method=method)
  if data is not None:
    req.add_header("Content-Type", "application/json")
  for key, value in (headers or {}).items():
    req.add_header(key, value)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      status, headers, raw = resp.status, resp.headers, resp.read()
  except urllib.error.HTTPError as e:
    status, headers, raw = e.code, e.headers, e.read()
  text = raw.decode("utf-8", errors="replace")
  result = HttpResult(
      method=method,
      url=url,
      request_body=body,
      status=status,
      headers={k.lower(): v for k, v in headers.items()},
      text=text,
  )
  content_type = result.headers.get("content-type", "")
  if "text/event-stream" in content_type:
    result.events = parse_sse(text)
  else:
    try:
      result.json = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
      result.json = None
  return result


def parse_sse(text: str) -> list[Any]:
  """Parses an SSE body into JSON payloads.

  Accepts both ``data: {...}`` and ``data:{...}`` framing, since SDKs differ.
  Non-JSON payloads are kept as strings so nothing is silently dropped.
  """
  events: list[Any] = []
  buffer: list[str] = []

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
      buffer.append(value[1:] if value.startswith(" ") else value)
  flush()
  return events


def sse_framing(text: str) -> str:
  """'data: ' (space after the colon), 'data:' (no space) or 'mixed'."""
  spaced = unspaced = 0
  for line in text.replace("\r\n", "\n").split("\n"):
    if line.startswith("data: "):
      spaced += 1
    elif line.startswith("data:"):
      unspaced += 1
  if spaced and unspaced:
    return "mixed"
  return "data: " if spaced else ("data:" if unspaced else "none")


def final_event(events: list[Any]) -> dict[str, Any] | None:
  """The last non-partial event that carries content."""
  for event in reversed(events):
    if isinstance(event, dict) and not event.get("partial") and event.get("content"):
      return event
  return None
