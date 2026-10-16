"""Tests for the attribution decision procedure."""

import unittest

from attribution import engine


def _t(subject, status, failures=(), kind="adk"):
  return {"scenario": "s", "subject": subject, "status": status, "failures": list(failures),
          "behaviors": ["A2A-SRV-001"], "topology": {"kind": kind}, "steps": []}


class DecideTest(unittest.TestCase):

  def test_table(self):
    self.assertEqual(engine.decide("fail", "fail", "fail"), "a2a")
    self.assertEqual(engine.decide("fail", "pass", "pass"), "stale-pin")
    self.assertEqual(engine.decide("pass", "pass", "fail"), "adk")
    self.assertEqual(engine.decide("pass", "pass", "pass"), "stale-pin")
    self.assertEqual(engine.decide("pass", "fail", "fail"), "unknown")


class FailureTest(unittest.TestCase):

  def _ev(self, bare_status, bare_failures=()):
    base = {"s": {
        "go": _t("go", "fail", ["cancel again :: error_code"]),
        "a2a-go": _t("a2a-go", bare_status, bare_failures, kind="bare"),
        "python": _t("python", "pass"), "rust": _t("rust", "pass"),
    }}
    versions = {"go": {"pinned": "v2.6.0", "latest": "v2.6.0"}}
    return engine.Evidence(base, versions=versions)

  def test_control_passes_points_at_adk(self):
    ev = self._ev("pass")
    r = engine.attribute_failure(ev, "s", "go", ev.get("s", "go"))
    self.assertEqual(r["attribution"], "adk-go")
    self.assertEqual(r["report_to"], "google/adk-go")
    self.assertEqual(r["confidence"], "high")

  def test_control_fails_same_check_points_at_a2a_sdk(self):
    ev = self._ev("fail", ["cancel again :: error_code"])
    r = engine.attribute_failure(ev, "s", "go", ev.get("s", "go"))
    self.assertEqual(r["attribution"], "a2a-go")
    self.assertEqual(r["report_to"], "a2aproject/a2a-go")

  def test_control_fails_other_check_is_unknown(self):
    ev = self._ev("fail", ["send :: state"])
    r = engine.attribute_failure(ev, "s", "go", ev.get("s", "go"))
    self.assertEqual(r["attribution"], "unknown")

  def test_rust_goes_to_adk_rust(self):
    base = {"s": {"rust": _t("rust", "fail", ["x :: y"]), "a2a-rs": _t("a2a-rs", "pass", kind="bare"),
                  "python": _t("python", "pass"), "go": _t("go", "pass")}}
    ev = engine.Evidence(base)
    r = engine.attribute_failure(ev, "s", "rust", ev.get("s", "rust"))
    self.assertEqual(r["attribution"], "adk-rust")
    self.assertEqual(r["report_to"], "zavora-ai/adk-rust")


class PairMatrixTest(unittest.TestCase):

  def _grid(self, failing):
    clients, servers = ["go", "python"], ["go", "python", "a2a-go"]
    cells = {c: {s: {"status": "fail" if (c, s) in failing else "pass", "failures": []}
                 for s in servers} for c in clients}
    return {"clients": clients, "servers": servers, "cells": cells}

  def test_row_column_cell(self):
    patterns = engine.pair_matrix(self._grid({("python", "go"), ("python", "python"),
                                              ("python", "a2a-go")}))
    self.assertEqual([(p["pattern"], p["attribution"]) for p in patterns], [("row", "adk-python")])
    patterns = engine.pair_matrix(self._grid({("go", "a2a-go"), ("python", "a2a-go")}))
    self.assertEqual([(p["pattern"], p["attribution"]) for p in patterns], [("column", "a2a-go")])
    patterns = engine.pair_matrix(self._grid({("python", "go")}))
    self.assertEqual([(p["pattern"], p["attribution"]) for p in patterns], [("cell", "pair")])


if __name__ == "__main__":
  unittest.main()
