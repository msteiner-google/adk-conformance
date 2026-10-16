"""Tests for the assertion path language."""

import unittest

from harness import paths


class PathsTest(unittest.TestCase):

  def test_lookup(self):
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    self.assertEqual(paths.lookup(data, "a.b[1].c"), 2)
    self.assertEqual(paths.lookup(data, "a.b[-1].c"), 2)

  def test_missing_key_names_the_gap(self):
    with self.assertRaisesRegex(KeyError, r"nothing at a\.x"):
      paths.lookup({"a": {}}, "a.x.y")

  def test_star_skips_missing(self):
    data = {"events": [{"a": {"x": 1}}, {"b": 2}, {"a": {"x": 3}}]}
    self.assertEqual(paths.lookup(data, "events[*].a.x"), [1, 3])

  def test_bad_path(self):
    with self.assertRaises(ValueError):
      paths.parse("a..b[")


if __name__ == "__main__":
  unittest.main()
