"""Normalized views of A2A responses, for checks and cross-SDK comparison.

Behavior checks run on a canonical A2A v1 view, the same idea as
``normalize`` for ADK events:

* enum values become v1 names (``completed`` -> ``TASK_STATE_COMPLETED``,
  ``agent`` -> ``ROLE_AGENT``);
* the 0.3 ``kind`` discriminator is dropped and 0.3 part/event shapes are
  rewritten to v1 ones (``{kind: text, text}`` -> ``{text}``; a stream event
  ``{kind: status-update, ...}`` -> ``{statusUpdate: {...}}``);
* keys are camelCase.

What the server actually sent (enum style, ``kind`` fields, key style) is kept
as *format facts* so spec checks (A2A-SRV-032) and the comparison report can
still see it.
"""

from __future__ import annotations

from typing import Any

from harness import normalize

TERMINAL_STATES = frozenset({
    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
})
INTERRUPTED_STATES = frozenset({"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"})

_V03_STATES = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "input_required": "TASK_STATE_INPUT_REQUIRED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
    "auth_required": "TASK_STATE_AUTH_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "canceled": "TASK_STATE_CANCELED",
    "cancelled": "TASK_STATE_CANCELED",
    "failed": "TASK_STATE_FAILED",
    "rejected": "TASK_STATE_REJECTED",
    "unknown": "TASK_STATE_UNSPECIFIED",
}
_V03_ROLES = {"user": "ROLE_USER", "agent": "ROLE_AGENT"}
_V03_EVENT_KINDS = {"task": "task", "message": "message", "status-update": "statusUpdate",
                    "artifact-update": "artifactUpdate"}
_EVENT_KINDS = ("task", "message", "statusUpdate", "artifactUpdate")
# User data: keys inside are kept verbatim.
_OPAQUE = frozenset({"metadata", "data"})


def _camelize(value: Any) -> Any:
  if isinstance(value, list):
    return [_camelize(v) for v in value]
  if not isinstance(value, dict):
    return value
  out = {}
  for key, child in value.items():
    new_key = normalize.camel(key) if isinstance(key, str) else key
    out[new_key] = child if new_key in _OPAQUE else _camelize(child)
  return out


def state_name(value: Any) -> Any:
  if isinstance(value, str):
    return _V03_STATES.get(value, value)
  return value


def role_name(value: Any) -> Any:
  if isinstance(value, str):
    return _V03_ROLES.get(value, value)
  return value


def canonical_part(part: Any) -> Any:
  """v1 part: exactly one of text/raw/url/data, plus mediaType/filename/metadata."""
  if not isinstance(part, dict):
    return part
  part = dict(part)
  kind = part.pop("kind", None)
  if kind == "file" and isinstance(part.get("file"), dict):
    f = part.pop("file")
    if "bytes" in f:
      part["raw"] = f["bytes"]
    if "uri" in f:
      part["url"] = f["uri"]
    if f.get("mimeType"):
      part["mediaType"] = f["mimeType"]
    if f.get("name"):
      part["filename"] = f["name"]
  return part


def canonical_message(message: Any) -> Any:
  if not isinstance(message, dict):
    return message
  message = dict(message)
  message.pop("kind", None)
  if "role" in message:
    message["role"] = role_name(message["role"])
  message["parts"] = [canonical_part(p) for p in message.get("parts") or []]
  return message


def canonical_status(status: Any) -> Any:
  if not isinstance(status, dict):
    return status
  status = dict(status)
  status["state"] = state_name(status.get("state"))
  if status.get("message") is not None:
    status["message"] = canonical_message(status["message"])
  return status


def canonical_artifact(artifact: Any) -> Any:
  if not isinstance(artifact, dict):
    return artifact
  artifact = dict(artifact)
  artifact["parts"] = [canonical_part(p) for p in artifact.get("parts") or []]
  return artifact


def canonical_task(task: Any) -> Any:
  if not isinstance(task, dict):
    return task
  task = dict(task)
  task.pop("kind", None)
  if "status" in task:
    task["status"] = canonical_status(task["status"])
  task["artifacts"] = [canonical_artifact(a) for a in task.get("artifacts") or []]
  task["history"] = [canonical_message(m) for m in task.get("history") or []]
  return task


def canonical_event(event: Any) -> dict[str, Any]:
  """One SendMessage result or stream event -> ``{kind: <v1 field>, <field>: obj}``.

  Accepts the v1 oneof wrapper (``{"task": {...}}``) and 0.3 objects with a
  ``kind`` discriminator. Unknown shapes come back as ``{kind: "unknown"}``.
  """
  if not isinstance(event, dict):
    return {"kind": "unknown", "raw": event}
  event = _camelize(event)
  for kind in _EVENT_KINDS:
    if isinstance(event.get(kind), dict) and len(event) == 1:
      return {"kind": kind, kind: _canonical_by_kind(kind, event[kind])}
  v03 = _V03_EVENT_KINDS.get(str(event.get("kind")))
  if v03:
    return {"kind": v03, v03: _canonical_by_kind(v03, event)}
  # A bare Task / Message without a wrapper (some servers return those).
  if "status" in event and "id" in event:
    return {"kind": "task", "task": canonical_task(event)}
  if "parts" in event and "role" in event:
    return {"kind": "message", "message": canonical_message(event)}
  return {"kind": "unknown", "raw": event}


