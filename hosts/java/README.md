# Java host (adk-java 1.14.0)

`Host.java` serves every fixture in `fixtures/FIXTURES.md` through
`com.google.adk.web.AdkWebServer.start(BaseAgent...)` (google-adk-dev, Spring
Boot 4, embedded Tomcat), API base path `""`. Port and bind address go in as
the Spring properties `server.port` and `server.address`; the server keeps
its default in-memory session, artifact and memory services. The dev UI
routes stay mounted (there is no switch); the scenarios do not use them.

```sh
bazel run //hosts/java:host -- --http-port 8080 --model-base-url http://127.0.0.1:18765
```

Dependencies: `maven.install(name = "maven_java")` in `MODULE.bazel`, locked
in `maven_install.json`. Run the host from runfiles, never as a `_deploy.jar`
(Spring Boot reads `META-INF/spring/*.imports` from many jars).

## SDK built-ins used

- `exit_loop`: `com.google.adk.tools.ExitLoopTool.INSTANCE`. Its description
  is "Exits the loop.\n\nCall this function only when you are instructed to
  do so." (as in Python); it also sets `skipSummarization`.
- Shared tools: static methods in `Tools.java` wrapped with
  `FunctionTool.create(Tools.class, name)`. Every parameter carries
  `@Schema(name = ...)`, since adk-java otherwise names parameters `arg0`.
- `request_approval`: `LongRunningFunctionTool.create`. Unlike Python, adk-java
  adds no long-running note to the description.
- `helper`: `AgentTool.create` with default settings (parameter `request`).
- `transfer_to_agent`: added by adk-java for `transfer_agent`'s sub-agents.
- `remote_agent`: `com.google.adk.a2a.agent.RemoteA2AAgent` with an a2a-java
  0.3.2 JSON-RPC client. It needs the card at construction: with
  `--remote-a2a-url` it is fetched at startup, otherwise a hand-built card
  points at the placeholder URL.

## Known gaps (SDK, not host)

- No A2A v1: adk-java speaks A2A 0.3 only and ships no A2A server
  ([google/adk-java#1194](https://github.com/google/adk-java/issues/1194)).
  The `java` subject has `a2a_server = False, a2a_client = False`, so every A2A
  scenario reports UNSUPPORTED; the host rejects `--a2a-port`/`--a2a-app`.
- `fail_always` throws, and adk-java turns any tool exception into the
  function response `{"status": "error", "message": "An internal error
  occurred."}`; the message `tool failed on purpose` never reaches the model.
- genai-java retries 5xx model responses (five attempts in total), so a
  scripted model error shows up as several LLM requests.
