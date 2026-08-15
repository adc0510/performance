"""Does tracing overhead scale with the number of traced function calls
(meaning it's paid throughout, on every call), or stay flat regardless of
call count (meaning it's a one-time cost paid only at the end, e.g. when the
trace is printed/written/sent)?

Runs the SAME per-call cost at 3 different call counts. If overhead is
per-call, wall time should scale roughly linearly with call count. If it's a
fixed end-of-trace cost, wall time should stay roughly flat.
"""

import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import perf  # noqa: E402
from workload import run  # noqa: E402


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def time_it(calls: int) -> dict:
    perf.OUTPUT_DIR = None
    perf.OUTPUT_FILE = None
    perf.ENDPOINT_URL = None
    perf.HOT_SPOTS = 0
    perf.CAPTURE_IO = False  # isolate the setprofile hook itself, no capture

    sys.stdout = _Null()
    traced = perf.trace(run)
    start = perf_counter()
    traced(calls=calls)
    elapsed = perf_counter() - start
    sys.stdout = sys.__stdout__

    return {"calls": calls, "wall_s": elapsed}


def main() -> None:
    # untraced reference at each scale, for comparison
    results = []
    for calls in (500, 1500, 3000):
        start = perf_counter()
        run(calls=calls)
        untraced = perf_counter() - start
        traced = time_it(calls)
        results.append(
            {
                "calls": calls,
                "untraced_wall_s": untraced,
                "traced_wall_s": traced["wall_s"],
                "overhead_s": traced["wall_s"] - untraced,
            }
        )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
