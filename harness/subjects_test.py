"""harness/subjects.py, tools/bazel/subjects.bzl and .bazelignore agree."""

import os
import re
import sys
import unittest

from harness import subjects

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BZL, IGNORE = (sys.argv[1:3] if len(sys.argv) >= 3 and sys.argv[1].endswith(".bzl") else
               (os.path.join(ROOT, "tools/bazel/subjects.bzl"), os.path.join(ROOT, ".bazelignore")))


def _dict_block(text: str, name: str) -> str:
  match = re.search(rf"^{name} = \{{\n(.*?)^\}}", text, re.M | re.S)
  return match.group(1) if match else ""


class SubjectsTest(unittest.TestCase):

  def setUp(self):
    with open(BZL, encoding="utf-8") as f:
      self.bzl = f.read()
    with open(IGNORE, encoding="utf-8") as f:
      self.ignored = {line.strip().rstrip("/") for line in f if line.strip()}

  def test_paused_names_match(self):
    paused = set(re.findall(r'^\s+"([^"]+)": struct', _dict_block(self.bzl, "PAUSED_SUBJECTS"), re.M))
    self.assertEqual(paused, set(subjects.PAUSED))

  def test_paused_hosts_are_ignored_and_active_hosts_are_not(self):
    for name, block in (("PAUSED_SUBJECTS", True), ("SUBJECTS", False), ("CONTROL_SUBJECTS", False)):
      for package in re.findall(r'host = "//([^:"]+):', _dict_block(self.bzl, name)):
        self.assertEqual(package in self.ignored, block, f"{name}: //{package} vs .bazelignore")

  def test_active_filters(self):
    self.assertEqual(subjects.active(["python", "rust", "go"]), ["python", "go"])
    self.assertTrue(subjects.involves_paused({"subject": "rust"}))
    self.assertTrue(subjects.involves_paused(
        {"subject": "go-to-a2a-rs", "topology": {"client": "go", "server": "a2a-rs"}}))
    self.assertFalse(subjects.involves_paused(
        {"subject": "go-to-java", "topology": {"client": "go", "server": "java"}}))


if __name__ == "__main__":
  unittest.main(argv=sys.argv[:1])
