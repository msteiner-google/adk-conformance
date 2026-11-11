package adkmonitor;

import com.google.adk.tools.Annotations.Schema;
import com.google.adk.tools.ToolContext;
import java.util.Map;

/**
 * The shared tool library (fixtures/FIXTURES.md), as static methods for adk-java's
 * FunctionTool.create(Class, method).
 *
 * <p>Every parameter carries {@code @Schema(name = ...)}: without it adk-java names parameters
 * from reflection ({@code arg0} unless javac ran with -parameters) and only skips a ToolContext
 * parameter named exactly {@code toolContext}. Parameters have no descriptions; {@code int} is
 * declared INTEGER.
 */
public final class Tools {
  private Tools() {}

  @Schema(name = "add", description = "Adds two integers.")
  public static int add(@Schema(name = "a") int a, @Schema(name = "b") int b) {
    return a + b; // a bare integer; the SDK wraps it as {"result": n}
  }

  @Schema(name = "get_weather", description = "Returns the weather for a city.")
  public static Map<String, Object> getWeather(@Schema(name = "city") String city) {
    return Map.of("city", city, "forecast", "sunny", "temperature_c", 21);
  }

  @Schema(name = "set_state", description = "Stores a value in session state.")
  public static Map<String, Object> setState(
      @Schema(name = "key") String key,
      @Schema(name = "value") String value,
      @Schema(name = "toolContext") ToolContext toolContext) {
    toolContext.state().put(key, value);
    return Map.of("status", "ok");
  }

  /** The language's normal tool error: an exception (adk-java hides its message). */
  @Schema(name = "fail_always", description = "Always fails.")
  public static Map<String, Object> failAlways() {
    throw new IllegalStateException("tool failed on purpose");
  }

  /** Long-running: wrapped in LongRunningFunctionTool by the host. */
  @Schema(name = "request_approval", description = "Requests human approval for an expense.")
  public static Map<String, Object> requestApproval(@Schema(name = "amount") int amount) {
    return Map.of("status", "pending", "ticket", "T-1");
  }
}
