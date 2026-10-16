"""Fixture 8, template_agent: its instruction reads {user_name} from state."""

from google.adk.agents import LlmAgent

from adk_monitor_shared.model import scripted_model

root_agent = LlmAgent(
    name="template_agent",
    model=scripted_model(),
    description="Agent whose instruction reads session state.",
    instruction="AGENT=template_agent. The user's name is {user_name}.",
)
