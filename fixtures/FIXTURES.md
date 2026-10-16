# Fixture agents (canonical spec)

Every host serves **all** fixtures below as separate apps, from one process.
Fixtures must be identical across languages; if they differ, findings are test
bugs. A fixture change must update every in-scope host in the same commit.

## Conventions

- **App name == root agent name.** The ADK REST API addresses apps by name.
- **Model:** every LLM agent uses `gemini-2.5-flash` through the SDK's Gemini
  integration, pointed at `--model-base-url`, API key `scripted-key`.
- **Agent marker:** every instruction except `hello_agent`'s starts with
  `AGENT=<agent name>.` The scripted model server reads this marker to know
  which agent is calling (`when: {agent: ...}` in a model script). Keep it
  verbatim, including the trailing period.
- **Descriptions:** set exactly as written. Tool parameters have no
  descriptions (only the function has one), so schemas stay comparable.
- **Services:** in-memory sessions, artifacts and memory.
- Where the SDK has a built-in for something below (for example `exit_loop`),
  use the built-in and say so in the host's README section; otherwise
  implement it as a plain function tool with the exact name and behavior.

## Shared tool library

| Tool | Signature | Description (function docstring) | Behavior |
|---|---|---|---|
| `add` | `add(a: int, b: int) -> int` | `Adds two integers.` | Returns `a + b` (a bare integer; the SDK wraps it). |
| `get_weather` | `get_weather(city: str) -> dict` | `Returns the weather for a city.` | Returns `{"city": city, "forecast": "sunny", "temperature_c": 21}`. |
| `set_state` | `set_state(key: str, value: str) -> dict` | `Stores a value in session state.` | Writes `state[key] = value` through the tool context; returns `{"status": "ok"}`. |
| `fail_always` | `fail_always() -> dict` | `Always fails.` | Raises/returns the language's normal tool error with message `tool failed on purpose`. |
| `request_approval` | `request_approval(amount: int) -> dict` | `Requests human approval for an expense.` | **Long-running** tool. Returns `{"status": "pending", "ticket": "T-1"}`. |
| `exit_loop` | `exit_loop() -> dict` | `Exits the loop.` | Sets `escalate` on the tool context actions; returns `{}`. |

Integer parameters must be declared as integers (not floats/numbers) if the
SDK allows it.

## Fixtures

### 1. `hello_agent`

- LlmAgent `hello_agent`, no tools.
- description: `Hello-world fixture agent.`
- instruction: `You are a friendly assistant. Answer in one short sentence.`

### 2. `tools_agent`

- LlmAgent `tools_agent`
- description: `Agent with the shared tool library.`
- instruction: `AGENT=tools_agent. Use the tools to answer.`
- tools, in this order: `add`, `get_weather`, `set_state`, `fail_always`, `request_approval`

### 3. `transfer_agent`

- LlmAgent `transfer_agent` (root), no tools.
  - description: `Routes requests to specialists.`
  - instruction: `AGENT=transfer_agent. Route greetings to greeter and goodbyes to farewell.`
  - sub-agents, in this order:
    - LlmAgent `greeter`: description `Greets the user.`, instruction `AGENT=greeter. Greet the user.`
    - LlmAgent `farewell`: description `Says goodbye.`, instruction `AGENT=farewell. Say goodbye.`
- Transfer settings: SDK defaults (transfer to parent and peers allowed).

### 4. `sequential_agent`

- SequentialAgent `sequential_agent`, description `Runs two steps in order.`
  - LlmAgent `step_one`: description `Picks a topic.`, instruction `AGENT=step_one. Pick a topic.`, output_key `topic`
  - LlmAgent `step_two`: description `Writes about the topic.`, instruction `AGENT=step_two. Write one sentence about {topic}.`

### 5. `parallel_agent`

- ParallelAgent `parallel_agent`, description `Runs two agents in parallel.`
  - LlmAgent `left`: description `Left branch.`, instruction `AGENT=left. Answer briefly.`
  - LlmAgent `right`: description `Right branch.`, instruction `AGENT=right. Answer briefly.`

### 6. `loop_agent`

- LoopAgent `loop_agent`, description `Repeats a worker.`, max_iterations `3`
  - LlmAgent `looper`: description `Does one unit of work.`, instruction `AGENT=looper. Do one unit of work; call exit_loop when finished.`, tools: `exit_loop`

### 7. `agent_tool_agent`

- LlmAgent `agent_tool_agent`
  - description: `Delegates to a helper agent used as a tool.`
  - instruction: `AGENT=agent_tool_agent. Ask the helper tool.`
  - tools: AgentTool wrapping LlmAgent `helper` (description `Answers questions for the parent.`, instruction `AGENT=helper. Answer the question.`), SDK default settings.

### 8. `template_agent`

- LlmAgent `template_agent`, no tools.
  - description: `Agent whose instruction reads session state.`
  - instruction: `AGENT=template_agent. The user's name is {user_name}.`

### 9. `remote_agent`

- The SDK's remote A2A agent (RemoteA2aAgent) as the app's root agent, SDK
  defaults: name `remote_agent`, description `Remote A2A agent.`
- Its agent card comes from `--remote-a2a-url` (the well-known path under that
  base URL). Without the flag the URL is a placeholder and the fixture is only
  listed, not runnable.

### 10. `remote_transfer_agent`

- LlmAgent `remote_transfer_agent`, no tools.
  - description: `Routes requests to a remote agent.`
  - instruction: `AGENT=remote_transfer_agent. Route every request to remote_agent.`
  - sub-agents: a fresh `remote_agent` (fixture 9).

## Serving a fixture over A2A

With `--a2a-port N --a2a-app NAME` a host also serves fixture `NAME` over A2A
v1 on `127.0.0.1:N`, through the SDK's own A2A exposure, with its own
in-memory services. The agent card is at `/.well-known/agent-card.json`; the
card's interfaces say where JSON-RPC (and HTTP+JSON, if any) live.

| Host | A2A exposure | JSON-RPC | HTTP+JSON |
|---|---|---|---|
| python | `google.adk.a2a.utils.agent_to_a2a.to_a2a` | `/` | — |
| go | `cmd/launcher/web/a2a` (adk-go's A2A web sublauncher) | `/a2a/v1/invoke` | — |
| rust | adk-server `a2a::v1` components (see hosts/rust) | `/` | `/rest` |

Bare A2A hosts (no ADK) are specified in [hosts/BARE_HOSTS.md](../hosts/BARE_HOSTS.md).
