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
