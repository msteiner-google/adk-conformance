"""Shared tool library from fixtures/FIXTURES.md.

Tool parameters carry no descriptions; only the function docstring does, so
the generated schemas stay comparable across SDKs. `exit_loop` is not defined
here: fixtures use the SDK built-in `google.adk.tools.exit_loop`.
"""

from google.adk.tools import LongRunningFunctionTool
from google.adk.tools import ToolContext


def add(a: int, b: int) -> int:
  """Adds two integers."""
  return a + b


def get_weather(city: str) -> dict:
  """Returns the weather for a city."""
  return {"city": city, "forecast": "sunny", "temperature_c": 21}


def set_state(key: str, value: str, tool_context: ToolContext) -> dict:
  """Stores a value in session state."""
  tool_context.state[key] = value
  return {"status": "ok"}


def fail_always() -> dict:
  """Always fails."""
  raise RuntimeError("tool failed on purpose")


def request_approval(amount: int) -> dict:
  """Requests human approval for an expense."""
  del amount  # Unused: the ticket is always pending.
  return {"status": "pending", "ticket": "T-1"}


# Long-running wrapper; the tool name is the function name, request_approval.
request_approval_tool = LongRunningFunctionTool(func=request_approval)
