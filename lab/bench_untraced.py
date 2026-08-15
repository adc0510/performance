"""No tracing at all -- the reference point for time/RAM overhead."""

import json
import resource
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent))

from workload import run  # noqa: E402


def main() -> None:
    start = perf_counter()
    run(calls=3000)
    elapsed = perf_counter() - start
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "label": "untraced",
                "wall_s": elapsed,
                "peak_rss_mb": peak_kb / 1024,
                "io_bytes": 0,
                "io_writes": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
