"""adk-monitor Python host: serves every fixture via the ADK API server.

Each package under fixtures/ is one app (app name == root agent name). Shared
code lives in adk_monitor_shared/, next to this file, so the agent loader does
not list it as an app.

A2A (host contract, M2):
  --a2a-port N --a2a-app NAME   also serve fixture NAME over A2A v1 at
                                http://127.0.0.1:N/ with google-adk's own
                                ``to_a2a`` (card at /.well-known/agent-card.json).
  --remote-a2a-url URL          base URL of a remote A2A agent; the
                                remote_agent / remote_transfer_agent fixtures
                                call it through RemoteA2aAgent.
"""

import argparse
import asyncio
import logging
import os
import pathlib
import sys

_MODEL_BASE_URL_ENV = "ADK_MONITOR_MODEL_BASE_URL"
_REMOTE_A2A_URL_ENV = "ADK_MONITOR_REMOTE_A2A_URL"
_HOST = "127.0.0.1"
# abspath, not resolve(): keep Bazel runfiles symlinks instead of following
# them back into the source tree.
_HOST_DIR = pathlib.Path(os.path.abspath(__file__)).parent
_FIXTURES_DIR = _HOST_DIR / "fixtures"
_FIXTURE_APPS = (
    "hello_agent",
    "tools_agent",
    "transfer_agent",
    "sequential_agent",
    "parallel_agent",
    "loop_agent",
    "agent_tool_agent",
    "template_agent",
    "remote_agent",
    "remote_transfer_agent",
)


def _parse_args(argv: list[str]) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--http-port", type=int, required=True)
  parser.add_argument("--model-base-url", required=True)
  parser.add_argument("--a2a-port", type=int, default=None)
  parser.add_argument("--a2a-app", default=None)
  parser.add_argument("--remote-a2a-url", default=None)
  args, unknown = parser.parse_known_args(argv)
  if unknown:
    logging.warning("Ignoring unknown flags: %s", unknown)
  return args


def _isolate_environment(model_base_url: str, remote_a2a_url: str | None) -> None:
  """Pins env so the SDK only talks to the scripted model server."""
  os.environ[_MODEL_BASE_URL_ENV] = model_base_url
  # The remote fixtures resolve the card lazily, so a placeholder is fine when
  # no remote agent is configured.
  os.environ[_REMOTE_A2A_URL_ENV] = remote_a2a_url or "http://127.0.0.1:9"
  os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
  os.environ["GOOGLE_GENAI_USE_ENTERPRISE"] = "false"
  os.environ["GOOGLE_API_KEY"] = "scripted-key"
  os.environ.pop("GEMINI_API_KEY", None)
  os.environ.pop("GOOGLE_GEMINI_BASE_URL", None)
  os.environ.pop("GOOGLE_CLOUD_AGENT_ENGINE_ID", None)
  # The agent loader walks up to / looking for a .env; keep runs hermetic.
  os.environ["ADK_DISABLE_LOAD_DOTENV"] = "1"


def main(argv: list[str]) -> int:
  logging.basicConfig(
      stream=sys.stderr,
      level=logging.INFO,
      format="%(asctime)s %(levelname)s %(name)s: %(message)s",
  )
  args = _parse_args(argv)
  if (args.a2a_port is None) != (args.a2a_app is None):
    logging.error("--a2a-port and --a2a-app go together")
    return 2
  _isolate_environment(args.model_base_url, args.remote_a2a_url)

  missing = [
      name
      for name in _FIXTURE_APPS
      if not (_FIXTURES_DIR / name / "agent.py").is_file()
  ]
  if missing:
    logging.error(
        "Fixture agents %s not found under %s", missing, _FIXTURES_DIR
    )
    return 1
  # Fixtures import adk_monitor_shared; make it importable natively and
  # under Bazel runfiles alike.
  if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

  # Import after the env is pinned so nothing reads stale settings.
  import uvicorn
  from google.adk.cli.fast_api import get_fast_api_app

  try:
    app = get_fast_api_app(
        agents_dir=str(_FIXTURES_DIR),
        session_service_uri="memory://",
        artifact_service_uri="memory://",
        memory_service_uri="memory://",
        use_local_storage=False,
        web=False,
        a2a=False,
        host=_HOST,
        bind_host=_HOST,
        port=args.http_port,
    )
  except Exception:  # pylint: disable=broad-except
    logging.exception("Failed to build the ADK API server app")
    return 1

  servers = [
      uvicorn.Server(
          uvicorn.Config(app, host=_HOST, port=args.http_port, log_level="info")
      )
  ]
  logging.info(
      "Serving ADK API on http://%s:%d (agents_dir=%s, model=%s)",
      _HOST,
      args.http_port,
      _FIXTURES_DIR,
      args.model_base_url,
  )
  if args.a2a_app:
    try:
      a2a_app = _build_a2a_app(args.a2a_app, args.a2a_port)
    except Exception:  # pylint: disable=broad-except
      logging.exception("Failed to build the A2A app for %s", args.a2a_app)
      return 1
    servers.append(
        uvicorn.Server(
            uvicorn.Config(
                a2a_app, host=_HOST, port=args.a2a_port, log_level="info"
            )
        )
    )
    logging.info(
        "Serving fixture %s over A2A on http://%s:%d/",
        args.a2a_app,
        _HOST,
        args.a2a_port,
    )

  async def serve_all() -> None:
    await asyncio.gather(*(server.serve() for server in servers))

  asyncio.run(serve_all())
  # uvicorn returns normally (started=False) when it cannot bind the port.
  return 0 if all(server.started for server in servers) else 1


def _build_a2a_app(app_name: str, port: int):
  """Fixture ``app_name`` as an A2A v1 Starlette app, via google-adk's to_a2a.

  The fixture comes from ADK's own agent loader; to_a2a gives it its own
  runner with in-memory services and builds the card from the agent.
  """
  from google.adk.a2a.utils.agent_to_a2a import to_a2a
  from google.adk.apps import App
  from google.adk.cli.utils.agent_loader import AgentLoader

  loaded = AgentLoader(str(_FIXTURES_DIR)).load_agent(app_name)
  agent = loaded.root_agent if isinstance(loaded, App) else loaded
  return to_a2a(agent, host=_HOST, port=port)


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
