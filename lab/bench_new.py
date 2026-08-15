"""New pipeline: trace workload.run() with flat records + WAL-backed queue +
spill-on-capture + dedup/checkpointing. Prints wall time, peak RSS, IO
(WAL + spill combined), and pipeline stats.
"""

import json
import resource
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent))

from bench_common import clean_pipeline_files  # noqa: E402
from pipeline import PipelineConfig, make_pipeline  # noqa: E402
from workload import Service, run  # noqa: E402

LAB_DIR = Path(__file__).parent
WAL_PATH = LAB_DIR / "lab_wal.jsonl"
SPILL_DIR = LAB_DIR / "lab_spill"


def main() -> None:
    clean_pipeline_files(WAL_PATH, SPILL_DIR)

    cfg = PipelineConfig(
        max_size_capture=4096,
        checkpoint_every=10,
        wal_path=WAL_PATH,
        spill_dir=SPILL_DIR,
        queue_maxsize_bytes=2_000_000,
    )
    trace, wal, spill, _sender, _shipper = make_pipeline(cfg)

    # Mirror perf.trace's auto-instrumentation (which wraps every project
    # method) by explicitly wrapping Service's methods too -- otherwise this
    # benchmark would only trace one top-level call and never exercise the
    # per-method self-dedup path the baseline hits on every call.
    Service.handle = trace(Service.handle)
    Service.summarize = trace(Service.summarize)
    Service.bulk_export = trace(Service.bulk_export)

    captured = {}
    traced_run = trace(run, on_root=lambda tid, cache: captured.update(trace_id=tid, cache=cache))
    start = perf_counter()
    traced_run(calls=3000)
    wal.close()
    instances = captured["cache"]  # the single InstanceCache used for this one trace
    elapsed = perf_counter() - start  # includes final drain/close, same as baseline's final write

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    io_bytes = wal.total_bytes_on_disk() + spill.bytes_written
    io_writes = wal.write_calls + spill.write_calls
    print(
        json.dumps(
            {
                "label": "new pipeline (flat + WAL + spill + dedup)",
                "wall_s": elapsed,
                "peak_rss_mb": peak_kb / 1024,
                "io_bytes": io_bytes,
                "io_writes": io_writes,
                "records_written": wal.record_count,
                "queue_overflows": wal.overflow_count,
                "wal_segments_on_disk": len(wal.segments_on_disk()),  # no shipper here -- nothing reclaims these
                "spill_files": spill._n,
                "spill_bytes": spill.bytes_written,
                "instance_full_snapshots": instances.full_snapshots,
                "instance_diffs": instances.diffs,
            }
        )
    )


if __name__ == "__main__":
    main()
