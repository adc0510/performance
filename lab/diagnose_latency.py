"""Isolates where perf.trace's wall-time overhead actually comes from, by
running the same workload under progressively more of the tracing machinery:

  1. untraced                          -- reference point
  2. sys.setprofile hook only          -- CAPTURE_IO off: patching + the
                                           profiler firing on every call/return,
                                           but no value capture at all
  3. + capture, no dedup, no budget    -- CAPTURE_IO on, MAX_SIZE_CAPTURE=None
                                           (today's original default)
  4. + budget (MAX_SIZE_CAPTURE+SPILL) -- adds the size-bounded conversion path
  5. + DEDUP_INSTANCES                 -- the full "new" configuration

Each stage adds exactly one thing on top of the previous, so the wall-time
delta between consecutive rows attributes cost to that one thing.
"""

import json
import shutil
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import perf  # noqa: E402
from workload import run  # noqa: E402

OUT_DIR = Path(__file__).parent / "diag_out"
SPILL_DIR = Path(__file__).parent / "diag_spill"


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def time_it(label: str, calls: int = 3000) -> dict:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    shutil.rmtree(SPILL_DIR, ignore_errors=True)
    sys.stdout = _Null()
    traced = perf.trace(run)
    start = perf_counter()
    traced(calls=calls)
    elapsed = perf_counter() - start
    sys.stdout = sys.__stdout__
    return {"label": label, "wall_s": elapsed}


def main() -> None:
    results = []

    # 1. untraced
    start = perf_counter()
    run(calls=3000)
    results.append({"label": "1. untraced", "wall_s": perf_counter() - start})

    perf.OUTPUT_DIR = None
    perf.OUTPUT_FILE = None
    perf.ENDPOINT_URL = None
    perf.HOT_SPOTS = 0

    # 2. setprofile hook + patching only, no capture at all
    perf.CAPTURE_IO = False
    perf.MAX_SIZE_CAPTURE = None
    perf.SPILL_DIR = None
    perf.DEDUP_INSTANCES = False
    results.append(time_it("2. + sys.setprofile hook (CAPTURE_IO off)"))

    # 3. + capture, unbounded (today's original default shape)
    perf.CAPTURE_IO = True
    results.append(time_it("3. + capture, unbounded (MAX_SIZE_CAPTURE=None)"))

    # 4. + size budget / spill
    perf.MAX_SIZE_CAPTURE = 4096
    perf.SPILL_DIR = str(SPILL_DIR)
    results.append(time_it("4. + MAX_SIZE_CAPTURE + SPILL_DIR"))

    # 5. + dedup
    perf.DEDUP_INSTANCES = True
    perf.DEDUP_CHECKPOINT_EVERY = 10
    results.append(time_it("5. + DEDUP_INSTANCES (= 'new')"))

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    shutil.rmtree(SPILL_DIR, ignore_errors=True)

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
