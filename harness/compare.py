"""Differential oracle: compares transcripts of the same scenario across SDKs.

Input: a directory tree containing ``<scenario>.<subject>.transcript.json``
files (for example ``bazel-testlogs``). Output: ``report.json`` and
``report.md`` with

* a matrix of per-test statuses (pass, fail, xfail, xpass, error), with steps
  that hit an unimplemented route marked as not-implemented;
* divergences: for each scenario, the aspects (one per step, the LLM
  requests, and wire format) where SDKs disagree, grouped by identical
  normalized value. A strict majority marks the minority as outliers; without
  one the finding is a *split* that needs a human decision.

Usage:
  bazel run //harness:compare -- --in "$PWD/bazel-testlogs" --out /tmp/report
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
from typing import Any

from harness import a2a_view, normalize, subjects

STATUS_MARK = {
    "pass": "✅", "fail": "❌", "xfail": "⚠️", "xpass": "❔", "error": "💥", "unsupported": "🚫",
    "missing": "·",
}
# Event fields that vary between runs and never indicate a behavior difference.
VOLATILE = {"id", "invocationId", "timestamp", "lastUpdateTime", "responseId", "modelVersion",
            "nodeInfo", "eventMetadata", "llmRequest", "groundingMetadata", "avgLogprobs"}
NOT_IMPLEMENTED = {404, 405, 501}


# Loading ---------------------------------------------------------------------------


def load_transcripts(root: str) -> dict[str, dict[str, dict[str, Any]]]:
  """Returns {scenario: {subject: transcript}}."""
  out: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
  for dirpath, _, files in os.walk(root, followlinks=True):
    for name in files:
      if not name.endswith(".transcript.json"):
        continue
      with open(os.path.join(dirpath, name), encoding="utf-8") as f:
        transcript = json.load(f)
      if subjects.involves_paused(transcript):
        continue  # left in bazel-testlogs by a run from before the pause
      out[transcript["scenario"]][transcript["subject"]] = transcript
  return dict(out)


# Signatures ------------------------------------------------------------------------


class IdMap:
  """Replaces generated ids with stable placeholders, keeping equality."""

  def __init__(self):
    self.ids: dict[str, str] = {}

  def __call__(self, value: Any) -> Any:
    if not value:
      return value
    if value not in self.ids:
      self.ids[value] = f"<id:{len(self.ids) + 1}>"
    return self.ids[value]


def _part_summary(part: dict[str, Any], ids: IdMap) -> Any:
  if part.get("thought"):
    return {"thought": True}
  if "text" in part:
    return {"text": part["text"]}
  if part.get("functionCall"):
    fc = part["functionCall"]
    return {"call": fc.get("name"), "args": fc.get("args"), "id": ids(fc.get("id"))}
  if part.get("functionResponse"):
    fr = part["functionResponse"]
    return {"response": fr.get("name"), "value": fr.get("response"), "id": ids(fr.get("id"))}
  return {"other": sorted(part)}


def _event_summary(event: Any, ids: IdMap) -> Any:
  if not isinstance(event, dict):
    return {"raw": event}
  actions = event.get("actions") or {}
  summary: dict[str, Any] = {
      "author": event.get("author"),
      "partial": bool(event.get("partial")),
      "parts": [_part_summary(p, ids) for p in normalize.parts_of(event)],
  }
  for key in ("stateDelta", "artifactDelta", "transferToAgent", "escalate", "skipSummarization"):
    if actions.get(key) not in (None, {}, False):
      summary[key] = actions[key]
  for key in ("errorCode", "errorMessage", "finishReason", "branch", "turnComplete"):
    if event.get(key) not in (None, ""):
      summary[key] = event[key]
  if event.get("longRunningToolIds"):
    summary["longRunningToolIds"] = [ids(i) for i in event["longRunningToolIds"]]
  return summary


def _step_result(step: dict[str, Any]) -> dict[str, Any] | None:
  if not step.get("exchanges"):
    return None
  return step["exchanges"][-1].get("response")


def step_signature(step: dict[str, Any], ids: IdMap) -> dict[str, Any]:
  """Behavior view of one step: status and normalized payload, no volatile fields."""
  if step["kind"].startswith("a2a."):
    return a2a_signature(step, ids)
  sig = _adk_step_signature(step, ids)
  traffic = traffic_signature(step, ids)
  if traffic is not None:
    sig["a2a"] = traffic
  return sig


def _adk_step_signature(step: dict[str, Any], ids: IdMap) -> dict[str, Any]:
  response = _step_result(step) or {}
  sig: dict[str, Any] = {"status": response.get("status"), "error": step.get("error") or None}
  if "events" in response:
    events = normalize.canonicalize_events(response["events"])
    sig["events"] = [_event_summary(e, ids) for e in events]
  elif "body" in response:
    body = normalize.camelize(response["body"])
    if isinstance(body, dict):
      if "events" in body:
        events = normalize.canonicalize_events(body.get("events") or [])
        body = dict(body, events=[_event_summary(e, ids) for e in events])
      sig["body"] = {k: v for k, v in body.items() if k not in VOLATILE and k != "id"}
      if "id" in body and step["kind"] != "adk.create_session":
        sig["body"]["id"] = ids(body["id"])
    else:
      sig["body"] = body
  return sig


# A2A signatures ---------------------------------------------------------------------

# Volatile A2A fields: generated ids are mapped to placeholders, timestamps dropped.
_A2A_ID_KEYS = {"id", "taskId", "contextId", "messageId", "artifactId"}
_A2A_DROP_KEYS = {"timestamp"}
# ADK metadata values that are generated per run (ids, session/user names).
_A2A_VOLATILE_META = re.compile(r"(_id|_session_id|_user_id|_invocation_id|_event_id)$")


def _a2a_scrub(value: Any, ids: IdMap) -> Any:
  if isinstance(value, list):
    return [_a2a_scrub(v, ids) for v in value]
  if not isinstance(value, dict):
    return value
  out = {}
  for key, child in value.items():
    if key in _A2A_DROP_KEYS:
      continue
    if key in _A2A_ID_KEYS and isinstance(child, str):
      out[key] = ids(child)
    elif key == "metadata" and isinstance(child, dict):
      out[key] = {k: ("<volatile>" if _A2A_VOLATILE_META.search(k) else _a2a_scrub(v, ids))
                  for k, v in sorted(child.items())}
    else:
      out[key] = _a2a_scrub(child, ids)
  return out


def a2a_step_view(step: dict[str, Any]) -> dict[str, Any] | None:
  """Recomputes the harness's A2A view of an a2a.* step from its raw exchange."""
  response = _step_result(step)
  if response is None:
    return None
  binding = step.get("binding") or "JSONRPC"
  content_type = response.get("content_type", "")
  if step["kind"] == "a2a.get_card":
    return a2a_view.card_view(response.get("status"), content_type, response.get("body"))
  if "events" in response:
    return a2a_view.stream_view(binding, response.get("status"), content_type, response["events"],
                                response.get("closed", True), response.get("sse_framing", ""))
  from harness import a2a_client  # pylint: disable=import-outside-toplevel
  result, error = a2a_client.unwrap(binding, response.get("body"))
  return a2a_view.response_view(binding, response.get("status"), content_type,
                                response.get("body"), result, error)


