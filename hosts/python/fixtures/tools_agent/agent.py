"""Fixture 2, tools_agent: an LlmAgent with the shared tool library."""

from google.adk.agents import LlmAgent

from adk_monitor_shared import tools
from adk_monitor_shared.model import scripted_model

root_agent = LlmAgent(
    name="tools_agent",
    model=scripted_model(),
    description="Agent with the shared tool library.",
    instruction="AGENT=tools_agent. Use the tools to answer.",
    tools=[
        tools.add,
        tools.get_weather,
        tools.set_state,
        tools.fail_always,
        tools.request_approval_tool,
    ],
)
