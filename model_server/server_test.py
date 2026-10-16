"""Tests for the scripted model server."""

import json
import unittest
import urllib.error
import urllib.request

from model_server.server import ModelScript, ScriptedModelServer


def _post(url, body):
  req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
  try:
    with urllib.request.urlopen(req) as resp:
      return resp.status, resp.read().decode()
  except urllib.error.HTTPError as e:
    return e.code, e.read().decode()


class ServerTest(unittest.TestCase):

  def setUp(self):
    script = ModelScript([
        {"when": {"turn": 1}, "reply": {"text": "Hi!", "chunks": ["H", "i!"]}},
        {"when": {"last_user_text_contains": "tool"},
         "reply": {"function_calls": [{"name": "add", "args": {"a": 1}}]}},
        {"when": {"turn": 3}, "reply": {"error": {"code": 429, "message": "slow down"}}},
    ])
    self.server = ScriptedModelServer(script)
    self.url = self.server.start()
    self.body = {"contents": [{"role": "user", "parts": [{"text": "use a tool"}]}]}

  def tearDown(self):
    self.server.stop()

  def test_script_sequence_and_recording(self):
    status, text = _post(f"{self.url}/v1beta/models/m:streamGenerateContent?alt=sse", self.body)
    self.assertEqual(status, 200)
    chunks = [json.loads(l[6:]) for l in text.split("\r\n") if l.startswith("data: ")]
    self.assertEqual([c["candidates"][0]["content"]["parts"][0]["text"] for c in chunks], ["H", "i!"])
    self.assertEqual(chunks[-1]["candidates"][0]["finishReason"], "STOP")

    status, text = _post(f"{self.url}/v1beta/models/m:generateContent", self.body)
    self.assertEqual(status, 200)
    part = json.loads(text)["candidates"][0]["content"]["parts"][0]
    self.assertEqual(part["functionCall"], {"name": "add", "args": {"a": 1}})

    self.body["contents"][0]["parts"][0]["text"] = "no match"
    status, text = _post(f"{self.url}/v1beta/models/m:generateContent", self.body)
    self.assertEqual(status, 429)
    self.assertEqual(json.loads(text)["error"]["status"], "RESOURCE_EXHAUSTED")

    status, _ = _post(f"{self.url}/v1beta/models/m:generateContent", self.body)
    self.assertEqual(status, 500)  # No rule for turn 4.

    recorded = self.server.recorded_requests()
    self.assertEqual([r["turn"] for r in recorded], [1, 2, 3, 4])
    self.assertEqual(recorded[0]["query"], {"alt": "sse"})
    self.assertEqual(recorded[1]["method"], "generateContent")


class AgentMatchingTest(unittest.TestCase):

  def test_agent_call_and_function_response(self):
    script = ModelScript([
        {"when": {"agent": "a", "has_function_response": "add"}, "reply": {"text": "done"}},
        {"when": {"agent": "a", "call": 1},
         "reply": {"function_calls": [{"name": "add", "args": {"a": 1}, "id": "c1"}]}},
        {"when": {"agent": "b"}, "reply": {"empty": True}},
    ])
    server = ScriptedModelServer(script)
    url = server.start()
    try:
      sys_a = {"systemInstruction": {"parts": [{"text": "AGENT=a. Do it."}]}}
      _, text = _post(f"{url}/v1beta/models/m:generateContent",
                      {**sys_a, "contents": [{"role": "user", "parts": [{"text": "hi"}]}]})
      call = json.loads(text)["candidates"][0]["content"]["parts"][0]["functionCall"]
      self.assertEqual(call, {"name": "add", "args": {"a": 1}, "id": "c1"})

      _, text = _post(f"{url}/v1beta/models/m:generateContent", {**sys_a, "contents": [
          {"role": "user", "parts": [{"functionResponse": {"name": "add", "response": {"result": 1}}}]}]})
      self.assertEqual(json.loads(text)["candidates"][0]["content"]["parts"][0]["text"], "done")

      # Marker in contents (instruction sent as a user turn) also identifies the agent.
      _, text = _post(f"{url}/v1beta/models/m:generateContent",
                      {"contents": [{"role": "user", "parts": [{"text": "AGENT=b. x"}]}]})
      self.assertEqual(json.loads(text)["candidates"][0]["content"]["parts"], [])

      recorded = server.recorded_requests()
      self.assertEqual([(r["agent"], r["agent_call"]) for r in recorded], [("a", 1), ("a", 2), ("b", 1)])
    finally:
      server.stop()


if __name__ == "__main__":
  unittest.main()
