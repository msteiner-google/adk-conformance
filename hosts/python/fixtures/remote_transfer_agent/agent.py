"""Fixture 10, remote_transfer_agent: an LlmAgent with a remote sub-agent."""

from google.adk.agents import LlmAgent

from adk_monitor_shared.model import scripted_model
from adk_monitor_shared.remote import remote_agent

root_agent = LlmAgent(
    name="remote_transfer_agent",
    model=scripted_model(),
    description="Routes requests to a remote agent.",
    instruction=(
        "AGENT=remote_transfer_agent. Route every request to remote_agent."
    ),
    sub_agents=[remote_agent()],
)
