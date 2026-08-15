"""Verification step 2 from the plan: default behavior (SPILL_DIR=None,
DEDUP_INSTANCES=False) must be identical to before this change -- same
captured JSON shape, same console output.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import perf  # noqa: E402

OUT = Path(__file__).parent / "verify_out.json"
OUT.unlink(missing_ok=True)

perf.OUTPUT_DIR = None
perf.OUTPUT_FILE = str(OUT)
perf.ENDPOINT_URL = None
perf.HOT_SPOTS = 0
perf.CAPTURE_IO = True
perf.MAX_SIZE_CAPTURE = 500  # small, to exercise the drop path
# SPILL_DIR and DEDUP_INSTANCES left at their defaults (None / False)


@perf.trace
def small(a: dict, big: str) -> dict:
    return {"echo": a, "n": len(big)}


small({"x": 1, "y": [1, 2, 3]}, "x" * 10_000)

tree = json.loads(OUT.read_text())
print(json.dumps({"args": tree["args"], "kwargs": tree["kwargs"], "result": tree["result"]}, indent=2)[:500])
OUT.unlink()
