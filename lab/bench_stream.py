"""New pipeline, but with ENDPOINT_URL pointed at a real (local, in-process)
collector: exercises the full streamed send path (batches + blob uploads +
end marker) and validates the collector's reconstruction against what was
actually produced, not just that bytes moved somewhere.
"""

import json
import resource
import sys
import urllib.request
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent))

from bench_common import clean_pipeline_files  # noqa: E402
from collector import start_collector  # noqa: E402
from pipeline import PipelineConfig, make_pipeline  # noqa: E402
from workload import Service, run  # noqa: E402

LAB_DIR = Path(__file__).parent
WAL_PATH = LAB_DIR / "lab_wal_stream.jsonl"
SPILL_DIR = LAB_DIR / "lab_spill_stream"


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
    traced_run(calls=3000)
    # trace() already sent the "end" marker itself (it's the top-level call) --
    # just wait for the WAL to be fully flushed and shipped.
    wal.close()  # stops local writes; every record is now on disk
    shipper.stop(timeout=30)  # tails the rest of the WAL, retrying until it's all confirmed sent
    sender.join(timeout=10)  # wait for any still-retrying blob uploads
    elapsed = perf_counter() - start

    trace_id = captured["trace_id"]
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wal_bytes_remaining = wal.total_bytes_on_disk()
    spill_files_remaining = len(list(SPILL_DIR.glob("*.json")))

    # Fetch the collector's reconstruction and sanity-check it against what
    # the producer actually produced -- this is the format-compatibility
    # check, not just "did requests succeed".
    with urllib.request.urlopen(f"{url}reconstruct/{trace_id}", timeout=10) as resp:
        tree = json.loads(resp.read())

    def count_nodes(node) -> int:
        return 1 + sum(count_nodes(c) for c in node["children"])

    checks = {
        "root_name_is_run": tree["name"] == "run",
        "node_count_matches_records": count_nodes(tree) == wal.record_count,
        "no_unresolved_blobs": "not yet received" not in json.dumps(tree),
        "self_dict_resolved_has_config": any(
            isinstance(a, dict) and "config" in a for c in tree["children"] for a in c.get("args", [])
        ),
        "server_status_recorded": store.status.get(trace_id) == "ok",
        "shipper_confirmed_every_written_record": shipper.records_shipped == wal.record_count,
        "wal_fully_reclaimed_after_shipping": wal_bytes_remaining == 0,
        "spill_files_fully_reclaimed_after_upload": spill_files_remaining == 0,
    }

    result = {
        "label": "streamed to collector (option 2 wire format)",
        "wall_s": elapsed,
        "peak_rss_mb": peak_kb / 1024,
        "io_bytes": sender.bytes_sent,  # network bytes, not disk -- this scenario ships over HTTP
        "io_writes": sender.requests_sent,
        "network_bytes_sent": sender.bytes_sent,
        "network_requests_sent": sender.requests_sent,
        "collector_bytes_received": store.bytes_received,
        "collector_requests_received": store.requests_received,
        "records_written": wal.record_count,
        "records_shipped": shipper.records_shipped,
        "batches_retried": shipper.batches_retried,
        "wal_segments_deleted": shipper.segments_deleted,
        "wal_bytes_reclaimed": shipper.bytes_reclaimed,
        "wal_bytes_remaining_on_disk": wal_bytes_remaining,
        "spill_files_created": spill._n,
        "spill_bytes_reclaimed": sender.spill_bytes_reclaimed,
        "spill_files_remaining_on_disk": spill_files_remaining,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    print(json.dumps(result))
    server.shutdown()


if __name__ == "__main__":
    main()
