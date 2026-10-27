# Capturing the trace you need

A trace is only as good as what was recorded. When it lacks a value, or its timing is
inflated, ask for (or set up) a re-run with settings aimed at the question. `advise TRACE`
suggests them from an existing trace.

Settings are module attributes set before the call (`perf.TRACE_LOCALS = [...]`) or
per-decorator overrides that apply only to that entry point
(`@perf.trace(TRACE_LOCALS=[...], OUTPUT_DIR="perf_traces")`). Unknown keys raise
`TypeError`. Two concurrent traced calls with different overrides can briefly see each
other's values.

## Recipes

**Timing only** (trustworthy numbers):
```python
@perf.trace(CAPTURE_IO=False, TRACE_LOCALS=[], OUTPUT_DIR="perf_traces")
```
Keeps the tree and the times and drops all values. To keep some values cheaply, use
`MAX_CAPTURES_PER_FUNCTION=50`: later calls of each function are timed and marked
`capture_skipped`.

**One function's insides** (debugging a value):
```python
@perf.trace(TRACE_LOCALS=["pkg.mod.func"])        # fnmatch on node names; "Class.method", "Cart.*"
```
TRACE_LOCALS costs time on every line of the matched functions (Python 3.12+), so keep the
pattern narrow. `MAX_LOCALS_EVENTS` (default 1000) caps entries per call. Raise it, or
use a smaller input, when `locals_dropped` shows up.

**A smaller file around what matters:**
- `LOG_FUNCTIONS=["Repo.find", "pkg.mod.handler"]` writes only those calls plus their
  ancestors (everything is still recorded, then pruned at write time).
- `SKIP_FUNCTIONS=["pkg.noisy.*"]` doesn't instrument those calls or anything under them.
  That cuts overhead too, but their time folds into the caller.

**Bigger or deeper values:** `MAX_DEPTH` (default 20), `MAX_SIZE_CAPTURE` (None = no
limit) and `SPILL_DIR` (oversize values go to their own files), plus
`MAX_YIELDS_CAPTURE` (default 10) for generators.

**Library code too:** `SCAN_VENV=True` instruments stdlib and site-packages. Use it
sparingly: it's much more expensive and noisy, but it splits "self time of the project
function" into the library calls behind it.

**Keeping secrets out:**
- `SKIP_VARIABLES=["*password*", "*token*", "api_key"]` drops values by name: kwargs,
  fields, dict keys, and positional args by parameter name.
- `REDACT_PATTERNS=[r"(?<=password=)\w+"]` masks by content everywhere, including reprs,
  exception messages and tracebacks.
Run `secrets` on a sample trace to find what needs covering.

**Stopping runaway calls:** `TIMEOUT=30` cancels async entry points and interrupts sync
ones via SIGALRM (Unix main thread only). Calls cut off show in `unfinished`.

**Where files go:** `OUTPUT_DIR` writes one uuid-named file per call, the default for
services. `OUTPUT_FILE` names a fixed file, overwritten on every call.

## What gets traced across threads and processes

- ThreadPoolExecutor jobs (`submit`, `map`, `loop.run_in_executor`, `asyncio.to_thread`)
  run in a copy of the submitter's context, so they nest under the submitting call.
- `Thread.start` passes the trace on when the thread's entry point is project code.
  Long-lived library threads (executor workers, connection-pool keepalives) don't inherit it.
- ProcessPoolExecutor and `multiprocessing.Pool` calls are traced in the worker and
  grafted under the call that submitted them.
- Work still running when the trace ends stops being recorded at that moment (see
  `unfinished`).
- Nested functions, lambdas and functions held in registries are traced on Python 3.12+.
  On older Pythons they fold into the caller's self time.

## Things that distort a trace

- A debugger (VS Code/debugpy), coverage or cProfile running at the same time: everything
  slows down.
- Heavy capture (see `summary`'s recording share). Self times exclude capture, but parents'
  wall times include their children's.
- `SUPPRESS_ERRORS=True`: perf swallows exceptions and returns a default value derived
  from the return annotation, so a failure looks like an empty result.
