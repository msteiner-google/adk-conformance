# adk-monitor

Cross-language conformance monitor for the ADK SDKs and the A2A SDKs beneath
them. It runs the same scenarios against every in-scope ADK SDK, compares the
results, and (from M3) reports differences to the upstream repositories.

Design doc: <https://docs.google.com/document/d/17Az5DdRkVcSm5RWNCr43ZdadbuPJA_WrhLkBBOpeklY/edit?resourcekey=0-GlwuOntepJWNPieFxlYgyw>

## Status: M1 (core diffing)

| Subject | SDK | Host |
|---|---|---|
| `python` | google-adk 2.12.0 | `//hosts/python:host` |
| `go` | google.golang.org/adk/v2 v2.6.0 | `//hosts/go:host` |
| `rust` | adk-rust 2.3.0 (community) | `//hosts/rust:host` |

- 50 scenarios (smoke, API, EVT, LLM, TOOL, AGT) × 3 subjects = 150 conformance tests.
- Every host serves the 8 fixtures in [`fixtures/FIXTURES.md`](fixtures/FIXTURES.md).
- 16 confirmed SDK differences are recorded in [`known_divergences.yaml`](known_divergences.yaml)
  (they report XFAIL); the comparison report lists every other divergence.
- Nightly runs on GCP via Cloud Build + Cloud Scheduler, results in Firestore
  (see [Running on GCP](#running-on-gcp)).

Java and TypeScript join in M5, once their ADKs speak A2A v1.

## Quick start

```sh
bazel test //...                                   # all scenarios on all SDKs
bazel test //scenarios/tool:tool_roundtrip_001_go  # one scenario on one SDK
python3 -m harness.compare --in bazel-testlogs --out /tmp/report   # cross-SDK report
CHANNEL=release ci/nightly.sh                      # what the nightly job runs
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
| ERROR | the run could not execute (e.g. the host did not start) | fail |

Expectations check a normalized view (camelCase keys, canonical part shapes),
so wire-format differences do not hide behavior; the comparison report lists
format differences separately.

## Layout

| Path | What it is |
|---|---|
| `scenarios/<area>/*.yaml` | Language-neutral scenarios. `BUILD.bazel` generates one test per subject. |
| `harness/` | Scenario runner: starts the model server and a host, drives the ADK REST API over raw HTTP, checks expectations, writes the transcript. |
| `model_server/` | Scripted Gemini-compatible server. Answers from the scenario's `model_script` and records every LLM request. |
| `fixtures/FIXTURES.md` | Canonical spec of the fixture agents every host serves. |
| `known_divergences.yaml` | Confirmed SDK differences (XFAIL), each with a reason and expiry. |
| `ci/nightly.sh` | One monitoring run: optional SDK bump, all tests, compare, publish. |
| `deploy/` | Runner image, Terraform, and deploy scripts for GCP. |
| `tools/` | `sdk_versions.py`, `bump_sdks.py` (latest channel), `publish_results.py` (GCS + Firestore). |
| `hosts/<lang>/` | One thin host per SDK. Serves the fixture agent through the SDK's own ADK API server. |
| `tools/bazel/` | `conformance_suite()` macro and the subject table (`subjects.bzl`). |
| `third_party/python/` | Python dependency lock (`requirements_lock.txt`). |

Go dependencies live in the root `go.mod`. Rust dependencies live in
`hosts/rust/Cargo.toml` and `Cargo.lock`.

## Host contract

A host accepts `--http-port N` and `--model-base-url URL`, binds the SDK's own
ADK REST API to `127.0.0.1:N`, and serves every fixture in
`fixtures/FIXTURES.md` as a separate app. Every LLM agent uses
`gemini-2.5-flash` with its Gemini client pointed at `--model-base-url`. Hosts
must not call any other network service.

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
`finish_reason`.

### Recording a known divergence

When a test fails, read its transcript. If the SDK really behaves
differently (not a scenario or fixture bug), add an entry to
`known_divergences.yaml` with the exact failure ids the test printed, a
one-line reason, and an expiry date. `//:known_divergences_lint` validates the
file.

## Running on GCP

One-time setup (and re-run after infrastructure changes):

```sh
deploy/deploy.sh <project-id> [region]
```

This applies `deploy/terraform` (APIs, Artifact Registry, three buckets, a
named Firestore database `adk-monitor`, two service accounts, a Cloud
Scheduler job), builds the runner image with Cloud Build, and uploads the
source tarball. From then on Cloud Scheduler starts a Cloud Build run every
night (04:00 UTC by default, channel `latest`), which needs nothing from your
machine:

1. Moves each SDK to its latest release (`tools/bump_sdks.py`).
2. Runs `bazel test //...` (Bazel remote cache in GCS).
3. Builds the comparison report.
4. Publishes to GCS (`gs://<project>-adk-monitor-results/runs/<run_id>/`) and
   Firestore: `runs/{run_id}`, `runs/{run_id}/results/{scenario.subject}`, and
   `findings/{fingerprint}` (first/last seen, seen count).

Start a run immediately: `terraform -chdir=deploy/terraform output -raw run_now`.

The depot repository is not reachable from GCP, so the cloud runs build the
uploaded tarball. After changing scenarios or hosts, run
`deploy/push_source.sh <source-bucket>` (it archives `HEAD`).

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
