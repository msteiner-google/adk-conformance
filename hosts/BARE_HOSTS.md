# Bare A2A hosts (canonical spec)

A bare host runs one **A2A SDK on its own**, with no ADK: the SDK's own
server, task store and request handler, around a small fixed executor. Bare
hosts are the **control group** for attribution (design doc, "Root-cause
attribution"): when an ADK host fails an A2A scenario and the bare host of the
same A2A SDK passes it, the ADK adapter is at fault; when both fail the same
way, the A2A SDK is.

| Subject | Host | SDK | Version source | Role |
|---|---|---|---|---|
| `a2a-python` | `//hosts/a2a-python:host` | a2a-sdk (a2aproject/a2a-python) | `third_party/python/requirements_lock.txt` (the version google-adk resolves) | control for `python` |
| `a2a-go` | `//hosts/a2a-go:host` | a2a-go v2 (a2aproject/a2a-go) | root `go.mod` (the version adk-go resolves) | control for `go` |
| `a2a-rs` (paused) | `//hosts/a2a-rs:host` | a2a-lf (a2aproject/a2a-rs) | `hosts/a2a-rs/Cargo.lock` | **reference** for `rust` (adk-rust implements A2A itself on a2a-protocol-types, so there is no same-SDK control) |

The executor is deliberately trivial, so a failure on a bare host is a
finding about the SDK, never about the executor. Change all three hosts in
the same commit.

## Process contract

```
host --a2a-port N --a2a-app NAME --model-base-url URL [--http-port M (ignored)]
```

- Binds `127.0.0.1:N`. Serves the agent card at `/.well-known/agent-card.json`,
  JSON-RPC at `http://127.0.0.1:N/` and, where the SDK has it, HTTP+JSON at
  `http://127.0.0.1:N/rest`. Nothing else is required.
- Talks to no network service except `--model-base-url`.

## Agent card

| Field | Value |
|---|---|
| `name` | `NAME` |
| `description` | the fixture's description (table below) |
| `version` | `1.0.0` |
| `supportedInterfaces` | JSONRPC `http://127.0.0.1:N/` (and HTTP+JSON `http://127.0.0.1:N/rest` if served), `protocolVersion: "1.0"` |
| `capabilities` | `streaming: true`, `pushNotifications: false` |
| `defaultInputModes` / `defaultOutputModes` | `["text/plain"]` |
| `skills` | one skill: `id` = `name` = `NAME`, the fixture description, `tags: ["adk-monitor"]` |

Fixtures a bare host knows (same text as fixtures/FIXTURES.md):

| NAME | description | instruction (sent as systemInstruction) |
|---|---|---|
| `hello_agent` | `Hello-world fixture agent.` | `You are a friendly assistant. Answer in one short sentence.` |
| `tools_agent` | `Agent with the shared tool library.` | `AGENT=tools_agent. Use the tools to answer.` |

An unknown `NAME` is a startup error.

## Executor

One model call per step through the scripted model server, with the
conversation kept in memory per `contextId`:

1. **New task:** emit the `Task` (state `TASK_STATE_SUBMITTED`, history = the
   user message), then a status update `TASK_STATE_WORKING`.
2. **Model request:** `POST {model-base-url}/v1beta/models/gemini-2.5-flash:generateContent`
   with header `x-goog-api-key: scripted-key` and body
   `{systemInstruction: {parts: [{text: <instruction>}]}, contents: <history>}`.
   History maps A2A parts to Gemini parts: text -> `{text}` (role `user` for
   user messages, `model` for agent replies); a data part with metadata
   `adk_type: function_response` -> `{functionResponse: <data>}` (role `user`);
   the model's own function calls are kept as `{functionCall}` (role `model`).
3. **Reply mapping** (the Gemini response's first candidate):
   - **text:** status update `TASK_STATE_WORKING` with an agent message
     carrying the text, then one artifact update (one text part, `lastChunk:
     true`), then status `TASK_STATE_COMPLETED`.
   - **function call `request_approval`** (long-running): status
     `TASK_STATE_INPUT_REQUIRED` with an agent message whose single part is a
     data part `{id, name, args}` with metadata
     `{adk_type: function_call, adk_is_long_running: true}`. Return. A later
     message with the same `taskId` resumes the task at step 2.
   - **other function calls** (`add`, `get_weather`, `set_state`,
     `fail_always`): run them as in FIXTURES.md, append the responses, and go
     back to step 2 (at most 5 rounds).
   - **HTTP error from the model:** status `TASK_STATE_FAILED` with an agent
     message carrying the error text.
4. **Cancel:** status `TASK_STATE_CANCELED`; an in-flight model call is
   abandoned.
