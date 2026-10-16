"""Fixture 6, loop_agent: repeats looper up to 3 times.

Uses the SDK built-in google.adk.tools.exit_loop.
"""

from google.adk.agents import LlmAgent
from google.adk.agents import LoopAgent
from google.adk.tools import exit_loop

from adk_monitor_shared.model import scripted_model

looper = LlmAgent(
    name="looper",
    model=scripted_model(),
    description="Does one unit of work.",
    instruction=(
        "AGENT=looper. Do one unit of work; call exit_loop when finished."
    ),
    tools=[exit_loop],
)

root_agent = LoopAgent(
    name="loop_agent",
    description="Repeats a worker.",
    max_iterations=3,
    sub_agents=[looper],
)