def a2a_signature(step: dict[str, Any], ids: IdMap) -> dict[str, Any]:
  """Behavior view of an a2a.* step: what a client can observe, minus volatility."""
  sig: dict[str, Any] = {"error": step.get("error") or None}
  if step["kind"] == "a2a.check_interfaces":
    responses = [ex.get("response", {}) for ex in step.get("exchanges", [])]
    sig["probes"] = [{"status": r.get("status"), "body": _a2a_scrub(r.get("body"), ids)}
                     for r in responses]
    return sig
  view = a2a_step_view(step)
  if view is None:
    return sig
  sig["status"] = view.get("status")
  if step["kind"] == "a2a.get_card":
    card = view.get("body") if isinstance(view.get("body"), dict) else {}
    interfaces = [{"binding": i.get("protocolBinding"), "version": i.get("protocolVersion")}
                  for i in card.get("supportedInterfaces") or [] if isinstance(i, dict)]
    sig["card"] = {
        "fields": view.get("field_names"),
        "name": card.get("name"),
        "description": card.get("description"),
        "capabilities": card.get("capabilities"),
        "interfaces": interfaces,
        "defaultInputModes": card.get("defaultInputModes"),
        "defaultOutputModes": card.get("defaultOutputModes"),
        "skills": [{k: s.get(k) for k in ("id", "name", "description", "tags")}
                   for s in card.get("skills") or [] if isinstance(s, dict)],
    }
    return sig
  if "events" in view:
    sig.update({
        "closed": view["closed"],
        "kinds": view["kinds"],
        "states": view["distinct_states"],
        "artifact_updates": [{k: u[k] for k in ("append", "lastChunk", "text")}
                             for u in view["artifact_updates"]],
        "status_texts": view["status_texts"],
        "message_texts": view["message_texts"],
        "data_parts": _a2a_scrub(view["data_parts"], ids),
        "error_codes": [e.get("code") for e in view["errors"]],
    })
    return sig
  task = view.get("task") or {}
  sig.update({
      "response_type": view.get("response_type"),
      "error_code": view.get("error_code"),
      "state": view.get("state"),
      "text_locations": view.get("text_locations"),
      "artifact_texts": view.get("artifact_texts"),
      "status_texts": view.get("status_texts"),
      "history_roles": view.get("history_roles"),
      "data_parts": _a2a_scrub(view.get("data_parts"), ids),
      "metadata_keys": view.get("metadata_keys"),
      "artifacts": [{"name": a.get("name"), "parts": len(a.get("parts") or [])}
                    for a in task.get("artifacts") or []],
  })
  if view.get("message"):
    sig["message"] = _a2a_scrub(view["message"], ids)
  return sig


