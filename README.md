# adk-monitor

Cross-language conformance monitor for the ADK SDKs and the A2A SDKs beneath
them. It runs the same scenarios against every in-scope ADK SDK, compares the
results, attributes A2A differences to the layer that causes them, and (from
M3) reports them to the upstream repositories.

Design doc: <https://docs.google.com/document/d/17Az5DdRkVcSm5RWNCr43ZdadbuPJA_WrhLkBBOpeklY/edit?resourcekey=0-GlwuOntepJWNPieFxlYgyw>

## Status: M2 (A2A and attribution)

| Subject | SDK | Host | Role |
|---|---|---|---|
| `python` | google-adk 2.13.0 (a2a-sdk 1.2.2) | `//hosts/python:host` | ADK |
| `go` | google.golang.org/adk/v2 v2.7.1 (a2a-go v2.6.0) | `//hosts/go:host` | ADK |
| `rust` | adk-rust 2.3.0 (community; own A2A on a2a-protocol-types 0.12.1) | `//hosts/rust:host` | ADK |
| `a2a-python` | a2a-sdk 1.2.2 | `//hosts/a2a-python:host` | control for `python` |
| `a2a-go` | a2a-go v2.6.0 | `//hosts/a2a-go:host` | control for `go` |
| `a2a-rs` | official a2aproject/a2a-rs | `//hosts/a2a-rs:host` | reference for `rust` |

- 71 scenarios, 300 conformance tests:
  - 50 ADK scenarios (smoke, API, EVT, LLM, TOOL, AGT) × 3 ADK subjects = 150;
  - 16 A2A server scenarios × 6 subjects (3 ADK + 3 bare) = 96;
  - 4 A2A client scenarios × 3 clients × 3 ADK servers = 36;
  - 1 interop scenario × 3 clients × 6 servers = 18.
- Every ADK host serves the 10 fixtures in [`fixtures/FIXTURES.md`](fixtures/FIXTURES.md),
  and one of them over A2A v1 on request. Bare A2A hosts (an A2A SDK with a
  fixed executor, no ADK) follow [`hosts/BARE_HOSTS.md`](hosts/BARE_HOSTS.md).
- 41 confirmed SDK differences (16 ADK, 25 A2A) are recorded in
  [`known_divergences.yaml`](known_divergences.yaml) (they report XFAIL); the
  comparison report lists every other divergence.
