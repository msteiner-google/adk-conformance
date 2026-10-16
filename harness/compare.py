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

from harness import normalize

STATUS_MARK = {
    "pass": "✅", "fail": "❌", "xfail": "⚠️", "xpass": "❔", "error": "💥", "missing": "·",
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


def build_report(transcripts: dict[str, dict[str, dict[str, Any]]],
                 meta: dict[str, Any] | None = None) -> dict[str, Any]:
  subjects = sorted({s for per in transcripts.values() for s in per})
  matrix, findings = {}, []
  counts = {s: collections.Counter() for s in subjects}
  for scenario in sorted(transcripts):
    per = transcripts[scenario]
    ni = not_implemented_steps(per)
    row = {}
    for subject in subjects:
      t = per.get(subject)
      status = (t.get("status") or ("pass" if t.get("passed") else "fail")) if t else "missing"
      counts[subject][status] += 1
      row[subject] = {
          "status": status,
          "failures": t.get("failures", []) if t else [],
          "not_implemented": ni.get(subject, []),
          "known_divergence": (t or {}).get("known_divergence"),
      }
    any_t = next(iter(per.values()))
    matrix[scenario] = {"area": any_t.get("area", ""), "behaviors": any_t.get("behaviors", []),
                        "subjects": row}
    # Aspects.
    step_names = [s["name"] for s in any_t.get("steps", [])]
    for name in step_names:
      values = {}
      for subject, t in per.items():
        ids = IdMap()
        step = next((s for s in t.get("steps", []) if s["name"] == name), None)
        if step is not None:
          values[subject] = step_signature(step, ids)
      finding = compare_aspect(scenario, f"step:{name}", values)
      if finding:
        findings.append(finding)
    llm_values = {s: llm_signature(t, IdMap()) for s, t in per.items()}
    finding = compare_aspect(scenario, "llm", llm_values)
    if finding:
      findings.append(finding)
    format_values = {s: format_facts(t) for s, t in per.items()}
    finding = compare_aspect(scenario, "format", format_values)
    if finding:
      findings.append(finding)
  return {
      "meta": meta or {},
      "subjects": subjects,
      "counts": {s: dict(c) for s, c in counts.items()},
      "matrix": matrix,
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
  lines += ["", "Legend: " + ", ".join(f"{m} {k}" for k, m in STATUS_MARK.items()) +
            ", 🚫 a step hit an unimplemented route.", ""]
  lines += ["## Matrix", "", "| Scenario | Area | " + " | ".join(subjects) + " |",
            "|---|---|" + "---|" * len(subjects)]
  for scenario, row in report["matrix"].items():
    cells = []
    for s in subjects:
      cell = row["subjects"][s]
      mark = STATUS_MARK.get(cell["status"], "?")
      if cell["not_implemented"]:
        mark += "🚫"
      cells.append(mark)
    lines.append(f"| `{scenario}` | {row['area']} | " + " | ".join(cells) + " |")
  failing = [(sc, s, row["subjects"][s]) for sc, row in report["matrix"].items() for s in subjects
             if row["subjects"][s]["status"] in ("fail", "error", "xpass")]
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
