package adkmonitor;

import com.google.adk.a2a.agent.RemoteA2AAgent;
import com.google.adk.agents.BaseAgent;
import com.google.adk.agents.LlmAgent;
import com.google.adk.agents.LoopAgent;
import com.google.adk.agents.ParallelAgent;
import com.google.adk.agents.SequentialAgent;
import com.google.adk.models.Gemini;
import com.google.adk.tools.AgentTool;
import com.google.adk.tools.ExitLoopTool;
import com.google.adk.tools.FunctionTool;
import com.google.adk.tools.LongRunningFunctionTool;
import com.google.adk.web.AdkWebServer;
import com.google.genai.Client;
import com.google.genai.types.HttpOptions;
import io.a2a.client.config.ClientConfig;
import io.a2a.client.http.A2ACardResolver;
import io.a2a.client.http.JdkA2AHttpClient;
import io.a2a.client.transport.jsonrpc.JSONRPCTransport;
import io.a2a.client.transport.jsonrpc.JSONRPCTransportConfig;
import io.a2a.spec.AgentCapabilities;
import io.a2a.spec.AgentCard;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * adk-monitor Java host: serves every fixture in fixtures/FIXTURES.md as a separate app (app
 * name == root agent name) through adk-java's own ADK REST API server (google-adk-dev
 * AdkWebServer, Spring Boot), base path "".
 *
 * <pre>
 *   bazel run //hosts/java:host -- --http-port 18766 --model-base-url http://127.0.0.1:18765
 * </pre>
 *
 * <p>A2A (host contract, M2): adk-java 1.14.0 speaks A2A 0.3 only (google/adk-java#1194) and
 * ships no A2A server, so --a2a-port/--a2a-app are rejected (the java subject sets a2a_server and
 * a2a_client to False; the harness reports A2A scenarios as UNSUPPORTED without starting the
 * host). The remote fixtures still use the SDK's RemoteA2AAgent (A2A 0.3 JSON-RPC client).
 *
 * <p>See README.md in this directory for the SDK built-ins the fixtures use.
 */
public final class Host {
  private static final String MODEL = "gemini-2.5-flash";
  private static final String API_KEY = "scripted-key";
  private static final String BIND_HOST = "127.0.0.1";
  private static final String PLACEHOLDER_REMOTE_URL = "http://127.0.0.1:9";

  private Host() {}

  public static void main(String[] argv) throws Exception {
    Map<String, String> flags;
    try {
      flags = parseFlags(argv);
    } catch (IllegalArgumentException e) {
      System.err.println("java host: " + e.getMessage());
      System.exit(2);
      return;
    }
    int port = parsePort(flags.get("http-port"));
    String modelBaseUrl = flags.get("model-base-url");
    if (port <= 0 || modelBaseUrl == null || modelBaseUrl.isEmpty()) {
      System.err.println("java host: --http-port (1..65535) and --model-base-url are required");
      System.exit(2);
    }
    if (flags.containsKey("a2a-port") || flags.containsKey("a2a-app")) {
      System.err.println(
          "java host: adk-java 1.14.0 has no A2A v1 server; --a2a-port/--a2a-app are not supported");
      System.exit(2);
    }
    String remoteUrl = flags.getOrDefault("remote-a2a-url", PLACEHOLDER_REMOTE_URL);

    // vertexAI(false): an explicit Gemini API client, whatever GOOGLE_GENAI_USE_VERTEXAI says.
    Client client =
        Client.builder()
            .apiKey(API_KEY)
            .vertexAI(false)
            .httpOptions(HttpOptions.builder().baseUrl(modelBaseUrl).build())
            .build();
    Gemini model = new Gemini(MODEL, client);

    // AdkWebServer.start takes no port or address: Spring Boot reads these properties.
    System.setProperty("server.port", Integer.toString(port));
    System.setProperty("server.address", BIND_HOST);
    System.setProperty("spring.main.banner-mode", "off");
    // Serves each agent under its name; in-memory session, artifact and memory services.
    // Returns once Tomcat is up; its non-daemon threads keep the JVM alive.
    AdkWebServer.start(fixtures(model, remoteUrl).toArray(new BaseAgent[0]));
  }

  // --- Fixtures --------------------------------------------------------------------------------

  static List<BaseAgent> fixtures(Gemini m, String remoteUrl) throws Exception {
    return List.of(
        llm(m, "hello_agent", "Hello-world fixture agent.",
                "You are a friendly assistant. Answer in one short sentence.")
            .build(),
        llm(m, "tools_agent", "Agent with the shared tool library.",
                "AGENT=tools_agent. Use the tools to answer.")
            .tools(
                FunctionTool.create(Tools.class, "add"),
                FunctionTool.create(Tools.class, "getWeather"),
                FunctionTool.create(Tools.class, "setState"),
                FunctionTool.create(Tools.class, "failAlways"),
                LongRunningFunctionTool.create(Tools.class, "requestApproval"))
            .build(),
        llm(m, "transfer_agent", "Routes requests to specialists.",
                "AGENT=transfer_agent. Route greetings to greeter and goodbyes to farewell.")
            .subAgents(
                llm(m, "greeter", "Greets the user.", "AGENT=greeter. Greet the user.").build(),
                llm(m, "farewell", "Says goodbye.", "AGENT=farewell. Say goodbye.").build())
            .build(),
        SequentialAgent.builder()
            .name("sequential_agent")
            .description("Runs two steps in order.")
            .subAgents(
                llm(m, "step_one", "Picks a topic.", "AGENT=step_one. Pick a topic.")
                    .outputKey("topic")
                    .build(),
                llm(m, "step_two", "Writes about the topic.",
                        "AGENT=step_two. Write one sentence about {topic}.")
                    .build())
            .build(),
        ParallelAgent.builder()
            .name("parallel_agent")
            .description("Runs two agents in parallel.")
            .subAgents(
                llm(m, "left", "Left branch.", "AGENT=left. Answer briefly.").build(),
                llm(m, "right", "Right branch.", "AGENT=right. Answer briefly.").build())
            .build(),
        LoopAgent.builder()
            .name("loop_agent")
            .description("Repeats a worker.")
            .maxIterations(3)
            .subAgents(
                llm(m, "looper", "Does one unit of work.",
                        "AGENT=looper. Do one unit of work; call exit_loop when finished.")
                    .tools(ExitLoopTool.INSTANCE)
                    .build())
            .build(),
        llm(m, "agent_tool_agent", "Delegates to a helper agent used as a tool.",
                "AGENT=agent_tool_agent. Ask the helper tool.")
            .tools(
                AgentTool.create(
                    llm(m, "helper", "Answers questions for the parent.",
                            "AGENT=helper. Answer the question.")
                        .build()))
            .build(),
        llm(m, "template_agent", "Agent whose instruction reads session state.",
                "AGENT=template_agent. The user's name is {user_name}.")
            .build(),
        remoteAgent(remoteUrl),
        llm(m, "remote_transfer_agent", "Routes requests to a remote agent.",
                "AGENT=remote_transfer_agent. Route every request to remote_agent.")
            .subAgents(remoteAgent(remoteUrl))
            .build());
  }

  private static LlmAgent.Builder llm(Gemini m, String name, String description, String instruction) {
    return LlmAgent.builder().name(name).description(description).model(m).instruction(instruction);
  }

  /**
   * RemoteA2AAgent needs a card at construction. With --remote-a2a-url the card is fetched from
   * the well-known path; without it the placeholder refuses connections, so a hand-built card
   * keeps the fixture listed.
   */
  private static RemoteA2AAgent remoteAgent(String url) throws Exception {
    AgentCard card;
    if (url.equals(PLACEHOLDER_REMOTE_URL)) {
      card =
          new AgentCard.Builder()
              .name("remote_agent")
              .description("Remote A2A agent.")
              .url(url)
              .preferredTransport("JSONRPC")
              .version("0")
              .capabilities(new AgentCapabilities.Builder().build())
              .defaultInputModes(List.of("text"))
              .defaultOutputModes(List.of("text"))
              .skills(List.of())
              .build();
    } else {
      card = new A2ACardResolver(new JdkA2AHttpClient(), url).getAgentCard();
    }
    io.a2a.client.Client a2a =
        io.a2a.client.Client.builder(card)
            .withTransport(JSONRPCTransport.class, new JSONRPCTransportConfig())
            .clientConfig(
                new ClientConfig.Builder().setStreaming(card.capabilities().streaming()).build())
            .build();
    return RemoteA2AAgent.builder()
        .name("remote_agent")
        .description("Remote A2A agent.")
        .agentCard(card)
        .a2aClient(a2a)
        .build();
  }

  // --- Flags -----------------------------------------------------------------------------------

  /** Parses {@code --name value} and {@code --name=value}. */
  static Map<String, String> parseFlags(String[] argv) {
    Map<String, String> flags = new HashMap<>();
    for (int i = 0; i < argv.length; i++) {
      String arg = argv[i];
      if (!arg.startsWith("--")) {
        throw new IllegalArgumentException("unexpected argument: " + arg);
      }
      int eq = arg.indexOf('=');
      if (eq > 0) {
        flags.put(arg.substring(2, eq), arg.substring(eq + 1));
      } else if (i + 1 < argv.length) {
        flags.put(arg.substring(2), argv[++i]);
      } else {
        throw new IllegalArgumentException(arg + " needs a value");
      }
    }
    return flags;
  }

  private static int parsePort(String value) {
    try {
      int port = Integer.parseInt(value);
      return port >= 1 && port <= 65535 ? port : -1;
    } catch (NumberFormatException e) {
      return -1;
    }
  }
}
