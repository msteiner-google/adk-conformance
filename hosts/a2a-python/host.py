"""Bare a2a-python host: the a2a-sdk server with a fixed executor, no ADK.

Control group for the python subject (see hosts/BARE_HOSTS.md). Serves one
fixture over A2A v1: the card at /.well-known/agent-card.json, JSON-RPC at /
and HTTP+JSON at /rest, all on --a2a-port.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any

import uvicorn
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes, create_rest_routes
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
)
from google.protobuf import json_format
from google.protobuf import struct_pb2
from starlette.applications import Starlette

HOST = "127.0.0.1"
MODEL = "gemini-2.5-flash"
API_KEY = "scripted-key"
MAX_ROUNDS = 5
LONG_RUNNING = {"request_approval"}
FIXTURES = {
    "hello_agent": ("Hello-world fixture agent.",
                    "You are a friendly assistant. Answer in one short sentence."),
    "tools_agent": ("Agent with the shared tool library.",
                    "AGENT=tools_agent. Use the tools to answer."),
}


def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
  """The shared tool library (fixtures/FIXTURES.md), minus the long-running tool."""
  if name == "add":
    return {"result": int(args.get("a", 0)) + int(args.get("b", 0))}
  if name == "get_weather":
    return {"city": args.get("city"), "forecast": "sunny", "temperature_c": 21}
  if name == "set_state":
    return {"status": "ok"}
  if name == "fail_always":
    return {"error": "tool failed on purpose"}
  return {"error": f"unknown tool {name}"}


class ModelError(Exception):
  pass


def call_model(base_url: str, instruction: str, contents: list[dict[str, Any]]) -> dict[str, Any]:
  url = f"{base_url.rstrip('/')}/v1beta/models/{MODEL}:generateContent"
  body = {"systemInstruction": {"parts": [{"text": instruction}]}, "contents": contents}
  req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                               headers={"Content-Type": "application/json",
                                        "x-goog-api-key": API_KEY})
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:
      data = json.loads(resp.read())
  except urllib.error.HTTPError as e:
    raise ModelError(f"model error {e.code}: {e.read().decode(errors='replace')}") from e
  candidates = data.get("candidates") or [{}]
  return (candidates[0].get("content") or {"role": "model", "parts": []})


def _data_part(data: dict[str, Any], metadata: dict[str, Any]) -> Part:
  value = struct_pb2.Value()
  json_format.ParseDict(data, value)
  meta = struct_pb2.Struct()
  meta.update(metadata)
  return Part(data=value, metadata=meta)


class BareExecutor(AgentExecutor):
  """Fixed executor: one scripted-model call per step (hosts/BARE_HOSTS.md)."""

  def __init__(self, app: str, model_base_url: str):
    self.instruction = FIXTURES[app][1]
    self.model_base_url = model_base_url
    self.contents: dict[str, list[dict[str, Any]]] = {}

  def _append_user(self, context_id: str, message: Message) -> None:
    history = self.contents.setdefault(context_id, [])
    parts = []
    for part in json_format.MessageToDict(message).get("parts") or []:
      if "text" in part:
        parts.append({"text": part["text"]})
      elif "data" in part and (part.get("metadata") or {}).get("adk_type") == "function_response":
        parts.append({"functionResponse": part["data"]})
    if parts:
      history.append({"role": "user", "parts": parts})

  async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
    message, task_id, context_id = context.message, context.task_id, context.context_id
    if not message or not task_id or not context_id:
      return
    if context.current_task is None:
      await event_queue.enqueue_event(Task(
          id=task_id, context_id=context_id,
          status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED), history=[message]))
    updater = TaskUpdater(event_queue=event_queue, task_id=task_id, context_id=context_id)
    await updater.start_work()
    self._append_user(context_id, message)
    history = self.contents[context_id]
    for _ in range(MAX_ROUNDS):
      try:
        content = await asyncio.to_thread(call_model, self.model_base_url, self.instruction, history)
      except ModelError as e:
        await updater.failed(updater.new_agent_message(parts=[Part(text=str(e))]))
        return
      parts = content.get("parts") or []
      history.append({"role": "model", "parts": parts})
      calls = [p["functionCall"] for p in parts if "functionCall" in p]
      if not calls:
        text = "".join(p.get("text", "") for p in parts)
        await updater.update_status(TaskState.TASK_STATE_WORKING,
                                    message=updater.new_agent_message(parts=[Part(text=text)]))
        await updater.add_artifact(parts=[Part(text=text)], last_chunk=True)
        await updater.complete()
        return
      pending = [c for c in calls if c.get("name") in LONG_RUNNING]
      if pending:
        call = pending[0]
        data = {"id": call.get("id", ""), "name": call["name"], "args": call.get("args") or {}}
        part = _data_part(data, {"adk_type": "function_call", "adk_is_long_running": True})
        await updater.requires_input(updater.new_agent_message(parts=[part]))
        return
      responses = [{"functionResponse": {"id": c.get("id", ""), "name": c["name"],
                                         "response": run_tool(c["name"], c.get("args") or {})}}
                   for c in calls]
      history.append({"role": "user", "parts": responses})
    await updater.failed(updater.new_agent_message(parts=[Part(text="too many tool rounds")]))

  async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
    updater = TaskUpdater(event_queue=event_queue, task_id=context.task_id or "",
                          context_id=context.context_id or "")
    await updater.cancel()


def build_app(app_name: str, port: int, model_base_url: str) -> Starlette:
  description, _ = FIXTURES[app_name]
  base = f"http://{HOST}:{port}"
  card = AgentCard(
      name=app_name,
      description=description,
      version="1.0.0",
      capabilities=AgentCapabilities(streaming=True, push_notifications=False),
      default_input_modes=["text/plain"],
      default_output_modes=["text/plain"],
      skills=[AgentSkill(id=app_name, name=app_name, description=description,
                         tags=["adk-monitor"])],
      supported_interfaces=[
          AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0", url=f"{base}/"),
          AgentInterface(protocol_binding="HTTP+JSON", protocol_version="1.0", url=f"{base}/rest"),
      ],
  )
  handler = DefaultRequestHandler(agent_executor=BareExecutor(app_name, model_base_url),
                                  task_store=InMemoryTaskStore(), agent_card=card)
  routes = [
      *create_agent_card_routes(agent_card=card),
      *create_rest_routes(request_handler=handler, path_prefix="/rest"),
      *create_jsonrpc_routes(request_handler=handler, rpc_url="/"),
  ]
  return Starlette(routes=routes)


def main(argv: list[str]) -> int:
  logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                      format="%(asctime)s %(levelname)s %(name)s: %(message)s")
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--a2a-port", type=int, required=True)
  parser.add_argument("--a2a-app", required=True, choices=sorted(FIXTURES))
  parser.add_argument("--model-base-url", required=True)
  args, unknown = parser.parse_known_args(argv)
  if unknown:
    logging.info("ignoring flags: %s", unknown)
  app = build_app(args.a2a_app, args.a2a_port, args.model_base_url)
  logging.info("bare a2a-python host: %s on http://%s:%d/", args.a2a_app, HOST, args.a2a_port)
  server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=args.a2a_port, log_level="info"))
  server.run()
  return 0 if server.started else 1


if __name__ == "__main__":
  sys.exit(main(sys.argv[1:]))
