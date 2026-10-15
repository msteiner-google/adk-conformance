# adk-monitor

Cross-language conformance monitor for the ADK SDKs and the A2A SDKs beneath
them. It runs the same scenarios against every in-scope ADK SDK, compares the
results, and (from M3) reports differences to the upstream repositories.

Design doc: <https://docs.google.com/document/d/17Az5DdRkVcSm5RWNCr43ZdadbuPJA_WrhLkBBOpeklY/edit?resourcekey=0-GlwuOntepJWNPieFxlYgyw>

## Status: M0 (skeleton)

| Subject | SDK | Host |
|---|---|---|
| `python` | google-adk 2.12.0 | `//hosts/python:host` |
| `go` | google.golang.org/adk/v2 v2.6.0 | `//hosts/go:host` |
| `rust` | adk-rust 2.3.0 (community) | `//hosts/rust:host` |

Java and TypeScript join in M5, once their ADKs speak A2A v1.

## Quick start

```sh
bazel test //...                                   # everything
bazel test //scenarios/core:hello_world_001_go     # one scenario on one SDK
```

Each conformance test writes a transcript (HTTP exchanges, SSE events, and the
LLM requests the SDK sent) to
`bazel-testlogs/scenarios/core/<test>/test.outputs/` (plus the host log).

## Layout

| Path | What it is |
|---|---|
| `scenarios/<area>/*.yaml` | Language-neutral scenarios. `BUILD.bazel` generates one test per subject. |
| `harness/` | Scenario runner: starts the model server and a host, drives the ADK REST API over raw HTTP, checks expectations, writes the transcript. |
| `model_server/` | Scripted Gemini-compatible server. Answers from the scenario's `model_script` and records every LLM request. |
| `hosts/<lang>/` | One thin host per SDK. Serves the fixture agent through the SDK's own ADK API server. |
| `tools/bazel/` | `conformance_suite()` macro and the subject table (`subjects.bzl`). |
| `third_party/python/` | Python dependency lock (`requirements_lock.txt`). |

Go dependencies live in the root `go.mod`. Rust dependencies live in
`hosts/rust/Cargo.toml` and `Cargo.lock`.

## Host contract (M0)

A host accepts `--http-port N` and `--model-base-url URL`, binds the SDK's own
ADK REST API to `127.0.0.1:N`, and serves one app, `hello_agent`: an LLM agent
on `gemini-2.5-flash` whose Gemini client points at `--model-base-url`. It must
not call any other network service.

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

Expectation values can be literals or one of `{contains: ...}`,
`{regex: ...}`, `{len: N}`, `{exists: true|false}`.

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
