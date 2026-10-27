# Capturing the trace you need

A trace is only as good as what was recorded. When it lacks a value, or its timing is
inflated, ask for (or set up) a re-run with settings aimed at the question. `advise TRACE`
suggests them from an existing trace.

## No traces yet: the app doesn't use perf

An empty `perf_traces/`, or no `@perf.trace` anywhere in the app, doesn't mean you have
to edit the app or add a dependency. Ask the user which function handles one request,
message or job (or find it in the code). Then set up the three steps below without
changing anything git tracks.

**1. perf in the project's `.venv`, not in its dependencies.** Check:
`uv run python -c "import perf; print(perf.__version__)"`. If it fails, install it into
the venv only:

```bash
uv pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

- This leaves `pyproject.toml` and `uv.lock` untouched, and perf survives `uv run` and
  `uv add`.
- **A plain `uv sync` removes it**: run `uv sync --inexact` in this project instead. If
  perf is gone ("No module named perf"), run the `uv pip install` again.
- Never `uv add perf` or edit `pyproject.toml` / `uv.lock` / `requirements.txt` for it
  unless the user asks.
- Not a uv project: `pip install` the same URL into the app's own virtualenv.

**2. A driver script that wraps the entry point.** Name it `perf_driver.py` in the
project root:

```python
import runpy, sys
import perf

perf.OUTPUT_DIR = "perf_traces"                                   # one file per traced call
# perf.TRACE_LOCALS = ["pkg.mod.func"]                            # only when debugging one function

import app.pipeline as m
m.Pipeline.handle = perf.trace(m.Pipeline.handle)                # method, patched on its class
# m.process = perf.trace(m.process)                              # or a module function

sys.argv = ["app.main", *sys.argv[1:]]
runpy.run_module("app.main", run_name="__main__")                 # start the app exactly as usual
```

Run it with `uv run python perf_driver.py ...`, passing the arguments the app normally
takes. Then send one request or message, and `ls perf_traces/`.

- Wrap the function that handles **one unit of work**. Wrapping a server's main loop
  gives a single trace that never finishes.
- Patch the name callers look up. Code that ran `from app.pipeline import process`
  before the patch keeps the original, so no trace starts: patch before that import
  runs, or patch the importer's name too. Everything the wrapped call reaches is then
  traced under it.
- For a one-off function call (no server), call it directly in the driver:
  `perf.trace(m.process, OUTPUT_FILE="one.json")(payload)`.

**3. Keep the driver and the traces out of git** with the clone-local exclude file. It
is never committed, unlike `.gitignore`. Add only the lines that are missing:

```bash
grep -qx 'perf_traces/' .git/info/exclude || echo 'perf_traces/' >> .git/info/exclude
grep -qx 'perf_driver.py' .git/info/exclude || echo 'perf_driver.py' >> .git/info/exclude
```

Check with `git status --short`: neither may appear. Traces hold real data, so run
`secrets` before sharing one.

## Settings

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
