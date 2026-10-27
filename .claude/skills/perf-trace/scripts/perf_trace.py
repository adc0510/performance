#!/usr/bin/env python3
"""Read perf trace files without loading them whole into context. Run with --help for every command.

Standard library only -- runs with any python3 (3.9+), perf need not be installed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # also under `python3 -I`

from ptrace.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
