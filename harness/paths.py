"""Tiny path language for assertions: ``final.content.parts[0].text``.

Supports dotted keys, integer indices (negative too) and ``[*]``, which maps
the rest of the path over a list and returns the values that exist (items
lacking the rest of the path are skipped). Missing keys raise ``KeyError`` so
assertion failures say what was absent rather than comparing against ``None``.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(-?\d+|\*)\]")
STAR = "*"


def parse(path: str) -> list[str | int]:
  tokens: list[str | int] = []
  pos = 0
  for match in _TOKEN_RE.finditer(path):
    if match.start() != pos and path[pos:match.start()] != ".":
      raise ValueError(f"bad path {path!r} near {path[pos:]!r}")
    key, index = match.groups()
    if index == STAR:
      tokens.append(STAR)
    else:
      tokens.append(int(index) if index is not None else key)
    pos = match.end()
  if pos != len(path):
    raise ValueError(f"bad path {path!r} near {path[pos:]!r}")
  return tokens


def lookup(data: Any, path: str) -> Any:
  """Returns the value at ``path`` or raises ``KeyError`` with context."""
  return _lookup(data, parse(path), path)


def _lookup(data: Any, tokens: list[str | int], path: str) -> Any:
  current = data
  walked: list[str | int] = []
  for i, token in enumerate(tokens):
    if token == STAR:
      if not isinstance(current, list):
        raise KeyError(f"{path}: [*] on a non-list at {_render(walked)}")
      out = []
      for item in current:
        try:
          out.append(_lookup(item, tokens[i + 1:], path))
        except KeyError:
          continue
      return out
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
