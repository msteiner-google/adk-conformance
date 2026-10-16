"""Root-cause attribution for A2A findings: ADK SDK vs A2A SDK (design doc).

Every A2A finding is attributed to one layer by combining experiments:

1. **Control group.** The same scenario on the bare A2A host of the subject's
   A2A SDK (hosts/BARE_HOSTS.md), at the version the ADK pins. A bare host
   that fails the same way points at the A2A SDK; a clean one at the ADK
   adapter.
2. **Version probe.** The bare host at the latest A2A SDK release.
3. **Dependency swap.** The ADK host rebuilt against the latest A2A SDK.
   (2 and 3 come from ``attribution/latest.py``; when the pin already is the
   latest release they equal experiment 1 and the ADK result.)
4. **Pair matrix.** For client findings: every client against every server.
   A failing row points at the client, a failing column at the server.
5. **External signal.** a2a-itk's nightly ITK/ACTS results for the A2A SDK
   (``attribution/external.py``), attached as evidence.

Decision table (bare@pinned, bare@latest, ADK@latest):

  fail, fail, fail -> a2a-<lang>      fail, pass, pass -> stale-pin
  pass, pass, fail -> adk-<lang>      pass, pass, pass -> stale-pin
  anything else    -> unknown (human triage)

adk-rust implements A2A itself, so its bare host is a *reference* (the
official a2a-rs), not a control: Rust findings go to adk-rust, with the
reference result as evidence.

  python3 -m attribution.engine --in bazel-testlogs --report out/report/report.json \\
      --out out/attribution [--latest out/latest] [--external out/external.json]
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys
from typing import Any

from attribution import external as external_signal
from harness import compare

A2A_AREAS = ("a2a-srv", "a2a-cli")
CONTROLS = {
    # ADK subject -> (control subject, A2A SDK name, same SDK as the ADK uses?)
    "python": ("a2a-python", "a2a-python", True),
    "go": ("a2a-go", "a2a-go", True),
    "rust": ("a2a-rs", "a2a-rs (reference)", False),
}
ADK_REPOS = {"python": "google/adk-python", "go": "google/adk-go", "rust": "zavora-ai/adk-rust"}
A2A_REPOS = {"python": "a2aproject/a2a-python", "go": "a2aproject/a2a-go",
             "rust": "a2aproject/a2a-rs"}
BAD = ("fail", "xfail", "error")


def _outcome(transcript: dict[str, Any] | None, failures: list[str] | None = None) -> str:
  """pass / fail / n/a for one transcript, optionally compared to failure ids.

  ``fail`` means the run failed on at least one of the given check ids (or at
  all, when none are given); a run that failed only on other checks is
  ``fail-other``.
  """
  if transcript is None:
    return "n/a"
  status = transcript.get("status")
  if status in ("pass", "xpass"):
    return "pass"
  if status == "unsupported":
    return "n/a"
  if status not in BAD:
    return "n/a"
  if failures is None:
    return "fail"
  mine = {f.split(" :: ", 1)[-1] for f in transcript.get("failures", [])}
  wanted = {f.split(" :: ", 1)[-1] for f in failures}
  return "fail" if mine & wanted else "fail-other"


def decide(bare_pinned: str, bare_latest: str, adk_latest: str) -> str:
  """The design doc's decision table; returns an attribution kind."""
  table = {
      ("fail", "fail", "fail"): "a2a",
      ("fail", "pass", "pass"): "stale-pin",
      ("pass", "pass", "fail"): "adk",
      ("pass", "pass", "pass"): "stale-pin",
  }
  return table.get((bare_pinned, bare_latest, adk_latest), "unknown")


def _layer(kind: str, lang: str, same_sdk: bool) -> tuple[str, str]:
  """(attribution label, repository) for a decision on one language."""
  if not same_sdk:
    # adk-rust implements A2A itself: there is no separate A2A SDK to blame.
    return f"adk-{lang}", ADK_REPOS[lang]
  if kind == "a2a":
    return f"a2a-{lang}", A2A_REPOS[lang]
  if kind == "adk":
    return f"adk-{lang}", ADK_REPOS[lang]
  if kind == "stale-pin":
    return "stale-pin", f"{ADK_REPOS[lang]} (upgrade request, linking the A2A fix)"
  return "unknown", "human triage"


