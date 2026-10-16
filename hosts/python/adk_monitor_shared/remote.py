"""Builds the RemoteA2aAgent the remote_* fixtures use (FIXTURES.md, fixture 9-10)."""

import os

from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

# host.py sets this from --remote-a2a-url before any agent is imported.
REMOTE_A2A_URL_ENV = "ADK_MONITOR_REMOTE_A2A_URL"
CARD_PATH = "/.well-known/agent-card.json"


def remote_agent() -> RemoteA2aAgent:
  """RemoteA2aAgent `remote_agent`, SDK defaults, card from --remote-a2a-url."""
  base = os.environ[REMOTE_A2A_URL_ENV].rstrip("/")
  return RemoteA2aAgent(
      name="remote_agent",
      description="Remote A2A agent.",
      agent_card=f"{base}{CARD_PATH}",
  )
