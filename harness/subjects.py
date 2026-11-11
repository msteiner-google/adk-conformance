"""Subjects the Python tools act on. Mirrors tools/bazel/subjects.bzl.

PAUSED lists subjects whose hosts stay in the tree but are not built, tested,
bumped or attributed (see "Paused subjects" in README.md). Keep it equal to
PAUSED_SUBJECTS in tools/bazel/subjects.bzl; //harness:subjects_test checks.
"""

from __future__ import annotations

PAUSED = frozenset({"rust", "a2a-rs"})


def active(names):
  """The names (subjects, or dict keys) that are not paused, in order."""
  return [n for n in names if n not in PAUSED]


def involves_paused(transcript) -> bool:
  """Whether a transcript ran a paused subject (as the subject, client or server)."""
  topology = transcript.get("topology") or {}
  names = {transcript.get("subject"), topology.get("client"), topology.get("server")}
  return bool(names & PAUSED)
