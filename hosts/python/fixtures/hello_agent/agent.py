"""Hello-world fixture agent served by the adk-monitor Python host."""

import os

from google.adk.agents import LlmAgent
from google.adk.models import Gemini

# host.py sets this before the ADK server imports any agent.
_MODEL_BASE_URL = os.environ["ADK_MONITOR_MODEL_BASE_URL"]

root_agent = LlmAgent(
    name="hello_agent",
    model=Gemini(
        model="gemini-2.5-flash",
        base_url=_MODEL_BASE_URL,
        # Gemini Developer API (not Vertex / enterprise) with a fake key.
        client_kwargs={"api_key": "scripted-key", "enterprise": False},
    ),
    description="Hello-world fixture agent.",
    instruction="You are a friendly assistant. Answer in one short sentence.",
)
