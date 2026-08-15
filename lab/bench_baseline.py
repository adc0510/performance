"""Baseline: trace workload.run() with the current perf.trace (nested tree,
CAPTURE_IO on, MAX_SIZE_CAPTURE unset -- today's default, OUTPUT_DIR set to
mirror real usage where the trace is actually persisted). Prints wall time,
peak RSS, and the resulting IO (one big write at the end of the trace).
"""

import json
import resource
import shutil
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import perf  # noqa: E402
from workload import run  # noqa: E402

OUT_DIR = Path(__file__).parent / "lab_baseline_out"

perf.OUTPUT_DIR = str(OUT_DIR)
perf.OUTPUT_FILE = "trace.json"
perf.ENDPOINT_URL = None
perf.HOT_SPOTS = 0
perf.CAPTURE_IO = True
perf.MAX_SIZE_CAPTURE = None  # today's default: unlimited


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def main() -> None:
    shutil.rmtree(OUT_DIR, ignore_errors=True)
    sys.stdout = _Null()  # perf.trace prints the whole tree; not interesting here
    traced = perf.trace(run)
    start = perf_counter()
    traced(calls=3000)
    elapsed = perf_counter() - start
    sys.stdout = sys.__stdout__

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out_file = OUT_DIR / "trace.json"
    io_bytes = out_file.stat().st_size if out_file.exists() else 0
    print(
        json.dumps(
            {
                "label": "baseline (perf.trace)",
                "wall_s": elapsed,
                "peak_rss_mb": peak_kb / 1024,
                "io_bytes": io_bytes,
                "io_writes": 1 if io_bytes else 0,  # one big write() at the end of the trace
            }
        )
    )


if __name__ == "__main__":
    main()
