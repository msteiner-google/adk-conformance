"""adk-monitor Python host: serves hello_agent via the ADK API server."""

import argparse
import logging
import os
import pathlib
import sys

_MODEL_BASE_URL_ENV = "ADK_MONITOR_MODEL_BASE_URL"
_HOST = "127.0.0.1"
# abspath, not resolve(): keep Bazel runfiles symlinks instead of following
# them back into the source tree.
_FIXTURES_DIR = pathlib.Path(os.path.abspath(__file__)).parent / "fixtures"


def _parse_args(argv: list[str]) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--http-port", type=int, required=True)
  parser.add_argument("--model-base-url", required=True)
  args, unknown = parser.parse_known_args(argv)
  if unknown:
    logging.warning("Ignoring unknown flags: %s", unknown)
  return args


def _isolate_environment(model_base_url: str) -> None:
  """Pins env so the SDK only talks to the scripted model server."""
  os.environ[_MODEL_BASE_URL_ENV] = model_base_url
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
  _isolate_environment(args.model_base_url)

  if not (_FIXTURES_DIR / "hello_agent" / "agent.py").is_file():
    logging.error("Fixture agent not found under %s", _FIXTURES_DIR)
    return 1

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

  logging.info(
      "Serving ADK API on http://%s:%d (agents_dir=%s, model=%s)",
      _HOST,
      args.http_port,
      _FIXTURES_DIR,
      args.model_base_url,
  )
  server = uvicorn.Server(
      uvicorn.Config(app, host=_HOST, port=args.http_port, log_level="info")
  )
  server.run()
  # uvicorn returns normally (started=False) when it cannot bind the port.
  return 0 if server.started else 1


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
