// adk-monitor Kotlin host: serves every fixture in fixtures/FIXTURES.md as a
// separate app (app name == root agent name) through adk-kotlin's own ADK REST
// API server (google-adk-kotlin-webserver AdkApiServer, Ktor/Netty), base path "".
//
//   bazel run //hosts/kotlin:host -- --http-port 18766 --model-base-url http://127.0.0.1:18765
//
// A2A (host contract, M2): adk-kotlin 1.5.0 has no A2A server, so --a2a-port and
// --a2a-app are rejected (the kotlin subject sets a2a_server = False and the
// harness reports those scenarios as UNSUPPORTED without starting the host).
// --remote-a2a-url URL points the remote_agent / remote_transfer_agent fixtures
// at a remote A2A v1 agent through the SDK's A2AAgent (a2a-java client).
//
// See README.md in this directory for the SDK built-ins the fixtures use.
package adkmonitor.kotlin

import com.google.adk.kt.a2a.agent.BaseRemoteA2AAgent
import com.google.adk.kt.a2a.jvm.A2AAgent
import com.google.adk.kt.agents.BaseAgent
import com.google.adk.kt.agents.Instruction
import com.google.adk.kt.agents.LlmAgent
import com.google.adk.kt.agents.LoopAgent
import com.google.adk.kt.agents.ParallelAgent
import com.google.adk.kt.agents.SequentialAgent
import com.google.adk.kt.apps.App
import com.google.adk.kt.artifacts.InMemoryArtifactService
import com.google.adk.kt.models.Gemini
import com.google.adk.kt.sessions.InMemorySessionService
import com.google.adk.kt.tools.AgentTool
import com.google.adk.kt.tools.BaseTool
import com.google.adk.kt.tools.ExitLoopTool
import com.google.adk.kt.tools.FunctionTool
import com.google.adk.kt.tools.ToolContext
import com.google.adk.kt.types.FunctionDeclaration
import com.google.adk.kt.types.Schema
import com.google.adk.kt.types.Type
import com.google.adk.kt.webserver.AdkApiServer
import com.google.adk.kt.webserver.AdkServerConfig
import com.google.adk.kt.webserver.loaders.InMemoryAppLoader
import com.google.genai.kotlin.Client
import com.google.genai.kotlin.types.HttpOptions
import kotlin.system.exitProcess
import kotlinx.coroutines.runBlocking
import org.a2aproject.sdk.spec.AgentCapabilities
import org.a2aproject.sdk.spec.AgentCard
import org.a2aproject.sdk.spec.AgentInterface

private const val MODEL = "gemini-2.5-flash"
private const val API_KEY = "scripted-key"
private const val BIND_HOST = "127.0.0.1"
private const val PLACEHOLDER_REMOTE_URL = "http://127.0.0.1:9"

// --- Shared tool library -------------------------------------------------------------
// Hand-written FunctionTool subclasses with explicit declarations (no KSP @Tool
// processor). Parameters have no descriptions; integers are declared INTEGER.

private abstract class FixtureTool(
  name: String,
  description: String,
  private val params: Map<String, Type> = emptyMap(),
  isLongRunning: Boolean = false,
) : FunctionTool(name, description, isLongRunning) {
  override fun declaration() =
    FunctionDeclaration(
      name = name,
      // The KSP processor appends this note to long-running tools; do the same.
      description = if (isLongRunning) "$description\n\n$LONG_RUNNING_OPERATION_NOTE" else description,
      parameters =
        if (params.isEmpty()) null
        else
          Schema(
            type = Type.OBJECT,
            properties = params.mapValues { (_, type) -> Schema(type = type) },
            required = params.keys.toList(),
          ),
    )
}

private fun Any?.asInt() = (this as Number).toInt()