def traffic_signature(step: dict[str, Any], ids: IdMap) -> dict[str, Any] | None:
  """What a client host put on the wire during one step (client scenarios)."""
  from harness import proxy  # pylint: disable=import-outside-toplevel
  traffic = step.get("a2a_traffic")
  if traffic is None:
    return None
  view = proxy.traffic_view(traffic)
  return {
      "card_fetches": view["card_fetches"] > 0,
      "methods": view["methods"],
      "versions": view["versions"],
      "messages": [{"role": m.get("role"),
                    "parts": [a2a_view.part_texts([p]) or sorted(p) for p in m.get("parts") or []],
                    "contextId": ids(m.get("contextId")) if m.get("contextId") else None,
                    "taskId": ids(m.get("taskId")) if m.get("taskId") else None}
                   for m in view["messages"]],
      "configuration": [_a2a_scrub((r.get("params") or {}).get("configuration"), ids)
                        for r in view["requests"] if isinstance(r.get("params"), dict)],
  }


def _schema(schema: Any) -> Any:
  """Canonical schema: lower-case types, drop empty fields, sort keys."""
  if isinstance(schema, list):
    return [_schema(s) for s in schema]
  if not isinstance(schema, dict):
    return schema.lower() if isinstance(schema, str) and schema.isupper() else schema
  return {k: _schema(v) for k, v in sorted(schema.items()) if v not in (None, "", [], {})}


def llm_signature(transcript: dict[str, Any], ids: IdMap) -> dict[str, Any]:
  requests = []
  for request in transcript.get("llm", {}).get("requests", []):
    body = request.get("body") or {}
    system = body.get("systemInstruction") or body.get("system_instruction") or {}
    contents = []
    for content in body.get("contents") or []:
      contents.append({
          "role": content.get("role"),
          "parts": [_part_summary(normalize.camelize(p), ids) for p in content.get("parts") or []],
      })
    tools = []
    for tool in body.get("tools") or []:
      tool = normalize.camelize(tool)
      for decl in tool.get("functionDeclarations") or []:
        tools.append({
            "name": decl.get("name"),
            "description": decl.get("description"),
            "parameters": _schema(decl.get("parameters") or decl.get("parametersJsonSchema")),
        })
      for other in sorted(k for k in tool if k != "functionDeclarations"):
        tools.append({"builtin": other})
    requests.append({
        "agent": request.get("agent"),
        "method": request.get("method"),
        "systemInstruction": [p.get("text") for p in system.get("parts") or [] if "text" in p],
        "contents": contents,
        "tools": tools,
        "generationConfig": _schema(body.get("generationConfig") or body.get("generation_config")),
    })
  return {"request_count": len(requests), "requests": requests}


def _type_name(value: Any) -> str:
  if value is None:
    return "missing"
  if isinstance(value, bool):
    return "boolean"
  if isinstance(value, (int, float)):
    return "number"
  return type(value).__name__


