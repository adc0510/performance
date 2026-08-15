"""Follow-up: sys.monitoring's callback signature is (code, instruction_offset)
-- NOT the frame object sys.setprofile hands you. perf's actual use of the
profiler needs frame.f_locals to pull out `self`:

    def profiler(frame, event, arg):
        if (event == "return" and frame.f_code.co_name == "__init__") or event == "call":
            self = frame.f_locals.get("self")
            if self is not None:
                _patch_instance(self)

Getting the frame back inside a sys.monitoring callback requires
sys._getframe(1) (or similar) -- does that eat the overhead win we just
measured? This isolates that specific cost.
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


def time_monitoring_with_frame_access(calls: int) -> float:
    m = sys.monitoring
    TOOL_ID = m.PROFILER_ID
    m.use_tool_id(TOOL_ID, "research2")
    seen = []
    try:
        def on_start(code, offset):
            # what perf's real hook needs: pull `self` out of locals
            frame = sys._getframe(1)
            self_obj = frame.f_locals.get("self")
            if self_obj is not None:
                seen.append(id(self_obj))  # stand-in for _patch_instance(self)

        m.register_callback(TOOL_ID, m.events.PY_START, on_start)
        m.set_events(TOOL_ID, m.events.PY_START)

        start = perf_counter()
        run(calls=calls)
        elapsed = perf_counter() - start

        m.set_events(TOOL_ID, 0)
        m.register_callback(TOOL_ID, m.events.PY_START, None)
    finally:
        m.free_tool_id(TOOL_ID)
    return elapsed


def time_setprofile_real_shape(calls: int) -> float:
    seen = []

    def profiler(frame, event, arg):
        if (event == "return" and frame.f_code.co_name == "__init__") or event == "call":
            self_obj = frame.f_locals.get("self")
            if self_obj is not None:
                seen.append(id(self_obj))

    sys.setprofile(profiler)
    start = perf_counter()
    run(calls=calls)
    elapsed = perf_counter() - start
    sys.setprofile(None)
    return elapsed


def main() -> None:
    calls = 3000
    baseline = time_baseline(calls)
    setprofile_real = time_setprofile_real_shape(calls)
    monitoring_real = time_monitoring_with_frame_access(calls)

    print(
        json.dumps(
            {
                "calls": calls,
                "untraced_s": baseline,
                "setprofile_with_frame_locals_s": setprofile_real,
                "setprofile_overhead_s": setprofile_real - baseline,
                "monitoring_with_frame_locals_s": monitoring_real,
                "monitoring_overhead_s": monitoring_real - baseline,
                "speedup": (setprofile_real - baseline) / max(monitoring_real - baseline, 1e-9),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
