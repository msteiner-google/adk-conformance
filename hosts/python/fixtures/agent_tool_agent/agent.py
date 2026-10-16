"""Fixture 7, agent_tool_agent: calls an LlmAgent wrapped in an AgentTool."""

from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool

from adk_monitor_shared.model import scripted_model

helper = LlmAgent(
    name="helper",
    model=scripted_model(),
    description="Answers questions for the parent.",
    instruction="AGENT=helper. Answer the question.",
)

root_agent = LlmAgent(
    name="agent_tool_agent",
    model=scripted_model(),
    description="Delegates to a helper agent used as a tool.",
    instruction="AGENT=agent_tool_agent. Ask the helper tool.",
    tools=[AgentTool(agent=helper)],
)
