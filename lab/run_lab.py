"""Runs all three scenarios as isolated subprocesses and prints a comparison:

  1. untraced          -- no instrumentation, the reference point
  2. baseline           -- current perf.trace (nested tree, unlimited capture,
                            OUTPUT_DIR set so it actually persists, like real usage)
  3. new pipeline        -- flat records + WAL-backed queue + spill-on-capture + dedup

Each subprocess self-reports wall time, peak RSS, and IO (bytes + write
calls) -- run as separate processes so one benchmark's allocations/IO can't
bleed into another's numbers.
"""

import json
import subprocess
import sys
from pathlib import Path

LAB_DIR = Path(__file__).parent


def run_bench(script: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(LAB_DIR / script)],
        capture_output=True,
        text=True,
        cwd=LAB_DIR,
        timeout=120,
    )
    if proc.returncode != 0:
        print(f"--- {script} failed ---\n{proc.stdout}\n{proc.stderr}", file=sys.stderr)
        raise SystemExit(1)
    line = [l for l in proc.stdout.strip().splitlines() if l.startswith("{")][-1]
    return json.loads(line)


def main() -> None:
    print("Running untraced (no instrumentation)...")
    untraced = run_bench("bench_untraced.py")
    print("Running baseline (perf.trace, nested tree, no size limit)...")
    baseline = run_bench("bench_baseline.py")
    print("Running new pipeline (flat records + WAL + spill + dedup)...")
    new = run_bench("bench_new.py")
    print("Running streamed pipeline (records shipped to a live local collector)...")
    stream = run_bench("bench_stream.py")

    rows = [untraced, baseline, new, stream]
    print()
    header = f"{'':38}{'wall (s)':>10}{'peak RSS (MB)':>16}{'IO bytes':>12}{'IO writes':>11}"
    print(header)
    for r in rows:
        print(f"{r['label']:38}{r['wall_s']:>10.3f}{r['peak_rss_mb']:>16.1f}{r['io_bytes']:>12,}{r['io_writes']:>11,}")

    print()
    print("Relative to untraced:")
    for r in (baseline, new, stream):
        time_x = r["wall_s"] / untraced["wall_s"]
        rss_x = r["peak_rss_mb"] / untraced["peak_rss_mb"]
        print(f"  {r['label']:38} {time_x:.2f}x time   {rss_x:.2f}x RAM")

    reduction = 100 * (1 - new["peak_rss_mb"] / baseline["peak_rss_mb"])
    time_delta = 100 * (1 - new["wall_s"] / baseline["wall_s"])
    io_delta = 100 * (1 - new["io_bytes"] / baseline["io_bytes"]) if baseline["io_bytes"] else float("nan")
    print()
    print("New pipeline (local WAL only) vs baseline:")
    print(f"  peak RSS:  {reduction:+.1f}%")
    print(f"  wall time: {time_delta:+.1f}%")
    print(f"  IO bytes:  {io_delta:+.1f}%  (writes: {baseline['io_writes']} -> {new['io_writes']})")

    print()
    print(f"Streamed-to-collector format check: all_checks_passed = {stream['all_checks_passed']}")
    for k, v in stream["checks"].items():
        print(f"  {k}: {v}")

    print()
    print("New pipeline internals:")
    for k in ("records_written", "queue_overflows", "spill_files", "spill_bytes", "instance_full_snapshots", "instance_diffs"):
        print(f"  {k}: {new[k]}")


if __name__ == "__main__":
    main()