- Every A2A finding is attributed to the ADK SDK, the A2A SDK, a stale pin or
  the spec ([Attribution](#attribution)); ACTS runs through a2a-itk on request.
- Run everything locally with `tools/run_pipeline.sh` and read
  `out/report/report.md` (see [Running the pipeline](#running-the-pipeline)).

Java and TypeScript join in M5, once their ADKs speak A2A v1.

## Quick start

```sh
bazel test //...                                   # all scenarios on all SDKs
bazel test //scenarios/tool:tool_roundtrip_001_go  # one scenario on one SDK
bazel test //scenarios/a2a_cli:a2a_cli_basic_001_python_to_go   # one client/server pair
python3 -m harness.compare --in bazel-testlogs --out /tmp/report   # cross-SDK report
tools/run_pipeline.sh                              # all tests + report in out/report/
```

Each conformance test writes a transcript (HTTP exchanges, SSE events, and the
LLM requests the SDK sent) to `bazel-testlogs/scenarios/<area>/<test>/test.outputs/`
(plus the host log). `harness.compare` reads them and writes `report.md` and
`report.json`: a status matrix, recurring differences (root causes first), and
every divergence with the subjects grouped by identical behavior.

### Test statuses

| Status | Meaning | Bazel |
|---|---|---|
| PASS | every expectation held | pass |
| FAIL | an expectation failed and no known divergence covers it | fail |
| XFAIL | failed exactly as a `known_divergences.yaml` entry says | pass |
| XPASS | passed although an entry exists: remove the entry | pass |
| UNSUPPORTED | the subject lacks a feature the scenario needs (e.g. its card declares no HTTP+JSON binding); capability matrix, not a bug | pass |
| ERROR | the run could not execute (e.g. the host did not start) | fail |

Expectations check a normalized view (camelCase keys, canonical part shapes),
so wire-format differences do not hide behavior; the comparison report lists
format differences separately.

## Layout

| Path | What it is |
|---|---|
| `scenarios/<area>/*.yaml` | Language-neutral scenarios. `BUILD.bazel` generates one test per subject (A2A server scenarios also on the bare hosts) or per client × server pair (A2A client scenarios). |
| `harness/` | Scenario runner: starts the model server and the host(s), drives the ADK REST API and A2A (JSON-RPC, HTTP+JSON) over raw HTTP, records client-to-server A2A traffic through a proxy, checks expectations, writes the transcript. |
| `model_server/` | Scripted Gemini-compatible server. Answers from the scenario's `model_script` and records every LLM request. |
| `fixtures/FIXTURES.md` | Canonical spec of the fixture agents every host serves. |
| `known_divergences.yaml` | Confirmed SDK differences (XFAIL), each with a reason and expiry. |
| `tools/run_pipeline.sh` | One monitoring run: optional SDK bump, all tests, cross-SDK report. |
| `tools/` | Also `sdk_versions.py` (versions from lock files), `bump_sdks.py` (move SDKs to their latest releases) and `run_acts.py` (ACTS through a2a-itk). |
| `hosts/<lang>/` | One thin host per ADK SDK. Serves the fixtures through the SDK's own ADK API server and A2A exposure. |
| `hosts/a2a-<lang>/` | Bare A2A hosts: one A2A SDK with a fixed executor, no ADK ([spec](hosts/BARE_HOSTS.md)). The control group for attribution. |
| `attribution/` | Root-cause attribution of A2A findings: control group, latest-release probe and swap (`latest.py`), interop matrix, a2a-itk nightly results (`external.py`). |
| `tools/bazel/` | `conformance_suite()` macro and the subject table (`subjects.bzl`). |
| `third_party/python/` | Python dependency lock (`requirements_lock.txt`). |

Go dependencies live in the root `go.mod`. Rust dependencies live in
`hosts/rust/Cargo.toml` and `Cargo.lock` (and `hosts/a2a-rs/` for the
reference host).

## Host contract

A host accepts `--http-port N` and `--model-base-url URL`, binds the SDK's own
ADK REST API to `127.0.0.1:N`, and serves every fixture in
`fixtures/FIXTURES.md` as a separate app. Every LLM agent uses
`gemini-2.5-flash` with its Gemini client pointed at `--model-base-url`. Hosts
must not call any other network service.

A2A (M2):

- `--a2a-port N --a2a-app NAME`: also serve fixture `NAME` over A2A v1 on
  `127.0.0.1:N` through the SDK's own A2A exposure, card at
  `/.well-known/agent-card.json`. The harness reads the binding URLs from the
  card.
- `--remote-a2a-url URL`: the base URL of a remote A2A agent; the
  `remote_agent` and `remote_transfer_agent` fixtures call it with the SDK's
  RemoteA2aAgent. In client scenarios this is the harness's recording proxy in
  front of the server host.

Bare A2A hosts accept `--a2a-port`, `--a2a-app` and `--model-base-url` only.

## Writing a scenario

```yaml
id: hello_world_001
behaviors: [AGT-001]            # IDs from the behavior catalog
model_script:                   # scripted Gemini replies, first match wins
  - when: {turn: 1}
    reply: {text: "Hello from the scripted model!"}
steps:
  - adk.create_session: {app: hello_agent, user: u1}
    save_as: session
  - adk.run_sse: {app: hello_agent, user: u1, session: "{{session.body.id}}", text: "Say hello"}
    expect:
      final.content.parts[0].text: "Hello from the scripted model!"
expect_llm:                     # checks on the LLM requests the SDK sent
  request_count: 1
```

Step drivers: `adk.list_apps`, `adk.create_session`, `adk.get_session`,
`adk.list_sessions`, `adk.delete_session`, `adk.run`, `adk.run_sse` (with
`streaming`, `state_delta`, or raw `parts`), and `http` for any route.

Event-producing steps expose `events`, `final`, `event_count`, and derived
`texts`, `authors`, `function_calls`, `function_responses`.

Paths support `[N]`, `[-1]` and `[*]` (maps over a list). Expectation values
are literals or one of `contains`, `not_contains`, `regex`, `len`, `eq`, `ne`,
`in`, `all_equal`, `same_as` (another path in the same step), `type`,
`exists`. Values can reference earlier steps with `{{save_as_name.path}}`.

Model-script `when` keys: `turn`, `agent` (from the `AGENT=<name>.` marker in
fixture instructions), `call` (Nth call of that agent), `has_function_response`,
`last_user_text_contains`, `newest_content_contains`. Replies: `text` (+
`chunks`), `function_calls` (with optional `id`), `empty`, `error`,
`finish_reason`, `delay_ms`.

### A2A server scenarios

`a2a: {app: <fixture>}` serves that fixture over A2A; `a2a.*` steps talk to it
with `A2A-Version: 1.0` (override with `version:`, or `version: null` to omit
it) on the JSON-RPC binding (or `binding: HTTP+JSON`). They run on the ADK
hosts and the bare hosts (`scenarios/a2a_srv`).

```yaml
a2a: {app: tools_agent}
steps:
  - a2a.send_message: {text: "Reimburse 500 EUR"}
    save_as: start
    expect:
      state: TASK_STATE_INPUT_REQUIRED
  - a2a.send_message:
      task_id: "{{start.task_id}}"
      parts: [{data: {id: call-approve, name: request_approval, response: {status: approved}},
               metadata: {adk_type: function_response}}]
    expect:
      state: TASK_STATE_COMPLETED
```

Drivers: `a2a.get_card`, `a2a.send_message`, `a2a.stream`, `a2a.get_task`,
`a2a.cancel_task`, `a2a.list_tasks`, `a2a.subscribe`, `a2a.raw` (a request as
written, for envelope errors) and `a2a.check_interfaces` (probes every binding
the card declares). Results are a normalized v1 view (0.3 enums and `kind`
rewritten): `task`, `message`, `state`, `error`, `error_code`, `task_id`,
`context_id`, `artifact_texts`, `status_texts`, `history_texts`,
`text_locations`, `data_parts`; streams add `kinds`, `states`, `final_state`,
`closed`, `artifact_updates`, `artifact_text`. `format` holds what was really
on the wire (`enum_style`, `kind_field`, `key_style`).

### A2A client scenarios

`remote: {app: <fixture>}` starts a second host (the server) serving that
fixture over A2A behind a recording proxy and points the subject's
`remote_*` fixtures at it. Steps drive the client over its ADK REST API; each
step's result gains `a2a` with what the client sent: `methods`, `versions`
(the `A2A-Version` headers), `messages`, `message_texts`, `context_ids`,
`task_ids`, `response_context_ids`, `card_fetches`. `scenarios/a2a_cli` runs
every ADK client against every ADK server; `scenarios/a2a_interop` adds the
bare servers (the interop matrix).

### Recording a known divergence

When a test fails, read its transcript. If the SDK really behaves
differently (not a scenario or fixture bug), add an entry to
`known_divergences.yaml` with the exact failure ids the test printed, a
one-line reason, and an expiry date. `//:known_divergences_lint` validates the
file.

## Running the pipeline

```sh
tools/run_pipeline.sh            # pinned SDK versions (the lock files)
tools/run_pipeline.sh --latest   # first move every SDK to its newest release
tools/run_pipeline.sh --fresh    # ignore Bazel's cached test results
tools/run_pipeline.sh --acts     # also run ACTS through a2a-itk (slow)
tools/run_pipeline.sh --offline  # no network after the tests
```

A cold run takes about 15 minutes (mostly compiling the Rust host); later
runs only rebuild what changed. Output, in `out/` (or `--out DIR`):

| File | Contents |
|---|---|
| `report/report.md` | The report to read: run metadata and SDK versions, a status summary per subject, the scenario × subject matrix, failures, recurring differences (root causes first), and every divergence. |
| `report/report.json` | The same data, machine-readable: `meta`, `counts`, `matrix`, `recurring`, `findings` (each with a stable `fingerprint`). Easier to parse than the markdown. |
| `meta.json`, `versions.json`, `bump.json` | Run metadata, resolved SDK versions, and what `--latest` changed. |
| `attribution/attribution.{md,json}` | The attribution of every A2A finding (also embedded in the report, before the divergence lists). |
| `latest/latest.json` | Pinned vs latest A2A SDK per language, and whether the latest-release run (experiments 2 and 3) happened. Its transcripts are in `latest/transcripts/`. |
| `external.json` | a2a-itk's newest nightly ITK/ACTS results per A2A SDK (experiment 5). |
| `acts/acts.json` | With `--acts`: ACTS MUST results per binding at the pinned and the latest A2A SDK release, and the delta. Raw reports next to it. |

Per-test transcripts stay under `bazel-testlogs/scenarios/` until the next run.

`--latest` rewrites the lock files in place: review with `git diff`, commit
them to adopt the new versions, or `git restore` them. Major-version jumps
(for example adk-go v3) are never taken automatically.

### Acting on the report

- **FAIL** in the matrix: a new difference or a regression. Read the
  transcript, then fix the scenario/fixture, or record the SDK difference in
  `known_divergences.yaml`.
- **XPASS**: an SDK fixed a known divergence; delete its entry.
- **UNSUPPORTED** (🚫): the SDK lacks the feature; it belongs in the
  capability matrix, not in a bug report.
- **Attribution**: for each A2A failure, which layer to report to and how
  sure the engine is (below).
- **Recurring differences / Divergences**: candidates for upstream issues,
  grouped by root cause. The odd one out is usually the SDK to report to.

## Attribution

`attribution/engine.py` decides, for every A2A finding, whether the ADK SDK,
its A2A SDK, a stale dependency pin or the spec is responsible, following the
design doc:

| Experiment | How | Source |
|---|---|---|
| 1. Control group | the same scenario on the bare host of the subject's A2A SDK (`a2a-python`, `a2a-go`) at the pinned version | the regular test run |
| 2. Version probe | the bare host built against the latest A2A SDK release | `attribution/latest.py` |
| 3. Dependency swap | the ADK host rebuilt against the latest A2A SDK release | `attribution/latest.py` |
| 4. Pair matrix | every client against every server: a failing row means the client, a failing column the server, one cell the pair | `scenarios/a2a_cli`, `scenarios/a2a_interop` |
| 5. External signal | a2a-itk's nightly ACTS/ITK results for the same SDK, matched by behavior | `attribution/external.py` |

Decision (bare@pinned, bare@latest, ADK@latest): fail/fail/fail → the A2A
SDK; pass/pass/fail → the ADK SDK; fail/pass/pass or pass/pass/pass → a stale
pin; anything else → human triage. When the pin already is the latest
release, experiments 2 and 3 equal experiment 1 and the ADK result, and the
report says so. A failure every SDK and every bare host shares is labelled
`spec` (or a scenario bug). Confidence is high when two independent signals
agree (for example the control group and ACTS at the pinned version).

adk-rust implements A2A itself, so `a2a-rs` (the official Rust SDK) is a
*reference*, not a control: Rust findings go to adk-rust, with the reference
result as evidence. A divergence (no expectation failed, but the SDKs differ)
is attributed by comparing the control's behavior with the outlier's and the
majority's.

`attribution/latest.py` never touches the checkout: it copies the workspace to
`~/.cache/adk-monitor/latest-a2a/ws`, moves only the A2A SDK there, and runs
the A2A server scenarios. `--latest python=1.2.1` overrides a version, which
is how the swap path is tested while the pins are current.

### ACTS through a2a-itk

`tools/run_acts.py` checks out a2a-itk (pinned commit) and each A2A SDK at the
pinned and the latest release, and runs a2a-itk's `run_acts.py` against the
SDK's own `itk/` agent on every binding (A2A-SDK-001, A2A-SDK-004). It needs
uv and the SDK's toolchain. ACTS MUST failures at the pinned version that test
the same behavior as a finding raise the attribution's confidence.

## Updating dependencies

```sh
# Python
uv pip compile third_party/python/requirements.in --generate-hashes \
  --python-version 3.13 --python-platform x86_64-unknown-linux-gnu \
  -o third_party/python/requirements_lock.txt
# Go
go mod tidy && bazel mod tidy && bazel run //:gazelle
# Rust
cargo update --manifest-path hosts/rust/Cargo.toml   # then rebuild
```
