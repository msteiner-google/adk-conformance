"""Tests for expectation operators and known-divergence classification."""

import unittest

from harness import runner


class CheckValueTest(unittest.TestCase):

  def test_operators(self):
    data = {"a": "x1", "b": "x1", "l": [1, 1]}
    cases = [
        ({"contains": "ell"}, "hello", True),
        ({"not_contains": "zz"}, ["a", "b"], True),
        ({"in": [200, 204]}, 204, True),
        ({"ne": 200}, 404, True),
        ({"all_equal": True}, [1, 1], True),
        ({"all_equal": True}, [1, 2], False),
        ({"same_as": "b"}, "x1", True),
        ({"type": "number"}, 1.5, True),
        ({"type": "number"}, True, False),
        ({"len": 2}, [1, 2], True),
        ({"regex": ".+"}, None, False),
        ({"contains": "transfer_to_agent"},
         [{"functionDeclarations": [{"name": "transfer_to_agent"}]}], True),
        ({"contains": 'name is "x"'}, {"parts": [{"text": 'Your name is "x".'}]}, True),
        ({"unknown_op": 1}, {"unknown_op": 1}, True),  # literal dict, not an operator
    ]
    for expected, actual, want in cases:
      with self.subTest(expected=expected):
        self.assertEqual(runner.check_value(expected, actual, data)[0], want)


class ClassifyTest(unittest.TestCase):

  def test_statuses(self):
    entry = {"checks": ["turn :: final.*"], "reason": "r", "expires": "2999-01-01"}
    self.assertEqual(runner.classify([], [])[0], "pass")
    self.assertEqual(runner.classify([], [entry])[0], "xpass")
    self.assertEqual(runner.classify(["turn :: final.author"], [entry])[0], "xfail")
    self.assertEqual(runner.classify(["turn :: status"], [entry])[0], "fail")
    literal = {"checks": ["llm :: requests[0].method"], "expires": "2999-01-01"}
    self.assertEqual(runner.classify(["llm :: requests[0].method"], [literal])[0], "xfail")
    self.assertEqual(runner.classify(["llm :: requests0.method"], [literal])[0], "fail")
    expired = dict(entry, expires="2000-01-01")
    status, _, note = runner.classify(["turn :: final.author"], [expired])
    self.assertEqual((status, bool(note)), ("fail", True))


class A2AParamsTest(unittest.TestCase):

  def test_message_and_unsupported(self):
    msg = runner.ScenarioRun.message({"text": "hi", "context_id": "c1", "message_id": "m1"})
    self.assertEqual(msg, {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "hi"}],
                           "contextId": "c1"})
    self.assertEqual(runner.classify(["send rest :: unsupported"], [])[0], "unsupported")
    self.assertEqual(runner.classify(["send rest :: unsupported", "x :: y"], [])[0], "fail")
    self.assertEqual(runner.pair_id("go", "a2a-python"), "go-to-a2a-python")


class ProxyTest(unittest.TestCase):

  def test_records_and_rewrites_card(self):
    import http.server  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel
    import threading  # pylint: disable=import-outside-toplevel
    from harness import a2a_client, proxy  # pylint: disable=import-outside-toplevel

    class Upstream(http.server.BaseHTTPRequestHandler):
      def log_message(self, *args):
        pass

      def do_GET(self):  # the card, advertising the upstream origin
        body = json.dumps({"supportedInterfaces": [
            {"protocolBinding": "JSONRPC", "protocolVersion": "1.0",
             "url": f"http://127.0.0.1:{self.server.server_address[1]}/"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

      def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"task": {
            "id": "t", "contextId": "ctx-1", "status": {"state": "TASK_STATE_COMPLETED"}}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    rec = proxy.RecordingProxy(f"http://127.0.0.1:{upstream.server_address[1]}")
    base = rec.start()
    try:
      card = a2a_client.http_request("GET", base + a2a_client.CARD_PATH)
      self.assertEqual(a2a_client.interfaces(card.json)["JSONRPC"], base + "/")
      a2a_client.http_request("POST", base + "/", a2a_client.jsonrpc_envelope(
          "SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "hi"}]}}),
          a2a_client.default_headers())
      view = proxy.traffic_view(rec.recorded())
      self.assertEqual(view["card_fetches"], 1)
      self.assertEqual(view["methods"], ["SendMessage"])
      self.assertEqual(view["versions"], ["1.0"])
      self.assertEqual(view["message_texts"], [["hi"]])
      self.assertEqual(view["response_context_ids"], ["ctx-1"])
    finally:
      rec.stop()
      upstream.shutdown()


class NormalizeTest(unittest.TestCase):

  def test_flat_function_call_is_canonicalized_and_reported(self):
    from harness import normalize  # pylint: disable=import-outside-toplevel
    raw = [{"author": "a", "content": {"role": "model", "parts": [{"name": "add", "args": {"a": 1}}]},
            "long_running_tool_ids": []}]
    events = normalize.canonicalize_events(raw)
    self.assertEqual(events[0]["content"]["parts"][0], {"functionCall": {"name": "add", "args": {"a": 1}}})
    self.assertIn("longRunningToolIds", events[0])
    self.assertEqual(normalize.part_shapes(raw), ["flat-call"])
    self.assertEqual(normalize.event_views(events)["function_calls"][0]["name"], "add")


if __name__ == "__main__":
  unittest.main()
