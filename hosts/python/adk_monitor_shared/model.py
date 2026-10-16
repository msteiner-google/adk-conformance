"""Builds the scripted Gemini model every fixture LLM agent uses."""

import os

from google.adk.models import Gemini

# host.py sets this before the ADK server imports any agent.
MODEL_BASE_URL_ENV = "ADK_MONITOR_MODEL_BASE_URL"
MODEL_NAME = "gemini-2.5-flash"
API_KEY = "scripted-key"


def scripted_model() -> Gemini:
  """Returns gemini-2.5-flash pointed at the scripted model server.

  Uses the Gemini Developer API (not Vertex / enterprise) with a fake key.
  """
  return Gemini(
      model=MODEL_NAME,
      base_url=os.environ[MODEL_BASE_URL_ENV],
      client_kwargs={"api_key": API_KEY, "enterprise": False},
  )
