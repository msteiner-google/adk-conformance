"""Test entry point: runs one scenario against one subject (see runner.py)."""

import sys

from harness import runner

if __name__ == "__main__":
  sys.exit(runner.main())