class Evidence:
  """Transcripts of the base run and, optionally, of the latest-A2A run."""

  def __init__(self, base: dict[str, dict[str, dict[str, Any]]],
               latest: dict[str, dict[str, dict[str, Any]]] | None = None,
               versions: dict[str, Any] | None = None,
               external: dict[str, Any] | None = None,
               acts: dict[str, Any] | None = None):
    self.base = base
    self.latest = latest or {}
    self.versions = versions or {}  # {lang: {"pinned": v, "latest": v, "swapped": bool}}
    self.external = external or {}
    self.acts = acts or {}  # tools/run_acts.py result, {"sdks": {sdk: {...}}}

  def acts_matches(self, subject: str, behaviors: list[str]) -> list[str]:
    """MUST failures of ACTS at the pinned A2A SDK version that test ``behaviors``."""
    for entry in (self.acts.get("sdks") or {}).values():
      if entry.get("subject") != subject:
        continue
      failing = {i for b in (entry.get("pinned") or {}).get("bindings", {}).values()
                 for i in b.get("must_failures", [])}
      prefixes = [p for b in behaviors for p in external_signal.BEHAVIOR_TO_ACTS.get(b, [])]
      return sorted(i for i in failing if any(i.startswith(p) for p in prefixes))
    return []

  def get(self, scenario: str, subject: str, latest: bool = False) -> dict[str, Any] | None:
    source = self.latest if latest else self.base
    return (source.get(scenario) or {}).get(subject)

  def pin_is_latest(self, lang: str) -> bool:
    info = self.versions.get(lang) or {}
    return bool(info) and info.get("pinned") == info.get("latest")


def _version_experiments(ev: Evidence, scenario: str, subject: str, control: str,
                         failures: list[str] | None, bare_pinned: str,
                         adk_pinned: str) -> tuple[str, str, str]:
  """(bare@latest, ADK@latest, note) for experiments 2 and 3."""
  if ev.pin_is_latest(subject):
    return bare_pinned, adk_pinned, f"pinned {ev.versions[subject]['pinned']} is the latest release"
  bare_latest = _outcome(ev.get(scenario, control, latest=True), failures)
  adk_latest = _outcome(ev.get(scenario, subject, latest=True), failures)
  info = ev.versions.get(subject) or {}
  if bare_latest == "n/a" and adk_latest == "n/a":
    reason = info.get("reason") or "latest-A2A run not available (attribution/latest.py)"
    return "n/a", "n/a", reason
  return bare_latest, adk_latest, f"latest {info.get('latest', '?')} vs pinned {info.get('pinned', '?')}"


