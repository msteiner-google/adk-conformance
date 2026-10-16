"""Fixture 5, parallel_agent: two LLM branches run in parallel."""

from google.adk.agents import LlmAgent
from google.adk.agents import ParallelAgent

from adk_monitor_shared.model import scripted_model

left = LlmAgent(
    name="left",
    model=scripted_model(),
    description="Left branch.",
    instruction="AGENT=left. Answer briefly.",
)

right = LlmAgent(
    name="right",
    model=scripted_model(),
    description="Right branch.",
    instruction="AGENT=right. Answer briefly.",
)

root_agent = ParallelAgent(
    name="parallel_agent",
    description="Runs two agents in parallel.",
    sub_agents=[left, right],
)