def _canonical_by_kind(kind: str, obj: dict[str, Any]) -> dict[str, Any]:
  obj = dict(obj)
  obj.pop("kind", None)
  if kind == "task":
    return canonical_task(obj)
  if kind == "message":
    return canonical_message(obj)
  if kind == "statusUpdate":
    obj["status"] = canonical_status(obj.get("status"))
    return obj
  if kind == "artifactUpdate":
    obj["artifact"] = canonical_artifact(obj.get("artifact"))
    return obj
  return obj


# Format facts -----------------------------------------------------------------------


def _walk(value: Any):
  if isinstance(value, dict):
    yield value
    for key, child in value.items():
      if key not in _OPAQUE:
        yield from _walk(child)
  elif isinstance(value, list):
    for item in value:
      yield from _walk(item)


def enum_style(value: Any) -> str:
  """'v1' (TASK_STATE_*/ROLE_*), 'v0.3' (completed/agent), 'mixed' or 'none'."""
  v1 = v03 = 0
  for obj in _walk(value):
    for key in ("state", "role"):
      enum = obj.get(key)
      if isinstance(enum, str):
        if enum.startswith(("TASK_STATE_", "ROLE_")):
          v1 += 1
        else:
          v03 += 1
  if v1 and v03:
    return "mixed"
  return "v1" if v1 else ("v0.3" if v03 else "none")


def has_kind_field(value: Any) -> bool:
  return any("kind" in obj for obj in _walk(value))


def result_shape(result: Any) -> str:
  """How a SendMessage result / stream event is wrapped on the wire.

  v1 wraps it in a oneof (``{"task": {...}}``, ``{"statusUpdate": {...}}``);
  0.3 used a ``kind`` discriminator; a bare Task or Message has neither.
  """
  if not isinstance(result, dict):
    return "none"
  if len(result) == 1 and next(iter(result)) in _EVENT_KINDS:
    return "oneof"
  if "kind" in result:
    return "kind"
  return "bare"


def _results(payload: Any) -> list[Any]:
  items = payload if isinstance(payload, list) else [payload]
  return [p.get("result") if isinstance(p, dict) and "jsonrpc" in p else p for p in items]


def format_facts(payload: Any, wrapped: bool = False) -> dict[str, Any]:
  """Wire-format facts. ``wrapped``: the payload carries SendMessage/stream results."""
  facts = {
      "enum_style": enum_style(payload),
      "kind_field": has_kind_field(payload),
      "key_style": normalize.key_style(payload),
  }
  if wrapped:
    shapes = sorted({result_shape(r) for r in _results(payload) if r is not None})
    facts["result_shape"] = shapes[0] if len(shapes) == 1 else ("+".join(shapes) or "none")
  return facts


# Derived views ----------------------------------------------------------------------


def part_texts(parts: list[Any]) -> list[str]:
  return [p["text"] for p in parts or [] if isinstance(p, dict) and isinstance(p.get("text"), str)]


def data_parts(obj: Any) -> list[Any]:
  """Every data part (``{data: ...}``) anywhere inside ``obj``, in order."""
  out = []
  for d in _walk(obj):
    if "data" in d and ("metadata" in d or "mediaType" in d or set(d) <= {"data", "metadata", "mediaType"}):
      out.append(d)
  return out


def task_views(task: dict[str, Any] | None) -> dict[str, Any]:
  if not task:
    return {"state": None, "artifact_texts": [], "status_texts": [], "history_texts": [],
            "agent_history_texts": [], "text_locations": [], "data_parts": [], "artifact_count": 0}
  status = task.get("status") or {}
  status_message = status.get("message") or {}
  artifact_texts = [t for a in task.get("artifacts") or [] for t in part_texts(a.get("parts"))]
  status_texts = part_texts(status_message.get("parts"))
  history = task.get("history") or []
  agent_history = [t for m in history if m.get("role") == "ROLE_AGENT" for t in part_texts(m.get("parts"))]
  locations = []
  if artifact_texts:
    locations.append("artifact")
  if status_texts:
    locations.append("status")
  if agent_history:
    locations.append("history")
  return {
      "state": status.get("state"),
      "artifact_texts": artifact_texts,
      "status_texts": status_texts,
      "history_texts": [t for m in history for t in part_texts(m.get("parts"))],
      "agent_history_texts": agent_history,
      "history_roles": [m.get("role") for m in history],
      "text_locations": locations,
      "data_parts": data_parts({"status": status, "artifacts": task.get("artifacts")}),
      "artifact_count": len(task.get("artifacts") or []),
      "metadata_keys": sorted((task.get("metadata") or {}).keys()),
  }


