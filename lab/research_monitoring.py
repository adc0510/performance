"""Research: does sys.monitoring (PEP 669, Python 3.12+) actually cost less
than sys.setprofile for the exact thing perf.trace uses it for -- getting
notified on every call/return so newly-created instances can be
auto-patched? Measures both against the same workload, no-op callbacks (so
this isolates hook overhead itself, not perf's own capture logic).
"""

import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).parent))

from workload import run  # noqa: E402


def time_baseline(calls: int) -> float:
    start = perf_counter()
    run(calls=calls)
    return perf_counter() - start


def time_setprofile(calls: int) -> float:
    def profiler(frame, event, arg):
        pass  # no-op, same shape as perf's _make_profiler callback

    sys.setprofile(profiler)
    start = perf_counter()
    run(calls=calls)
    elapsed = perf_counter() - start
    sys.setprofile(None)
    return elapsed


def time_monitoring(calls: int) -> float | None:
    if not hasattr(sys, "monitoring"):
        return None
    m = sys.monitoring
    TOOL_ID = m.PROFILER_ID
    m.use_tool_id(TOOL_ID, "research")
    try:
        def on_call(code, offset, *a):
            pass

        m.register_callback(TOOL_ID, m.events.PY_START, on_call)
        m.register_callback(TOOL_ID, m.events.PY_RETURN, on_call)
        m.set_events(TOOL_ID, m.events.PY_START | m.events.PY_RETURN)

        start = perf_counter()
        run(calls=calls)
        elapsed = perf_counter() - start

        m.set_events(TOOL_ID, 0)
        m.register_callback(TOOL_ID, m.events.PY_START, None)
        m.register_callback(TOOL_ID, m.events.PY_RETURN, None)
    finally:
        m.free_tool_id(TOOL_ID)
    return elapsed


def main() -> None:
    calls = 3000
    baseline = time_baseline(calls)
    setprofile = time_setprofile(calls)
    monitoring = time_monitoring(calls)

    result = {
        "calls": calls,
        "untraced_s": baseline,
        "sys.setprofile_s": setprofile,
        "sys.setprofile_overhead_s": setprofile - baseline,
        "sys.monitoring_s": monitoring,
        "sys.monitoring_overhead_s": (monitoring - baseline) if monitoring is not None else None,
    }
    if monitoring is not None:
        result["monitoring_vs_setprofile_speedup"] = (setprofile - baseline) / max(monitoring - baseline, 1e-9)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
