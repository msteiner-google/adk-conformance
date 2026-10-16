"""Fixture 1, hello_agent: a plain LlmAgent with no tools."""

from google.adk.agents import LlmAgent

from adk_monitor_shared.model import scripted_model

root_agent = LlmAgent(
    name="hello_agent",
    model=scripted_model(),
    description="Hello-world fixture agent.",
    instruction="You are a friendly assistant. Answer in one short sentence.",
)
