# Kotlin host (adk-kotlin 1.5.0)

`Host.kt` serves every fixture in `fixtures/FIXTURES.md` through
`com.google.adk.kt.webserver.AdkApiServer` (google-adk-kotlin-webserver, Ktor
on Netty; the REST API without the dev UI), API base path `""`, with
`InMemoryAppLoader`, `InMemorySessionService` and `InMemoryArtifactService`.
The server builds an in-memory runner per request, so the memory service is
the runner's default `InMemoryMemoryService`.

```sh
bazel run //hosts/kotlin:host -- --http-port 8080 --model-base-url http://127.0.0.1:18765
```

Dependencies: `maven.install(name = "maven_kotlin")` in `MODULE.bazel`, locked
in `maven_install.json`. The KMP modules are used through their `-jvm`
artifacts (Bazel does not read Gradle module metadata). No KSP and no
kotlinx-serialization plugin are needed.

## SDK built-ins used

- `exit_loop`: `com.google.adk.kt.tools.ExitLoopTool`. Its description is
  "Exits the loop.\n\nCall this function only when you are instructed to do
  so.\n"; it returns `Unit` (response `{"result": null}`) and also sets
  `skipSummarization`.
- Shared tools: hand-written `FunctionTool` subclasses with explicit
  `FunctionDeclaration`s instead of the KSP `@Tool` processor. They omit the
  `response` schema the processor would generate, and `request_approval`
  appends `FunctionTool.LONG_RUNNING_OPERATION_NOTE` to its description as the
  processor does.
- `helper`: `AgentTool` with default settings (parameter `request`).
- `transfer_to_agent`: added by adk-kotlin for `transfer_agent`'s sub-agents.
- `remote_agent`: `com.google.adk.kt.a2a.jvm.A2AAgent` (A2A v1 JSON-RPC client
  on a2a-java 1.3.2). It fetches the card at construction: with
  `--remote-a2a-url` at startup, otherwise a hand-built card points at the
  placeholder URL. `description` is passed explicitly; the SDK default is the
  remote card's description.
- Model: `Gemini(Client(apiKey, enterprise = false, HttpOptions(baseUrl)))`.
  This path sets no retry options, so the scripted server sees one request
  per model call.

## Known gaps (SDK, not host)

- No A2A server: adk-kotlin has only the A2A client. The `kotlin` subject has
  `a2a_server = False`, so A2A server scenarios (and client scenarios with
  kotlin as the server) report UNSUPPORTED; the host rejects
  `--a2a-port`/`--a2a-app`.
- `fail_always` throws, and adk-kotlin aborts the run: `/run_sse` ends with a
  `data: {"error": "IllegalStateException: tool failed on purpose"}` frame.
- REST events omit default-valued fields (no empty `actions`, `partial`,
  `longRunningToolIds`), and timestamps are epoch milliseconds.
