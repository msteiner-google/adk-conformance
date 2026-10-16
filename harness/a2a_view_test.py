"""Tests for the A2A normalization and derived views."""

import unittest

from harness import a2a_client, a2a_view


class CanonicalTest(unittest.TestCase):

  def test_v03_task_becomes_v1(self):
    v03 = {"kind": "task", "id": "t1", "contextId": "c1",
           "status": {"state": "input-required",
                      "message": {"kind": "message", "role": "agent",
                                  "parts": [{"kind": "data", "data": {"x": 1}}]}},
           "artifacts": [{"artifactId": "a", "parts": [{"kind": "text", "text": "hi"}]}]}
    event = a2a_view.canonical_event(v03)
    self.assertEqual(event["kind"], "task")
    task = event["task"]
    self.assertEqual(task["status"]["state"], "TASK_STATE_INPUT_REQUIRED")
    self.assertEqual(task["status"]["message"]["role"], "ROLE_AGENT")
    self.assertEqual(task["artifacts"][0]["parts"], [{"text": "hi"}])
    self.assertEqual(a2a_view.enum_style(v03), "v0.3")
    self.assertTrue(a2a_view.has_kind_field(v03))

  def test_v1_wrapper_and_format(self):
    v1 = {"task": {"id": "t", "status": {"state": "TASK_STATE_COMPLETED"}, "artifacts": []}}
    self.assertEqual(a2a_view.canonical_event(v1)["kind"], "task")
    self.assertEqual(a2a_view.enum_style(v1), "v1")
    self.assertFalse(a2a_view.has_kind_field(v1))

  def test_stream_view_reassembles_chunks(self):
    def art(text, append, last):
      return {"jsonrpc": "2.0", "id": 1, "result": {"artifactUpdate": {
          "taskId": "t", "contextId": "c", "append": append, "lastChunk": last,
          "artifact": {"artifactId": "a1", "parts": [{"text": text}]}}}}
    payloads = [
        {"jsonrpc": "2.0", "id": 1, "result": {"task": {"id": "t", "contextId": "c",
                                                         "status": {"state": "TASK_STATE_SUBMITTED"}}}},
        art("Hel", False, False), art("lo", True, True),
        {"jsonrpc": "2.0", "id": 1, "result": {"statusUpdate": {
            "taskId": "t", "contextId": "c", "status": {"state": "TASK_STATE_COMPLETED"}}}},
    ]
    view = a2a_view.stream_view("JSONRPC", 200, "text/event-stream", payloads, True, "data: ")
    self.assertEqual(view["kinds"], ["task", "artifactUpdate", "artifactUpdate", "statusUpdate"])
    self.assertEqual(view["artifact_text"], "Hello")
    self.assertEqual(view["final_state"], "TASK_STATE_COMPLETED")
    self.assertEqual(view["task_id"], "t")

  def test_text_locations(self):
    view = a2a_view.response_view("JSONRPC", 200, "application/json", {}, {"task": {
        "id": "t", "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [{"parts": [{"text": "x"}]}],
        "history": [{"role": "ROLE_AGENT", "parts": [{"text": "x"}]}]}}, None)
    self.assertEqual(view["text_locations"], ["artifact", "history"])

  def test_interfaces_prefer_v1(self):
    card = {"supportedInterfaces": [
        {"protocolBinding": "JSONRPC", "protocolVersion": "0.3", "url": "http://h/old"},
        {"protocolBinding": "JSONRPC", "protocolVersion": "1.0", "url": "http://h/new"}]}
    self.assertEqual(a2a_client.interfaces(card), {"JSONRPC": "http://h/new"})


if __name__ == "__main__":
  unittest.main()
