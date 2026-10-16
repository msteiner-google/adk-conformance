"""Fixture 3, transfer_agent: a router with two LLM sub-agents.

Transfer settings are the SDK defaults (parent and peer transfer allowed).
"""

from google.adk.agents import LlmAgent

from adk_monitor_shared.model import scripted_model

greeter = LlmAgent(
    name="greeter",
    model=scripted_model(),
    description="Greets the user.",
    instruction="AGENT=greeter. Greet the user.",
)

farewell = LlmAgent(
    name="farewell",
    model=scripted_model(),
    description="Says goodbye.",
    instruction="AGENT=farewell. Say goodbye.",
)

root_agent = LlmAgent(
    name="transfer_agent",
    model=scripted_model(),
    description="Routes requests to specialists.",
    instruction=(
        "AGENT=transfer_agent. Route greetings to greeter and goodbyes to"
        " farewell."
    ),
    sub_agents=[greeter, farewell],
)