def format_facts(transcript: dict[str, Any]) -> dict[str, Any]:
  """Wire-format facts per step, recorded before normalization."""
  facts: dict[str, Any] = {}
  for step in transcript.get("steps", []):
    response = _step_result(step) or {}
    entry: dict[str, Any] = {"content_type": (response.get("content_type") or "").split(";")[0].lower()}
    if step["kind"].startswith("a2a."):
      payload = response.get("events") if "events" in response else response.get("body")
      wrapped = step["kind"] in ("a2a.send_message", "a2a.stream", "a2a.subscribe")
      entry.update(a2a_view.format_facts(payload, wrapped=wrapped))
      if "events" in response:
        entry["sse_framing"] = response.get("sse_framing")
        entry["sse_line_ending"] = response.get("sse_line_ending")
      if step["kind"] == "a2a.raw" or (isinstance(payload, dict) and "error" in payload):
        entry["http_status"] = response.get("status")
      facts[step["name"]] = entry
      continue
    payload = response.get("events") if "events" in response else response.get("body")
    if payload is not None:
      entry["key_style"] = normalize.key_style(payload)
    events = response.get("events")
    if events is None and isinstance(response.get("body"), dict):
      events = response["body"].get("events")
    if events:
      entry["part_shapes"] = normalize.part_shapes(events)
      first = next((e for e in events if isinstance(e, dict)), {})
      entry["event_keys"] = sorted(normalize.camelize(first))
      entry["timestamp_type"] = _type_name(first.get("timestamp"))
    facts[step["name"]] = entry
  return facts


# Comparison ------------------------------------------------------------------------


def canonical(value: Any) -> str:
  return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def diff_paths(values: dict[str, Any], prefix: str = "", limit: int = 12) -> list[dict[str, Any]]:
  """Paths where the per-subject values differ, with each subject's value."""
  out: list[dict[str, Any]] = []
  subjects = list(values)
  first = values[subjects[0]]
  if all(isinstance(v, dict) for v in values.values()):
    keys = sorted(set().union(*[v.keys() for v in values.values()]))
    for key in keys:
      sub = {s: values[s].get(key, "<absent>") for s in subjects}
      if len({canonical(v) for v in sub.values()}) > 1:
        out.extend(diff_paths(sub, f"{prefix}.{key}" if prefix else key, limit - len(out)))
      if len(out) >= limit:
        break
    return out
  if all(isinstance(v, list) for v in values.values()) and len({len(v) for v in values.values()}) == 1:
    for i in range(len(first)):
      sub = {s: values[s][i] for s in subjects}
      if len({canonical(v) for v in sub.values()}) > 1:
        out.extend(diff_paths(sub, f"{prefix}[{i}]", limit - len(out)))
      if len(out) >= limit:
        break
    return out
  return [{"path": prefix or "$", "values": values}]


def compare_aspect(scenario: str, aspect: str, values: dict[str, Any]) -> dict[str, Any] | None:
  if len(values) < 2:
    return None
  groups: dict[str, list[str]] = collections.defaultdict(list)
  for subject, value in sorted(values.items()):
    groups[canonical(value)].append(subject)
  if len(groups) == 1:
    return None
  ranked = sorted(groups.values(), key=len, reverse=True)
  majority = ranked[0] if len(ranked[0]) * 2 > len(values) else None
  outliers = sorted(s for g in ranked if g is not majority for s in g) if majority else []
  details = diff_paths(values)
  fingerprint = hashlib.sha1(canonical(
      [scenario, aspect, outliers or sorted(map(sorted, ranked)), [d["path"] for d in details]]
  ).encode()).hexdigest()[:16]
  return {
      "fingerprint": fingerprint,
      "scenario": scenario,
      "aspect": aspect,
      "kind": "divergence" if majority else "split",
      "majority": majority or [],
      "outliers": outliers,
      "groups": ranked,
      "details": details,
  }