def attribute_failure(ev: Evidence, scenario: str, subject: str,
                      transcript: dict[str, Any]) -> dict[str, Any]:
  """Attribution record for an ADK subject failing (or XFAILing) an A2A server scenario."""
  control, sdk_name, same_sdk = CONTROLS[subject]
  failures = [f for f in transcript.get("failures", []) if not f.startswith("run :: ")] or None
  bare = _outcome(ev.get(scenario, control), failures)
  adk = _outcome(transcript, failures)
  bare_latest, adk_latest, note = _version_experiments(ev, scenario, subject, control, failures,
                                                       bare, adk)
  others = {s: _outcome(ev.get(scenario, s), failures) for s in CONTROLS if s != subject}
  assumed = bare_latest == "n/a" and adk_latest == "n/a"
  if assumed:
    # No latest-release evidence: decide on the pinned results alone, as if
    # the pin were the latest release, and say so.
    kind = decide(bare, bare, adk) if same_sdk else ("adk" if bare == "pass" else "unknown")
    note += "; decided on pinned results only"
  else:
    kind = decide(bare, bare_latest, adk_latest) if same_sdk else ("adk" if bare == "pass" else "unknown")
  everyone_fails = all(o == "fail" for o in others.values()) and bare == "fail"
  if everyone_fails:
    kind, label, repo = "spec", "spec", "a2aproject/A2A (discussion) or this repo (scenario bug)"
  else:
    label, repo = _layer(kind, subject, same_sdk)
    if not same_sdk and bare != "pass":
      label, repo = f"adk-{subject}", ADK_REPOS[subject]
  agreeing = [bare != "n/a"]
  if not ev.pin_is_latest(subject) and bare_latest != "n/a":
    agreeing.append(True)
  if any(o == "pass" for o in others.values()) and kind in ("adk", "a2a"):
    agreeing.append(True)  # another SDK passes: the scenario itself is satisfiable
  nightly = external_signal.matches(ev.external.get(subject), transcript.get("behaviors", []))
  if nightly and kind == "a2a":
    agreeing.append(True)  # a2a-itk's nightly ACTS fails the same behavior
  acts = ev.acts_matches(subject, transcript.get("behaviors", [])) if same_sdk else []
  if acts and kind == "a2a":
    agreeing.append(True)  # ACTS at the pinned version fails the same behavior
  confidence = "high" if sum(agreeing) >= 2 and kind not in ("unknown", "spec") else (
      "medium" if kind not in ("unknown",) and agreeing[0] else "low")
  if assumed and confidence == "high" and not ((nightly or acts) and kind == "a2a"):
    confidence = "medium"
  return {
      "kind": "failure",
      "scenario": scenario,
      "subject": subject,
      "behaviors": transcript.get("behaviors", []),
      "checks": failures or transcript.get("failures", []),
      "status": transcript.get("status"),
      "known_divergence": bool(transcript.get("known_divergence")),
      "experiments": {
          "control": {"subject": control, "sdk": sdk_name, "same_sdk": same_sdk, "result": bare},
          "version_probe": {"result": bare_latest, "note": note},
          "dependency_swap": {"result": adk_latest, "note": note},
          "other_adk": others,
          "external": {"sdk": (ev.external.get(subject) or {}).get("sdk"),
                       "matching_acts_failures": nightly},
          "acts_pinned": acts,
      },
      "attribution": label,
      "decision": kind,
      "confidence": confidence,
      "report_to": repo,
  }


def attribute_divergence(ev: Evidence, finding: dict[str, Any]) -> list[dict[str, Any]]:
  """Attribution for an A2A server divergence: is the outlier's A2A SDK the cause?

  The control's signature for the same step is compared with the outlier's
  and with the majority's: a control that behaves like the outlier points at
  the A2A SDK, one that behaves like the majority at the ADK adapter.
  """
  aspect = finding["aspect"]
  if not aspect.startswith("step:") or not finding.get("outliers"):
    return []
  step_name = aspect.split(":", 1)[1]
  scenario = finding["scenario"]
  majority = finding.get("majority") or []

  def signature(subject: str) -> str | None:
    t = ev.get(scenario, subject)
    step = next((s for s in (t or {}).get("steps", []) if s["name"] == step_name), None)
    return compare.canonical(compare.step_signature(step, compare.IdMap())) if step else None

  majority_sig = signature(majority[0]) if majority else None
  if majority_sig and '"unsupported:' in majority_sig:
    return []  # the majority lacks the feature: a capability difference, not a bug
  out = []
  for subject in finding["outliers"]:
    if subject not in CONTROLS:
      continue
    control, sdk_name, same_sdk = CONTROLS[subject]
    control_sig = signature(control)
    if control_sig is None:
      result = "n/a"
    elif control_sig == signature(subject):
      result = "fail"  # the bare A2A SDK behaves like the outlier
    elif control_sig == majority_sig:
      result = "pass"  # the bare A2A SDK behaves like the majority
    else:
      result = "other"
    if not same_sdk:
      label, repo, kind = f"adk-{subject}", ADK_REPOS[subject], "adk"
    elif result == "fail":
      label, repo, kind = f"a2a-{subject}", A2A_REPOS[subject], "a2a"
    elif result == "pass":
      label, repo, kind = f"adk-{subject}", ADK_REPOS[subject], "adk"
    else:
      label, repo, kind = "unknown", "human triage", "unknown"
    out.append({
        "kind": "divergence",
        "scenario": scenario,
        "subject": subject,
        "aspect": aspect,
        "fingerprint": finding["fingerprint"],
        "paths": [d["path"] for d in finding.get("details", [])][:6],
        "experiments": {
            "control": {"subject": control, "sdk": sdk_name, "same_sdk": same_sdk, "result": result,
                        "compared_with": "outlier vs majority signature"},
        },
        "attribution": label,
        "decision": kind,
        "confidence": "medium" if kind != "unknown" else "low",
        "report_to": repo,
    })
  return out


