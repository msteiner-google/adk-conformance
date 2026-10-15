"""Tests for SSE parsing, which must tolerate every SDK's framing."""

import unittest

from harness import adk_client


class ParseSseTest(unittest.TestCase):

  def test_both_framings(self):
    body = 'data: {"a": 1}\n\ndata:{"b": 2}\r\n\r\n'
    self.assertEqual(adk_client.parse_sse(body), [{"a": 1}, {"b": 2}])

  def test_non_json_kept(self):
    self.assertEqual(adk_client.parse_sse("data: oops\n\n"), ["oops"])

  def test_final_event_skips_partials(self):
    events = [{"content": {"parts": [{"text": "x"}]}, "partial": True},
              {"content": {"parts": [{"text": "full"}]}}]
    self.assertEqual(adk_client.final_event(events)["content"]["parts"][0]["text"], "full")


if __name__ == "__main__":
  unittest.main()
