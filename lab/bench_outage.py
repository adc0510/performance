"""Proves fix #1 and #2 actually hold under a real collector outage:

* start tracing normally
* kill the collector mid-trace (simulating a network blip / restart)
* keep tracing while it's down -- local capture must not stall or lose data
* bring the collector back up
* confirm the shipper catches up and every record is eventually confirmed
  received, with zero silent loss
"""

import json
import resource
import sys
import threading
import urllib.request
from pathlib import Path
from time import perf_counter, sleep

sys.path.insert(0, str(Path(__file__).parent))

from bench_common import clean_pipeline_files  # noqa: E402
from collector import start_collector  # noqa: E402
from pipeline import PipelineConfig, make_pipeline  # noqa: E402
from workload import Service, run  # noqa: E402

LAB_DIR = Path(__file__).parent
WAL_PATH = LAB_DIR / "lab_wal_outage.jsonl"
SPILL_DIR = LAB_DIR / "lab_spill_outage"


def main() -> None:
    clean_pipeline_files(WAL_PATH, SPILL_DIR)

    server, store = start_collector()
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    cfg = PipelineConfig(
        max_size_capture=4096,
        checkpoint_every=10,
        wal_path=WAL_PATH,
        spill_dir=SPILL_DIR,
        queue_maxsize_bytes=2_000_000,
        endpoint_url=url,
        tags=["lab"],
        batch_size=50,
    )
    trace, wal, spill, sender, shipper = make_pipeline(cfg)

    Service.handle = trace(Service.handle)
    Service.summarize = trace(Service.summarize)
    Service.bulk_export = trace(Service.bulk_export)

    captured = {}
    traced_run = trace(run, on_root=lambda tid, cache: captured.update(trace_id=tid))

    start = perf_counter()

    # Run the workload with the collector going away partway through.
    result_box = {}

    def run_workload() -> None:
        result_box["total"] = traced_run(calls=3000)

    t = threading.Thread(target=run_workload)
    t.start()

    sleep(0.3)  # let some records ship normally first
    server.shutdown()  # simulate the collector going away mid-trace
    server.server_close()  # actually free the port so we can rebind it below
    print("[bench] collector killed mid-trace", file=sys.stderr)
    sleep(1.0)  # tracing keeps running against a dead collector for a bit
    lag_during_outage = shipper.pending_bytes()  # backlog metric: should be > 0 here

    # Same port (continuity of ENDPOINT_URL) and the SAME store -- a real
    # collector's storage (a DB) survives a process restart; only this lab's
    # toy in-memory TraceStore wouldn't, so we pass it through explicitly to
    # simulate that persistence rather than test an artifact of the toy.
    server2, store2 = start_collector(port=port, store=store)
    print("[bench] collector back up", file=sys.stderr)

    t.join()  # traced_run returned -- trace() already fired off its own retrying "end" send
    wal.close()
    shipper.stop(timeout=30)  # must catch up now that the collector is back
    sender.join(timeout=15)  # wait for the "end" marker's own retry (it may have started during the outage)
    elapsed = perf_counter() - start

    trace_id = captured["trace_id"]
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wal_bytes_remaining = wal.total_bytes_on_disk()
    spill_files_remaining = len(list(SPILL_DIR.glob("*.json")))
    lag_after_recovery = shipper.pending_bytes()

    with urllib.request.urlopen(f"{url}reconstruct/{trace_id}", timeout=10) as resp:
        tree = json.loads(resp.read())

    def count_nodes(node) -> int:
        return 1 + sum(count_nodes(c) for c in node["children"])

    checks = {
        "no_records_lost": count_nodes(tree) == wal.record_count,
        "shipper_confirmed_every_record": shipper.records_shipped == wal.record_count,
        "retries_actually_happened": shipper.batches_retried > 0,  # proves the outage was hit, not skipped
        "no_unresolved_blobs": "not yet received" not in json.dumps(tree),
        "collector_status_ok": store2.status.get(trace_id) == "ok",
        "wal_fully_reclaimed_after_shipping": wal_bytes_remaining == 0,
        "spill_files_fully_reclaimed_after_upload": spill_files_remaining == 0,
        "lag_metric_rose_during_outage": lag_during_outage > 0,  # proves pending_bytes() actually tracked the backlog
        "lag_metric_cleared_after_recovery": lag_after_recovery == 0,
    }

    result = {
        "label": "outage resilience (collector killed mid-trace)",
        "wall_s": elapsed,
        "peak_rss_mb": peak_kb / 1024,
        "records_written": wal.record_count,
        "records_shipped": shipper.records_shipped,
        "batches_retried": shipper.batches_retried,
        "wal_segments_deleted": shipper.segments_deleted,
        "wal_bytes_reclaimed": shipper.bytes_reclaimed,
        "lag_bytes_during_outage": lag_during_outage,
        "lag_bytes_after_recovery": lag_after_recovery,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    print(json.dumps(result))
    server2.shutdown()


if __name__ == "__main__":
    main()
