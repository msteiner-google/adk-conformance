"""Tiny path language for assertions: ``final.content.parts[0].text``.

Supports dotted keys, integer indices (negative too) and ``len(...)``-free
lookups. Missing keys raise ``KeyError`` so assertion failures say what was
absent rather than comparing against ``None``.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(-?\d+)\]")


def parse(path: str) -> list[str | int]:
  tokens: list[str | int] = []
  pos = 0
  for match in _TOKEN_RE.finditer(path):
    if match.start() != pos and path[pos:match.start()] != ".":
      raise ValueError(f"bad path {path!r} near {path[pos:]!r}")
    key, index = match.groups()
    tokens.append(int(index) if index is not None else key)
    pos = match.end()
  if pos != len(path):
    raise ValueError(f"bad path {path!r} near {path[pos:]!r}")
  return tokens


def lookup(data: Any, path: str) -> Any:
  """Returns the value at ``path`` or raises ``KeyError`` with context."""
  current = data
  walked = []
  for token in parse(path):
    walked.append(token)
    try:
      if isinstance(token, int):
        current = current[token]
      elif isinstance(current, dict):
        current = current[token]
      else:
        raise KeyError(token)
    except (KeyError, IndexError, TypeError) as e:
      raise KeyError(f"{path}: nothing at {_render(walked)}") from e
  return current


def _render(tokens: list[str | int]) -> str:
  out = ""
  for token in tokens:
    out += f"[{token}]" if isinstance(token, int) else (f".{token}" if out else token)
  return out
