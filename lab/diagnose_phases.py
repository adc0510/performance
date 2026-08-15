"""Breaks total traced wall time into three phases, measured directly (not
inferred) by wrapping perf's own internal functions with timers:

  1. function time  -- the traced call's own execution, EXCLUDING capture
                        (this is exactly what node["time"] reports -- capture
                        runs after _finish_node stamps it, see _sync_wrap)
  2. trace/capture time -- total time spent inside _record_inputs/_record_output
                        across EVERY node in the tree (args/kwargs/result
                        conversion -- the per-call cost that happens on the
                        way back up through each wrapped function)
  3. send/report time -- time spent in print_tree, which covers console
                        rendering + _write_output (OUTPUT_FILE) + _send_trace
                        (ENDPOINT_URL) -- the ONE-TIME cost paid once per
                        top-level trace, after every function has returned
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

OUT_DIR = Path(__file__).parent / "diag_phases_out"
SPILL_DIR = Path(__file__).parent / "diag_phases_spill"
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

# --- instrument capture (_record_inputs/_record_output) ---
capture_time = [0.0]
_orig_record_inputs = perf._record_inputs
_orig_record_output = perf._record_output


def _timed_record_inputs(node, args, kwargs):
    start = perf_counter()
    try:
        return _orig_record_inputs(node, args, kwargs)
    finally:
        capture_time[0] += perf_counter() - start


def _timed_record_output(node, result):
    start = perf_counter()
    try:
        return _orig_record_output(node, result)
    finally:
        capture_time[0] += perf_counter() - start


perf._record_inputs = _timed_record_inputs
perf._record_output = _timed_record_output

# --- instrument send/report (print_tree) ---
send_time = [0.0]
_orig_print_tree = perf.print_tree


def _timed_print_tree(root):
    start = perf_counter()
    try:
        return _orig_print_tree(root)
    finally:
        send_time[0] += perf_counter() - start


perf.print_tree = _timed_print_tree


class _Null:
    def write(self, *a, **k):
        pass

    def flush(self):
        pass


def main() -> None:
    sys.stdout = _Null()
    traced = perf.trace(run)
    wall_start = perf_counter()
    traced(calls=3000)
    total_wall = perf_counter() - wall_start
    sys.stdout = sys.__stdout__

    # function time = total wall minus everything perf itself measured
    # (capture + send) -- what's left is time actually spent running the
    # traced code (including sys.monitoring/setprofile's own per-call cost,
    # which isn't separately instrumented here).
    function_and_hook_time = total_wall - capture_time[0] - send_time[0]

    result = {
        "total_wall_s": total_wall,
        "function_done_time_s (traced code + hook overhead)": function_and_hook_time,
        "function_done_trace_s (capture: _record_inputs/_record_output)": capture_time[0],
        "function_done_send_s (print_tree: console + write + POST)": send_time[0],
    }
    print(json.dumps(result, indent=2))

    shutil.rmtree(OUT_DIR, ignore_errors=True)
    shutil.rmtree(SPILL_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