def pair_matrix(grid: dict[str, Any]) -> list[dict[str, Any]]:
  """Experiment 4 on one client x server grid: failing rows, columns, cells."""
  clients, servers, cells = grid["clients"], grid["servers"], grid["cells"]

  def bad(client: str, server: str) -> bool | None:
    cell = cells.get(client, {}).get(server)
    if not cell or cell["status"] in ("missing", "unsupported"):
      return None
    return cell["status"] in BAD

  out = []
  failing_rows = {c for c in clients
                  if all(bad(c, s) is not False for s in servers) and any(bad(c, s) for s in servers)}
  failing_cols = {s for s in servers
                  if all(bad(c, s) is not False for c in clients) and any(bad(c, s) for c in clients)}
  for client in sorted(failing_rows):
    out.append({"pattern": "row", "client": client, "attribution": f"adk-{client}",
                "report_to": ADK_REPOS.get(client, "human triage"),
                "note": f"the {client} client fails against every server"})
  for server in sorted(failing_cols - failing_rows):
    lang = next((adk for adk, (ctl, _, _) in CONTROLS.items() if ctl == server), None)
    if lang:  # a bare server: its A2A SDK
      label, repo = f"a2a-{lang}" if CONTROLS[lang][2] else server, A2A_REPOS[lang]
    else:
      label, repo = f"adk-{server}", ADK_REPOS.get(server, "human triage")
    out.append({"pattern": "column", "server": server, "attribution": label, "report_to": repo,
                "note": f"every client fails against the {server} server"})
  for client in clients:
    for server in servers:
      if bad(client, server) and client not in failing_rows and server not in failing_cols:
        note = (f"only {client} -> {server} fails: an interop difference between the {client} "
                f"client and the {server} server")
        control = CONTROLS.get(server, (None,))[0]
        if control in servers and bad(client, control) is False:
          note += (f"; {client} passes against the bare {control} server, so the {server} ADK "
                   f"adapter's output (not its A2A SDK) is what the {client} client trips on")
        elif control in servers and bad(client, control):
          note += (f"; {client} also fails against the bare {control} server, so it trips on "
                   f"what the {control} SDK puts on the wire, not on the {server} ADK adapter")
        out.append({"pattern": "cell", "client": client, "server": server,
                    "attribution": "pair", "report_to": "human triage", "note": note,
                    "checks": cells[client][server].get("failures", [])})
  return out


def run(report: dict[str, Any], ev: Evidence) -> dict[str, Any]:
  records: list[dict[str, Any]] = []
  for scenario, row in report.get("matrix", {}).items():
    if row.get("area") not in A2A_AREAS:
      continue
    for subject in CONTROLS:
      cell = row["subjects"].get(subject)
      if cell and cell["status"] in BAD:
        transcript = ev.get(scenario, subject)
        if transcript:
          records.append(attribute_failure(ev, scenario, subject, transcript))
  failed = {(r["scenario"], r["subject"]) for r in records}
  for finding in report.get("findings", []):
    row = report.get("matrix", {}).get(finding["scenario"])
    if row and row.get("area") == "a2a-srv" and finding["kind"] == "divergence":
      # A subject that already failed the scenario is attributed above.
      records.extend(r for r in attribute_divergence(ev, finding)
                     if (r["scenario"], r["subject"]) not in failed)
  interop = {}
  for scenario, grid in report.get("interop", {}).items():
    patterns = pair_matrix(grid)
    if patterns:
      interop[scenario] = patterns
  by_label = collections.Counter(r["attribution"] for r in records)
  for patterns in interop.values():
    by_label.update(p["attribution"] for p in patterns)
  return {
      "versions": ev.versions,
      "records": records,
      "interop": interop,
      "summary": dict(sorted(by_label.items())),
      "external": ev.external,
      "acts": ev.acts,
  }


