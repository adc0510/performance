"""Verification step 1 from the plan: with the new opt-in flags on, peak RAM
for the real perf.trace on the lab's own workload should track close to
lab/bench_new.py's numbers (~64MB), not lab/bench_baseline.py's (~2.6GB).
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
from workload import Service, run  # noqa: E402

OUT_DIR = Path(__file__).parent / "verify_prod_out"
SPILL_DIR = Path(__file__).parent / "verify_prod_spill"
shutil.rmtree(OUT_DIR, ignore_errors=True)
shutil.rmtree(SPILL_DIR, ignore_errors=True)

perf.OUTPUT_DIR = str(OUT_DIR)
perf.OUTPUT_FILE = "trace.json"
perf.ENDPOINT_URL = None
perf.HOT_SPOTS = 0
perf.CAPTURE_IO = True
perf.MAX_SIZE_CAPTURE = 4096
perf.SPILL_DIR = str(SPILL_DIR)
perf.DEDUP_INSTANCES = True
perf.DEDUP_CHECKPOINT_EVERY = 10


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def main() -> None:
    sys.stdout = _Null()
    traced = perf.trace(run)
    start = perf_counter()
    traced(calls=3000)
    elapsed = perf_counter() - start
    sys.stdout = sys.__stdout__

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out_file = OUT_DIR / "trace.json"
    output_json_bytes = out_file.stat().st_size if out_file.exists() else 0
    spill_files = list(SPILL_DIR.glob("*.json")) if SPILL_DIR.exists() else []
    spill_bytes = sum(p.stat().st_size for p in spill_files)

    print(
        json.dumps(
            {
                "label": "new (perf.trace, SPILL_DIR + DEDUP_INSTANCES on)",
                "wall_s": elapsed,
                "peak_rss_mb": peak_kb / 1024,
                "io_bytes": output_json_bytes + spill_bytes,
                "io_writes": 1 + len(spill_files),  # one OUTPUT_FILE write + one per spilled value
                "output_json_bytes": output_json_bytes,
                "spill_files": len(spill_files),
                "spill_bytes": spill_bytes,
            }
        )
    )


if __name__ == "__main__":
    main()
