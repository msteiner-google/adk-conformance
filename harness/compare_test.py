"""Tests for the differential oracle."""

import unittest

from harness import compare


def _transcript(subject, text, status=200, author="a"):
  event = {"id": f"e-{subject}", "invocationId": f"i-{subject}", "author": author,
           "content": {"role": "model", "parts": [{"text": text}]}}
  return {
      "scenario": "s1", "subject": subject, "status": "pass", "area": "x", "behaviors": [],
      "steps": [{"name": "turn", "kind": "adk.run_sse", "exchanges": [
          {"response": {"status": status, "content_type": "text/event-stream", "events": [event]}}]}],
      "llm": {"requests": []},
  }


class CompareTest(unittest.TestCase):

  def test_majority_outlier_ignores_volatile_ids(self):
    transcripts = {"s1": {"py": _transcript("py", "hi"), "go": _transcript("go", "hi"),
                          "rs": _transcript("rs", "HI")}}
    report = compare.build_report(transcripts)
    steps = [f for f in report["findings"] if f["aspect"] == "step:turn"]
    self.assertEqual(len(steps), 1)
    self.assertEqual(steps[0]["outliers"], ["rs"])
    self.assertEqual(steps[0]["kind"], "divergence")
    self.assertIn("events[0].parts[0].text", [d["path"] for d in steps[0]["details"]])

  def test_identical_behavior_has_no_findings(self):
    transcripts = {"s1": {"py": _transcript("py", "hi"), "go": _transcript("go", "hi")}}
    report = compare.build_report(transcripts)
    self.assertFalse([f for f in report["findings"] if f["aspect"].startswith("step:")])

  def test_not_implemented(self):
    transcripts = {"s1": {"py": _transcript("py", "hi"), "go": _transcript("go", "hi", status=404)}}
    report = compare.build_report(transcripts)
    self.assertEqual(report["matrix"]["s1"]["subjects"]["go"]["not_implemented"], ["turn"])
    self.assertIn("🚫", compare.render_markdown(report))


if __name__ == "__main__":
  unittest.main()