def render_markdown(result: dict[str, Any]) -> str:
  lines = ["## Attribution", "",
           "Which layer each A2A finding comes from (design doc, root-cause attribution). "
           "Experiments: control = the bare A2A host of the same SDK at the pinned version; "
           "probe = that bare host at the latest A2A release; swap = the ADK host rebuilt "
           "against the latest A2A release.", ""]
  if result.get("versions"):
    lines += ["| A2A SDK | pinned | latest | latest run |", "|---|---|---|---|"]
    for lang, info in sorted(result["versions"].items()):
      lines.append(f"| {info.get('sdk', lang)} | {info.get('pinned', '?')} | {info.get('latest', '?')} | "
                   f"{info.get('reason') or ('swapped' if info.get('swapped') else 'same version')} |")
    lines.append("")
  if result.get("summary"):
    lines += ["Totals: " + ", ".join(f"**{k}** {v}" for k, v in result["summary"].items()), ""]
  failures = [r for r in result["records"] if r["kind"] == "failure"]
  if failures:
    lines += ["### Failures", "",
              "| Scenario | Subject | Control | Probe | Swap | ACTS @pinned | a2a-itk nightly | Attribution | Confidence | Report to |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in failures:
      e = r["experiments"]
      known = " (known)" if r.get("known_divergence") else ""
      nightly = ", ".join(e["external"]["matching_acts_failures"]) or "—"
      acts = ", ".join(e.get("acts_pinned") or []) or "—"
      lines.append(
          f"| `{r['scenario']}` | {r['subject']}{known} | {e['control']['subject']}: "
          f"{e['control']['result']} | {e['version_probe']['result']} | "
          f"{e['dependency_swap']['result']} | {acts} | {nightly} | **{r['attribution']}** | "
          f"{r['confidence']} | {r['report_to']} |")
    lines.append("")
  divergences = [r for r in result["records"] if r["kind"] == "divergence"]
  if divergences:
    lines += ["### Divergences (outlier vs control)", "",
              "| Scenario | Aspect | Outlier | Control behaves like | Attribution | Report to |",
              "|---|---|---|---|---|---|"]
    like = {"fail": "the outlier", "pass": "the majority", "other": "neither", "n/a": "n/a"}
    for r in divergences:
      c = r["experiments"]["control"]
      lines.append(f"| `{r['scenario']}` | {r['aspect']} | {r['subject']} | {c['subject']}: "
                   f"{like.get(c['result'], c['result'])} | **{r['attribution']}** | {r['report_to']} |")
    lines.append("")
  if result.get("interop"):
    lines += ["### Interop matrix patterns", "", "| Scenario | Pattern | Who | Attribution | Note |",
              "|---|---|---|---|---|"]
    for scenario, patterns in result["interop"].items():
      for p in patterns:
        who = p.get("client") or p.get("server")
        if p["pattern"] == "cell":
          who = f"{p['client']} -> {p['server']}"
        lines.append(f"| `{scenario}` | {p['pattern']} | {who} | **{p['attribution']}** | {p['note']} |")
    lines.append("")
  acts_sdks = (result.get("acts") or {}).get("sdks") or {}
  if acts_sdks:
    itk = (result["acts"].get("a2a_itk") or {}).get("ref", "?")[:12]
    lines += ["### A2A SDK conformance (ACTS through a2a-itk)", "",
              f"ACTS MUST results per binding at the pinned A2A SDK version (A2A-SDK-001) and what "
              f"an upgrade would change (A2A-SDK-004). a2a-itk @ `{itk}`.", "",
              "| A2A SDK | ref | binding | MUST | MUST failures | fixed by upgrade | new in latest |",
              "|---|---|---|---|---|---|---|"]
    for sdk, entry in sorted(acts_sdks.items()):
      pin = entry.get("pinned") or {}
      if pin.get("error"):
        lines.append(f"| {sdk} | {pin.get('ref', '?')} | — | — | error: {pin['error']} | | |")
        continue
      for binding, b in sorted((pin.get("bindings") or {}).items()):
        must = (b.get("summary") or {}).get("by_level", {}).get("must", {})
        d = (entry.get("delta") or {}).get(binding, {})
        lines.append(f"| {sdk} | {pin.get('ref')} | {binding} | {must.get('passed', '?')}/"
                     f"{must.get('total', '?')} | {', '.join(b['must_failures']) or '—'} | "
                     f"{', '.join(d.get('fixed_by_upgrade', [])) or '—'} | "
                     f"{', '.join(d.get('new_in_latest', [])) or '—'} |")
    lines.append("")
  external = {k: v for k, v in (result.get("external") or {}).items() if v}
  if external:
    lines += ["### External signal (a2a-itk nightly)", ""]
    for lang, info in sorted(external.items()):
      lines.append(f"- **{info.get('sdk', lang)}**: {info.get('summary', '')}")
    lines.append("")
  return "\n".join(lines) + "\n"


def _load_json(path: str | None) -> Any:
  if not path or not os.path.exists(path):
    return None
  with open(path, encoding="utf-8") as f:
    return json.load(f)


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  parser.add_argument("--in", dest="inp", required=True, help="transcripts (bazel-testlogs)")
  parser.add_argument("--report", required=True, help="report.json from harness.compare")
  parser.add_argument("--out", required=True, help="output directory")
  parser.add_argument("--latest", default=None,
                      help="directory from attribution/latest.py (latest.json + transcripts)")
  parser.add_argument("--external", default=None, help="JSON from attribution/external.py")
  parser.add_argument("--acts", default=None, help="acts.json from tools/run_acts.py")
  parser.add_argument("--append", action="store_true",
                      help="also append the section to report.md and embed it in report.json")
  args = parser.parse_args(argv)
  report = _load_json(args.report)
  if report is None:
    print(f"cannot read {args.report}", file=sys.stderr)
    return 1
  base = compare.load_transcripts(args.inp)
  latest_transcripts, versions = {}, {}
  if args.latest:
    versions = (_load_json(os.path.join(args.latest, "latest.json")) or {}).get("sdks", {})
    transcripts_dir = os.path.join(args.latest, "transcripts")
    if os.path.isdir(transcripts_dir):
      latest_transcripts = compare.load_transcripts(transcripts_dir)
  external = _load_json(args.external) or {}
  acts = _load_json(args.acts) or {}
  result = run(report, Evidence(base, latest_transcripts, versions, external.get("sdks", {}), acts))
  os.makedirs(args.out, exist_ok=True)
  with open(os.path.join(args.out, "attribution.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, sort_keys=True)
  markdown = render_markdown(result)
  with open(os.path.join(args.out, "attribution.md"), "w", encoding="utf-8") as f:
    f.write(markdown)
  if args.append:
    report["attribution"] = result
    with open(args.report, "w", encoding="utf-8") as f:
      json.dump(report, f, indent=2, sort_keys=True, ensure_ascii=False)
    report_md = os.path.join(os.path.dirname(args.report), "report.md")
    with open(report_md, encoding="utf-8") as f:
      text = f.read()
    # Before the long divergence lists, so readers meet it right after the failures.
    anchor = next((a for a in ("\n## Recurring differences", "\n## Divergences") if a in text), None)
    text = text.replace(anchor, "\n" + markdown + anchor, 1) if anchor else text + "\n" + markdown
    with open(report_md, "w", encoding="utf-8") as f:
      f.write(text)
  print(f"{len(result['records'])} attributed finding(s), {sum(map(len, result['interop'].values()))} "
        f"interop pattern(s): {result['summary']}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