private fun sharedTools(): List<BaseTool> =
  listOf(
    object : FixtureTool("add", "Adds two integers.", mapOf("a" to Type.INTEGER, "b" to Type.INTEGER)) {
      // A bare Int: the SDK wraps it as {"result": n}.
      override suspend fun execute(context: ToolContext, args: Map<String, Any?>): Any =
        args["a"].asInt() + args["b"].asInt()
    },
    object : FixtureTool("get_weather", "Returns the weather for a city.", mapOf("city" to Type.STRING)) {
      override suspend fun execute(context: ToolContext, args: Map<String, Any?>): Any =
        mapOf("city" to args["city"], "forecast" to "sunny", "temperature_c" to 21)
    },
    object :
      FixtureTool("set_state", "Stores a value in session state.", mapOf("key" to Type.STRING, "value" to Type.STRING)) {
      override suspend fun execute(context: ToolContext, args: Map<String, Any?>): Any {
        context.updateState(args["key"] as String, args["value"] as String)
        return mapOf("status" to "ok")
      }
    },
    object : FixtureTool("fail_always", "Always fails.") {
      // The language's normal tool error: an exception (adk-kotlin aborts the run).
      override suspend fun execute(context: ToolContext, args: Map<String, Any?>): Any =
        throw IllegalStateException("tool failed on purpose")
    },
    object :
      FixtureTool(
        "request_approval",
        "Requests human approval for an expense.",
        mapOf("amount" to Type.INTEGER),
        isLongRunning = true,
      ) {
      // A non-Unit result is emitted as the function response.
      override suspend fun execute(context: ToolContext, args: Map<String, Any?>): Any =
        mapOf("status" to "pending", "ticket" to "T-1")
    },
  )

// --- Fixtures ----------------------------------------------------------------------------

private fun llm(
  model: Gemini,
  name: String,
  description: String,
  instruction: String,
  tools: List<BaseTool> = emptyList(),
  subAgents: List<BaseAgent> = emptyList(),
  outputKey: String? = null,
) =
  LlmAgent(
    name = name,
    model = model,
    description = description,
    instruction = Instruction(instruction),
    tools = tools,
    subAgents = subAgents,
    outputKey = outputKey,
  )

// A2AAgent fetches the card eagerly. Without --remote-a2a-url the placeholder
// refuses connections; build the card by hand so the fixture is still listed.
private suspend fun remoteAgent(url: String): BaseRemoteA2AAgent {
  val description = "Remote A2A agent."
  if (url != PLACEHOLDER_REMOTE_URL) {
    return A2AAgent(name = "remote_agent", agentCardUrl = url, description = description)
  }
  val card =
    AgentCard.builder()
      .name("remote_agent")
      .description(description)
      .version("0")
      .capabilities(AgentCapabilities.builder().build())
      .defaultInputModes(listOf("text"))
      .defaultOutputModes(listOf("text"))
      .skills(emptyList())
      .supportedInterfaces(listOf(AgentInterface("JSONRPC", url)))
      .build()
  return A2AAgent(name = "remote_agent", agentCard = card, description = description)
}