def response_view(binding: str, status: int, content_type: str, payload: Any,
                  result: Any, error: Any) -> dict[str, Any]:
  """View of a unary response (SendMessage, GetTask, CancelTask, ListTasks)."""
  view: dict[str, Any] = {
      "status": status,
      "content_type": content_type,
      "binding": binding,
      "body": payload,
      "error": _camelize(error) if error is not None else None,
      "error_code": (error or {}).get("code") if isinstance(error, dict) else None,
      "format": format_facts(payload),
  }
  if result is not None and isinstance(result, dict) and ("task" in result or "message" in result
                                                          or "status" in result or "parts" in result):
    view["format"]["result_shape"] = result_shape(result)
  event = canonical_event(result) if result is not None else None
  view["response_type"] = event["kind"] if event else None
  task = event.get("task") if event else None
  message = event.get("message") if event else None
  view["task"] = task
  view["message"] = message
  view["result"] = event.get(event["kind"]) if event and event["kind"] != "unknown" else result
  view.update(task_views(task))
  if message:
    view["message_texts"] = part_texts(message.get("parts"))
    if "message" not in view["text_locations"] and view["message_texts"]:
      view["text_locations"] = view["text_locations"] + ["message"]
  view["task_id"] = (task or {}).get("id") or (message or {}).get("taskId")
  view["context_id"] = (task or {}).get("contextId") or (message or {}).get("contextId")
  return view


def stream_view(binding: str, status: int, content_type: str, payloads: list[Any],
                closed: bool, framing: str) -> dict[str, Any]:
  """View of a streaming response (SendStreamingMessage, SubscribeToTask)."""
  events, errors = [], []
  for payload in payloads:
    if binding == "JSONRPC" and isinstance(payload, dict):
      if payload.get("error") is not None:
        errors.append(_camelize(payload["error"]))
        continue
      payload = payload.get("result")
    events.append(canonical_event(payload))
  kinds = [e["kind"] for e in events]
  states: list[Any] = []
  artifacts: dict[str, dict[str, Any]] = {}
  updates = []
  task_id = context_id = None
  for e in events:
    obj = e.get(e["kind"]) or {}
    task_id = task_id or obj.get("id") if e["kind"] == "task" else task_id or obj.get("taskId")
    context_id = context_id or obj.get("contextId")
    if e["kind"] == "task":
      states.append((obj.get("status") or {}).get("state"))
    elif e["kind"] == "statusUpdate":
      states.append((obj.get("status") or {}).get("state"))
    elif e["kind"] == "artifactUpdate":
      artifact = obj.get("artifact") or {}
      aid = artifact.get("artifactId") or f"<artifact {len(artifacts)}>"
      texts = part_texts(artifact.get("parts"))
      entry = artifacts.setdefault(aid, {"name": artifact.get("name"), "text": "", "chunks": 0})
      entry["text"] = (entry["text"] if obj.get("append") else "") + "".join(texts)
      entry["chunks"] += 1
      updates.append({"append": bool(obj.get("append")), "lastChunk": bool(obj.get("lastChunk")),
                      "text": "".join(texts), "artifact": aid})
  final_state = next((s for s in reversed(states) if s), None)
  last = events[-1] if events else None
  last_task = next((e["task"] for e in reversed(events) if e["kind"] == "task"), None)
  view = {
      "status": status,
      "content_type": content_type,
      "binding": binding,
      "closed": closed,
      "sse_framing": framing,
      "events": events,
      "event_count": len(events),
      "kinds": kinds,
      "first_kind": kinds[0] if kinds else None,
      "states": states,
      "distinct_states": [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]],
      "final_state": final_state,
      "terminal": final_state in TERMINAL_STATES or final_state in INTERRUPTED_STATES,
      "last_kind": last["kind"] if last else None,
      "artifacts": list(artifacts.values()),
      "artifact_text": "".join(a["text"] for a in artifacts.values()),
      "artifact_updates": updates,
      "status_texts": [t for e in events if e["kind"] == "statusUpdate"
                       for t in part_texts(((e["statusUpdate"].get("status") or {}).get("message") or {}).get("parts"))],
      "message_texts": [t for e in events if e["kind"] == "message" for t in part_texts(e["message"].get("parts"))],
      "data_parts": data_parts([e.get(e["kind"]) for e in events]),
      "errors": errors,
      "error_code": errors[0].get("code") if errors else None,
      "task_id": task_id,
      "context_id": context_id,
      "last_task": last_task,
      "format": format_facts(payloads, wrapped=True),
  }
  return view


def card_view(status: int, content_type: str, card: Any) -> dict[str, Any]:
  from harness import a2a_client  # pylint: disable=import-outside-toplevel
  card_c = _camelize(card) if isinstance(card, dict) else card
  view = {
      "status": status,
      "content_type": content_type,
      "body": card_c,
      "format": {"key_style": normalize.key_style(card)},
  }
  if isinstance(card_c, dict):
    view["interfaces"] = a2a_client.interfaces(card_c)
    view["bindings"] = sorted(view["interfaces"])
    view["skill_names"] = [s.get("name") for s in card_c.get("skills") or [] if isinstance(s, dict)]
    view["skill_ids"] = [s.get("id") for s in card_c.get("skills") or [] if isinstance(s, dict)]
    view["field_names"] = sorted(card_c)
  return view