def not_implemented_steps(per_subject: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
  """{subject: [step names]} where the subject got 404/405/501 but another got 2xx."""
  statuses: dict[str, dict[str, int]] = collections.defaultdict(dict)
  for subject, transcript in per_subject.items():
    for step in transcript.get("steps", []):
      response = _step_result(step) or {}
      if response.get("status") is not None:
        statuses[step["name"]][subject] = response["status"]
  out: dict[str, list[str]] = collections.defaultdict(list)
  for step, by_subject in statuses.items():
    if any(200 <= code < 300 for code in by_subject.values()):
      for subject, code in by_subject.items():
        if code in NOT_IMPLEMENTED:
          out[subject].append(step)
  return dict(out)


_INDEX_RE = re.compile(r"\[\d+\]")


def recurring(findings: list[dict[str, Any]], top: int = 25) -> list[dict[str, Any]]:
  """Groups divergence details by (aspect kind, index-free path, odd one out).

  One root cause (e.g. an enum serialized differently) shows up in many
  scenarios; this ranks them so a report reader starts with root causes.
  """
  groups: dict[tuple[str, str, str], dict[str, Any]] = {}
  for f in findings:
    kind = f["aspect"].split(":", 1)[0]
    odd = ",".join(f["outliers"]) if f["outliers"] else "split"
    for d in f["details"]:
      path = _INDEX_RE.sub("[*]", d["path"])
      if kind == "step":
        path = path.split(".", 1)[-1] if "." in path else path
      key = (kind, path, odd)
      g = groups.setdefault(key, {"kind": kind, "path": path, "outliers": odd, "scenarios": [],
                                  "example": d["values"]})
      if f["scenario"] not in g["scenarios"]:
        g["scenarios"].append(f["scenario"])
  ranked = sorted(groups.values(), key=lambda g: (-len(g["scenarios"]), g["path"]))
  return [dict(g, count=len(g["scenarios"])) for g in ranked[:top]]


def subject_role(transcript: dict[str, Any]) -> str:
  """'adk' (an ADK host), 'control' (a bare A2A host) or 'pair' (client-to-server)."""
  topology = transcript.get("topology") or {}
  if topology.get("server"):
    return "pair"
  return "control" if topology.get("kind") == "bare" else "adk"


def _compare_scenario(scenario: str, per: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
  """Differential findings for one scenario over the given transcripts."""
  findings = []
  if len(per) < 2:
    return findings
  any_t = next(iter(per.values()))
  for name in [s["name"] for s in any_t.get("steps", [])]:
    values = {}
    for subject, t in per.items():
      step = next((s for s in t.get("steps", []) if s["name"] == name), None)
      if step is not None:
        values[subject] = step_signature(step, IdMap())
    finding = compare_aspect(scenario, f"step:{name}", values)
    if finding:
      findings.append(finding)
  finding = compare_aspect(scenario, "llm", {s: llm_signature(t, IdMap()) for s, t in per.items()})
  if finding:
    findings.append(finding)
  finding = compare_aspect(scenario, "format", {s: format_facts(t) for s, t in per.items()})
  if finding:
    findings.append(finding)
  return findings


def _compare_clients(scenario: str, pairs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
  """Client differential for a client scenario: clients are the voters.

  Each client's value is its behavior against every ADK server ({server:
  signature}), so a difference that only shows against one server still
  separates the clients, and server-side differences (covered by the A2A
  server scenarios) do not.
  """
  by_client: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
  for t in pairs.values():
    topo = t.get("topology") or {}
    if topo.get("server_kind") != "bare":
      by_client[topo["client"]][topo["server"]] = t
  if len(by_client) < 2:
    return []
  servers = sorted(set.intersection(*[set(v) for v in by_client.values()]))
  findings = []
  any_t = next(iter(pairs.values()))
  for name in [s["name"] for s in any_t.get("steps", [])]:
    values = {}
    for client, per_server in by_client.items():
      values[client] = {}
      for server in servers:
        step = next((s for s in per_server[server].get("steps", []) if s["name"] == name), None)
        values[client][server] = step_signature(step, IdMap()) if step else None
    finding = compare_aspect(scenario, f"step:{name}", values)
    if finding:
      findings.append(finding)
  for aspect, fn in (("llm", llm_signature), ("format", lambda t, _ids: format_facts(t))):
    values = {c: {srv: fn(per[srv], IdMap()) for srv in servers} for c, per in by_client.items()}
    finding = compare_aspect(scenario, aspect, values)
    if finding:
      findings.append(finding)
  return findings


def _ran(t: dict[str, Any]) -> bool:
  """False for a run skipped as a capability gap (UNSUPPORTED, no host started):
  it has no behavior to compare, so it does not vote in the differential."""
  return t.get("ran", True)


def _status(t: dict[str, Any] | None) -> str:
  if not t:
    return "missing"
  return t.get("status") or ("pass" if t.get("passed") else "fail")


def build_report(transcripts: dict[str, dict[str, dict[str, Any]]],
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
  """Matrix, differential findings and interop grid.

  The differential oracle compares ADK hosts only: bare A2A hosts are a
  control group (attribution), not voters. In client scenarios the voters are
  the clients, each with its behavior against every ADK server.
  """
  roles: dict[str, str] = {}
  for per in transcripts.values():
    for subject, t in per.items():
      roles[subject] = subject_role(t)
  adk = sorted(s for s, r in roles.items() if r == "adk")
  controls = sorted(s for s, r in roles.items() if r == "control")
  subjects = adk + controls
  matrix, findings, interop = {}, [], {}
  counts = {s: collections.Counter() for s in subjects}
  pair_counts: collections.Counter = collections.Counter()
  for scenario in sorted(transcripts):
    per = transcripts[scenario]
    any_t = next(iter(per.values()))
    pairs = {s: t for s, t in per.items() if roles[s] == "pair"}
    if pairs:
      clients = sorted({(t.get("topology") or {}).get("client") for t in pairs.values()})
      servers = sorted({(t.get("topology") or {}).get("server") for t in pairs.values()},
                       key=lambda x: (roles.get(x) == "control", x))
      cells: dict[str, dict[str, Any]] = {c: {} for c in clients}
      for subject, t in pairs.items():
        topo = t.get("topology") or {}
        status = _status(t)
        pair_counts[status] += 1
        cells[topo["client"]][topo["server"]] = {
            "subject": subject, "status": status, "failures": t.get("failures", []),
            "known_divergence": t.get("known_divergence"),
            "server_kind": topo.get("server_kind")}
      interop[scenario] = {"area": any_t.get("area", ""), "behaviors": any_t.get("behaviors", []),
                           "clients": clients, "servers": servers, "cells": cells}
      findings.extend(_compare_clients(scenario, {s: t for s, t in pairs.items() if _ran(t)}))
      continue
    ni = not_implemented_steps({s: t for s, t in per.items() if s in subjects})
    row = {}
    for subject in subjects:
      t = per.get(subject)
      status = _status(t)
      if not (status == "missing" and roles[subject] == "control"):
        counts[subject][status] += 1  # bare hosts only run the A2A server scenarios
      unsupported = [st["name"] for st in (t or {}).get("steps", [])
                     if str(st.get("error", "")).startswith("unsupported:")]
      row[subject] = {
          "status": status,
          "failures": t.get("failures", []) if t else [],
          "not_implemented": sorted(set(ni.get(subject, []) + unsupported)),
          "known_divergence": (t or {}).get("known_divergence"),
      }
    matrix[scenario] = {"area": any_t.get("area", ""), "behaviors": any_t.get("behaviors", []),
                        "subjects": row}
    findings.extend(_compare_scenario(
        scenario, {s: t for s, t in per.items() if roles[s] == "adk" and _ran(t)}))
  return {
      "meta": meta or {},
      "subjects": subjects,
      "adk_subjects": adk,
      "control_subjects": controls,
      "counts": {s: dict(c) for s, c in counts.items()},
      "pair_counts": dict(pair_counts),
      "matrix": matrix,
      "interop": interop,
      "findings": findings,
      "recurring": recurring(findings),
  }


# Rendering -------------------------------------------------------------------------


def _short(value: Any, width: int = 90) -> str:
  text = canonical(value)
  return text if len(text) <= width else text[: width - 1] + "…"


def render_markdown(report: dict[str, Any]) -> str:
  subjects = report["subjects"]
  meta = report.get("meta") or {}
  lines = ["# adk-monitor report", ""]
  if meta:
    lines += [f"- **{k}**: {_short(v, 200)}" for k, v in sorted(meta.items())] + [""]
  lines += ["## Summary", "", "| Subject | " + " | ".join(STATUS_MARK) + " |",
            "|---|" + "---|" * len(STATUS_MARK)]
  for s in subjects:
    c = report["counts"][s]
    lines.append(f"| {s} | " + " | ".join(str(c.get(k, 0)) for k in STATUS_MARK) + " |")
  if report.get("pair_counts"):
    c = report["pair_counts"]
    lines.append("| *client-to-server pairs* | " + " | ".join(str(c.get(k, 0)) for k in STATUS_MARK) + " |")
  lines += ["", "Legend: " + ", ".join(f"{m} {k}" for k, m in STATUS_MARK.items()) +
            "; 🚫 after a status: a step hit an unimplemented route or binding.", ""]
  if report.get("control_subjects"):
    lines += ["Control subjects (bare A2A hosts, not part of the differential oracle): " +
              ", ".join(f"`{s}`" for s in report["control_subjects"]) + ".", ""]
  lines += ["## Matrix", "", "| Scenario | Area | " + " | ".join(subjects) + " |",
            "|---|---|" + "---|" * len(subjects)]
  for scenario, row in report["matrix"].items():
    cells = []
    for s in subjects:
      cell = row["subjects"][s]
      mark = STATUS_MARK.get(cell["status"], "?")
      if cell["not_implemented"] and cell["status"] != "unsupported":
        mark += "🚫"
      cells.append(mark)
    lines.append(f"| `{scenario}` | {row['area']} | " + " | ".join(cells) + " |")
  for scenario, grid in (report.get("interop") or {}).items():
    if scenario == next(iter(report["interop"])):
      lines += ["", "## A2A client × server", "",
                "Rows: the client (an ADK host's RemoteA2aAgent). Columns: the server. "
                "A failing row points at the client, a failing column at the server.", ""]
    lines += [f"### `{scenario}`", "", "| client \\ server | " + " | ".join(grid["servers"]) + " |",
              "|---|" + "---|" * len(grid["servers"])]
    for client in grid["clients"]:
      cells = [STATUS_MARK.get(grid["cells"][client].get(srv, {}).get("status", "missing"), "?")
               for srv in grid["servers"]]
      lines.append(f"| {client} | " + " | ".join(cells) + " |")
    lines.append("")
  failing = [(sc, s, row["subjects"][s]) for sc, row in report["matrix"].items() for s in subjects
             if row["subjects"][s]["status"] in ("fail", "error", "xpass")]
  failing += [(sc, cell["subject"], cell) for sc, grid in (report.get("interop") or {}).items()
              for row in grid["cells"].values() for cell in row.values()
              if cell["status"] in ("fail", "error", "xpass")]
  if failing:
    lines += ["", "## Failures", ""]
    for scenario, subject, cell in failing:
      lines.append(f"- `{scenario}` on **{subject}** ({cell['status']}): " +
                   "; ".join(f"`{f}`" for f in cell["failures"][:6]))
  if report.get("recurring"):
    lines += ["", "## Recurring differences", "",
              "Same path and same odd one out across scenarios; usually one root cause each.", "",
              "| Scenarios | Aspect | Path | Odd one out | Example |", "|---|---|---|---|---|"]
    for g in report["recurring"]:
      example = "; ".join(f"{s}={_short(v, 40)}" for s, v in sorted(g["example"].items()))
      lines.append(f"| {g['count']} | {g['kind']} | `{g['path']}` | {g['outliers']} | {example} |")
  lines += ["", f"## Divergences ({len(report['findings'])})", ""]
  for f in report["findings"]:
    who = (f"outliers: **{', '.join(f['outliers'])}**" if f["outliers"]
           else "split: " + " vs ".join("/".join(g) for g in f["groups"]))
    lines.append(f"### `{f['scenario']}` · {f['aspect']} · {f['kind']} ({who})")
    lines.append("")
    for d in f["details"][:8]:
      lines.append(f"- `{d['path']}`: " + "; ".join(
          f"{s}={_short(v, 70)}" for s, v in sorted(d["values"].items())))
    lines.append("")
  return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description="Compare transcripts across SDKs.")
  parser.add_argument("--in", dest="inp", required=True, help="directory with *.transcript.json")
  parser.add_argument("--out", required=True, help="output directory for report.{json,md}")
  parser.add_argument("--meta", default=None, help="JSON file with run metadata to embed")
  args = parser.parse_args(argv)
  meta = None
  if args.meta:
    with open(args.meta, encoding="utf-8") as f:
      meta = json.load(f)
  transcripts = load_transcripts(args.inp)
  if not transcripts:
    print(f"no transcripts under {args.inp}", file=sys.stderr)
    return 1
  report = build_report(transcripts, meta)
  os.makedirs(args.out, exist_ok=True)
  with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, sort_keys=True, ensure_ascii=False)
  with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8") as f:
    f.write(render_markdown(report))
  c = {s: dict(v) for s, v in report["counts"].items()}
  print(f"{len(transcripts)} scenarios, {len(report['findings'])} divergences, counts={c}")
  print(f"wrote {args.out}/report.md and report.json")
  return 0


if __name__ == "__main__":
  sys.exit(main())
