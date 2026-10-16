"""Normalization of SDK responses before they are checked or compared.

SDKs disagree on wire format (for example, Rust emits snake_case event keys
while the others emit camelCase). Behavior checks should not fail on format,
so expectations run against a canonical camelCase view; the raw response stays
in the transcript and format differences are recorded separately as *format
facts* (see ``format_facts``).
"""

from __future__ import annotations

import re
from typing import Any

# Keys whose values are user data: their own name is canonicalized, but the
# keys inside them are kept verbatim (state keys, tool args, ...).
OPAQUE_KEYS = frozenset({
    "state", "stateDelta", "args", "response", "artifactDelta", "customMetadata",
    "metadata", "partialArgs", "requestedAuthConfigs", "requestedToolConfirmations",
})

_SNAKE_RE = re.compile(r"_([a-z0-9])")


def camel(key: str) -> str:
  if "_" not in key or key.startswith("_"):
    return key
  return _SNAKE_RE.sub(lambda m: m.group(1).upper(), key)


def camelize(value: Any) -> Any:
  """Returns a copy with snake_case keys converted to camelCase."""
  if isinstance(value, list):
    return [camelize(v) for v in value]
  if not isinstance(value, dict):
    return value
  out = {}
  for key, child in value.items():
    new_key = camel(key) if isinstance(key, str) else key
    out[new_key] = child if new_key in OPAQUE_KEYS else camelize(child)
  return out


def key_style(value: Any) -> str:
  """'camelCase', 'snake_case', 'mixed' or 'none' for the keys of dicts in ``value``."""
  snake = camel_count = 0

  def walk(v: Any) -> None:
    nonlocal snake, camel_count
    if isinstance(v, list):
      for item in v:
        walk(item)
    elif isinstance(v, dict):
      for key, child in v.items():
        if isinstance(key, str):
          if re.search(r"[a-z]_[a-z]", key):
            snake += 1
          elif re.search(r"[a-z][A-Z]", key):
            camel_count += 1
        if key not in OPAQUE_KEYS:
          walk(child)

  walk(value)
  if snake and camel_count:
    return "mixed"
  if snake:
    return "snake_case"
  if camel_count:
    return "camelCase"
  return "none"


_FLAT_CALL_KEYS = {"name", "args", "id", "thoughtSignature"}


def part_shape(part: Any) -> str:
  """Wire shape of one content part, e.g. 'text', 'functionCall', 'flat-call'."""
  if not isinstance(part, dict):
    return type(part).__name__
  keys = set(camelize(part))
  if {"name", "args"} <= keys <= _FLAT_CALL_KEYS:
    return "flat-call"
  for key in ("text", "functionCall", "functionResponse", "inlineData", "fileData",
              "executableCode", "codeExecutionResult"):
    if key in keys:
      return key
  return "+".join(sorted(keys)) or "empty"


def canonical_part(part: dict[str, Any]) -> dict[str, Any]:
  """Rewrites known non-canonical part shapes to the canonical one.

  adk-rust 2.3.0 serializes function calls as a flat {"name", "args"} object
  instead of {"functionCall": {...}}. The call is still there, so behavior
  checks see it; the shape itself is reported as a format fact.
  """
  if part_shape(part) == "flat-call":
    return {"functionCall": {k: v for k, v in part.items() if k != "thoughtSignature"}}
  return part


def canonicalize_events(events: list[Any]) -> list[Any]:
  """camelCase keys and canonical part shapes, for checks and comparison."""
  out = []
  for event in camelize(events):
    if isinstance(event, dict) and isinstance(event.get("content"), dict):
      content = dict(event["content"])
      content["parts"] = [canonical_part(p) if isinstance(p, dict) else p
                          for p in content.get("parts") or []]
      event = dict(event, content=content)
    out.append(event)
  return out


def part_shapes(events: list[Any]) -> list[str]:
  """Sorted distinct part shapes in raw events (format fact)."""
  shapes = set()
  for event in events:
    if isinstance(event, dict):
      for part in (event.get("content") or {}).get("parts") or []:
        shapes.add(part_shape(part))
  return sorted(shapes)


def parts_of(event: dict[str, Any]) -> list[dict[str, Any]]:
  content = event.get("content") or {}
  return [p for p in content.get("parts") or [] if isinstance(p, dict)]


def event_views(events: list[Any]) -> dict[str, Any]:
  """Derived, order-preserving views over normalized events."""
  texts, authors, calls, responses = [], [], [], []
  for event in events:
    if not isinstance(event, dict):
      continue
    author = event.get("author")
    authors.append(author)
    for part in parts_of(event):
      if part.get("text") is not None and not part.get("thought"):
        texts.append(part["text"])
      if part.get("functionCall"):
        fc = part["functionCall"]
        calls.append({"author": author, "name": fc.get("name"), "args": fc.get("args"),
                      "id": fc.get("id")})
      if part.get("functionResponse"):
        fr = part["functionResponse"]
        responses.append({"author": author, "name": fr.get("name"),
                          "response": fr.get("response"), "id": fr.get("id")})
  return {
      "texts": texts,
      "authors": authors,
      "function_calls": calls,
      "function_responses": responses,
  }
