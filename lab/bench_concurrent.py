"""Proves fix #1 (per-trace dedup baseline) and fix #2 (per-trace trace_id)
under the scenario that actually motivated them: several concurrent
"requests" (threads), each a separate top-level trace, all hitting the SAME
long-lived Service instance -- exactly the case a real request-serving
process hits constantly and the earlier single-serial-trace benchmarks could
never exercise.

Before the fix: only the first request to ever touch the shared instance got
a `__full__` self baseline (process-wide InstanceCache); every other
request's self would reconstruct as missing fields. And all requests shared
one fixed trace_id, so the collector would keep only one root and silently
drop the rest.

After the fix: every request gets its own trace_id and its own fresh
InstanceCache, so every single one -- not just the first -- reconstructs a
complete, independent tree with its own full self baseline.
"""

import json
import resource
import sys
import threading
import urllib.request
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent))

from bench_common import clean_pipeline_files  # noqa: E402
from collector import start_collector  # noqa: E402
from pipeline import PipelineConfig, make_pipeline  # noqa: E402
from workload import Service, run_request  # noqa: E402

LAB_DIR = Path(__file__).parent
WAL_PATH = LAB_DIR / "lab_wal_concurrent.jsonl"
SPILL_DIR = LAB_DIR / "lab_spill_concurrent"

N_REQUESTS = 12
CALLS_PER_REQUEST = 200


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

    shared_svc = Service()  # ONE long-lived instance, hit by every "request"
    trace_ids: list = []
    lock = threading.Lock()

    def on_root(tid: str, cache) -> None:
        with lock:
            trace_ids.append(tid)

    traced_request = trace(run_request, on_root=on_root)

    def do_request() -> None:
        traced_request(shared_svc, CALLS_PER_REQUEST)

    start = perf_counter()
    threads = [threading.Thread(target=do_request) for _ in range(N_REQUESTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    wal.close()
    shipper.stop(timeout=30)
    sender.join(timeout=15)
    elapsed = perf_counter() - start

    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wal_bytes_remaining = wal.total_bytes_on_disk()
    spill_files_remaining = len(list(SPILL_DIR.glob("*.json")))

    def count_nodes(node) -> int:
        return 1 + sum(count_nodes(c) for c in node["children"])

    def self_args(node) -> list:
        found = list(node.get("args", []))
        for c in node["children"]:
            found.extend(self_args(c))
        return found

    trees = {}
    for tid in trace_ids:
        with urllib.request.urlopen(f"{url}reconstruct/{tid}", timeout=10) as resp:
            trees[tid] = json.loads(resp.read())

    checks = {
        "no_trace_id_collisions": len(set(trace_ids)) == N_REQUESTS,
        "every_trace_reconstructed": all(t is not None for t in trees.values()),
        "every_trace_has_own_root": all(t["name"] == "run_request" for t in trees.values()),
        # the crux of fix #1: EVERY trace's self snapshots carry "config" --
        # not just the first one to ever touch the shared instance.
        "every_trace_self_has_config": all(
            any(isinstance(a, dict) and "config" in a for a in self_args(t)) for t in trees.values()
        ),
        "no_unresolved_blobs": all("not yet received" not in json.dumps(t) for t in trees.values()),
        "all_statuses_ok": all(store.status.get(tid) == "ok" for tid in trace_ids),
        "node_counts_sum_to_records_written": sum(count_nodes(t) for t in trees.values()) == wal.record_count,
        "wal_fully_reclaimed_after_shipping": wal_bytes_remaining == 0,
        "spill_files_fully_reclaimed_after_upload": spill_files_remaining == 0,
    }

    result = {
        "label": f"concurrent traces ({N_REQUESTS} requests, 1 shared instance)",
        "wall_s": elapsed,
        "peak_rss_mb": peak_kb / 1024,
        "records_written": wal.record_count,
        "records_shipped": shipper.records_shipped,
        "distinct_trace_ids": len(set(trace_ids)),
        "wal_segments_deleted": shipper.segments_deleted,
        "checks": checks,
        "all_checks_passed": all(checks.values()),
    }
    print(json.dumps(result))
    server.shutdown()


if __name__ == "__main__":
    main()
