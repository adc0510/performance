"""Shared setup for the case scripts.

Every case calls :func:`setup` before its traced entry point runs, so each
trace lands at ``cases/out/<case>/<OUTPUT_FILE>.json`` where ``run_all.py``
can check it. Output/sending config only; anything a case is *about* (CAPTURE_IO,
SKIP_FUNCTIONS, …) is set by the case itself.
"""

from pathlib import Path

import perf

OUT = Path(__file__).parent / "out"


def setup(case_file: str, **config) -> None:
    perf.OUTPUT_DIR = str(OUT / Path(case_file).stem)
    perf.OUTPUT_FILE = "main"
    perf.ENDPOINT_URL = None
    perf.HOT_SPOTS = 0
    for key, value in config.items():
        setattr(perf, key, value)