private suspend fun fixtures(m: Gemini, remoteUrl: String): List<BaseAgent> =
  listOf(
    llm(m, "hello_agent", "Hello-world fixture agent.", "You are a friendly assistant. Answer in one short sentence."),
    llm(
      m, "tools_agent", "Agent with the shared tool library.", "AGENT=tools_agent. Use the tools to answer.",
      tools = sharedTools(),
    ),
    llm(
      m, "transfer_agent", "Routes requests to specialists.",
      "AGENT=transfer_agent. Route greetings to greeter and goodbyes to farewell.",
      subAgents =
        listOf(
          llm(m, "greeter", "Greets the user.", "AGENT=greeter. Greet the user."),
          llm(m, "farewell", "Says goodbye.", "AGENT=farewell. Say goodbye."),
        ),
    ),
    SequentialAgent(
      name = "sequential_agent",
      description = "Runs two steps in order.",
      subAgents =
        listOf(
          llm(m, "step_one", "Picks a topic.", "AGENT=step_one. Pick a topic.", outputKey = "topic"),
          llm(m, "step_two", "Writes about the topic.", "AGENT=step_two. Write one sentence about {topic}."),
        ),
    ),
    ParallelAgent(
      name = "parallel_agent",
      description = "Runs two agents in parallel.",
      subAgents =
        listOf(
          llm(m, "left", "Left branch.", "AGENT=left. Answer briefly."),
          llm(m, "right", "Right branch.", "AGENT=right. Answer briefly."),
        ),
    ),
    LoopAgent(
      name = "loop_agent",
      maxIterations = 3,
      description = "Repeats a worker.",
      subAgents =
        listOf(
          llm(
            m, "looper", "Does one unit of work.",
            "AGENT=looper. Do one unit of work; call exit_loop when finished.",
            tools = listOf(ExitLoopTool()),
          )
        ),
    ),
    llm(
      m, "agent_tool_agent", "Delegates to a helper agent used as a tool.",
      "AGENT=agent_tool_agent. Ask the helper tool.",
      tools =
        listOf(AgentTool(llm(m, "helper", "Answers questions for the parent.", "AGENT=helper. Answer the question."))),
    ),
    llm(
      m, "template_agent", "Agent whose instruction reads session state.",
      "AGENT=template_agent. The user's name is {user_name}.",
    ),
    remoteAgent(remoteUrl),
    llm(
      m, "remote_transfer_agent", "Routes requests to a remote agent.",
      "AGENT=remote_transfer_agent. Route every request to remote_agent.",
      subAgents = listOf(remoteAgent(remoteUrl)),
    ),
  )

// --- Main ----------------------------------------------------------------------------------

/** Parses `--name value` and `--name=value`. */
private fun parseFlags(argv: Array<String>): Map<String, String> {
  val flags = mutableMapOf<String, String>()
  var i = 0
  while (i < argv.size) {
    val arg = argv[i]
    require(arg.startsWith("--")) { "unexpected argument: $arg" }
    val eq = arg.indexOf('=')
    if (eq > 0) {
      flags[arg.substring(2, eq)] = arg.substring(eq + 1)
    } else {
      require(i + 1 < argv.size) { "$arg needs a value" }
      flags[arg.substring(2)] = argv[++i]
    }
    i++
  }
  return flags
}

fun main(argv: Array<String>) {
  val flags =
    try {
      parseFlags(argv)
    } catch (e: IllegalArgumentException) {
      System.err.println("kotlin host: ${e.message}")
      exitProcess(2)
    }
  val port = flags["http-port"]?.toIntOrNull()
  val modelBaseUrl = flags["model-base-url"]
  if (port == null || port !in 1..65535 || modelBaseUrl.isNullOrEmpty()) {
    System.err.println("kotlin host: --http-port (1..65535) and --model-base-url are required")
    exitProcess(2)
  }
  if ("a2a-port" in flags || "a2a-app" in flags) {
    System.err.println("kotlin host: adk-kotlin has no A2A server; --a2a-port/--a2a-app are not supported")
    exitProcess(2)
  }
  val remoteUrl = flags["remote-a2a-url"] ?: PLACEHOLDER_REMOTE_URL

  // Client(...) rather than Gemini(name, apiKey): no retry options, so the
  // scripted server sees exactly one request per model call. enterprise=false
  // keeps the client off Vertex regardless of the environment.
  val client = Client(apiKey = API_KEY, enterprise = false, httpOptions = HttpOptions(baseUrl = modelBaseUrl))
  val model = Gemini(client, MODEL)
  val apps = runBlocking { fixtures(model, remoteUrl) }.map { App(appName = it.name, rootAgent = it) }

  // SDK defaults otherwise (web UI off for AdkApiServer, wire casing as shipped).
  val config =
    AdkServerConfig(
      appLoader = InMemoryAppLoader(*apps.toTypedArray()),
      sessionService = InMemorySessionService(),
      artifactService = InMemoryArtifactService(),
      host = BIND_HOST,
      port = port,
    )
  AdkApiServer(config).start(wait = true)
}
