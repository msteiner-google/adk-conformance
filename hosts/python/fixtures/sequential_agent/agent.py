"""Fixture 4, sequential_agent: step_one's output feeds step_two's prompt."""

from google.adk.agents import LlmAgent
from google.adk.agents import SequentialAgent

from adk_monitor_shared.model import scripted_model

step_one = LlmAgent(
    name="step_one",
    model=scripted_model(),
    description="Picks a topic.",
    instruction="AGENT=step_one. Pick a topic.",
    output_key="topic",
)

step_two = LlmAgent(
    name="step_two",
    model=scripted_model(),
    description="Writes about the topic.",
    instruction="AGENT=step_two. Write one sentence about {topic}.",
)

root_agent = SequentialAgent(
    name="sequential_agent",
    description="Runs two steps in order.",
    sub_agents=[step_one, step_two],
)
