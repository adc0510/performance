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
Keeps the tree and the times and drops all values.

**How many calls keep their values:** since perf 0.12.0, only the first 20 calls of each
function in a trace record args/result (`MAX_CAPTURES_PER_FUNCTION=20` by default). Later
calls are timed and marked `capture_skipped`. Before 0.12.0, every call was captured unless
you set a cap. When the call you need is past the cap (its node says `capture_skipped`),
re-run that input with the cap lifted, scoped to the one entry point:
```python
@perf.trace(MAX_CAPTURES_PER_FUNCTION=None)   # every call; or a larger N
```
Expect that run to be slower: on real libraries, capturing every call costs 4-40x more
than the default cap.

**Tree-shaped objects with parent links** (docutils nodes, ASTs, DOMs, ORM sessions): one
captured node can reach the whole tree through `parent`, so a single capture can take
about a second and tens of MB. If `summary` shows recording dominating and the values are
huge, set `MAX_DEPTH=4` (or `MAX_SIZE_CAPTURE`).

What capture costs depends on the perf version (`perf.__version__`). Since 0.11.0, data
unchanged since its last capture is reused rather than converted and hashed again: long
strings, and dicts/lists/tuples of 16+ plain values still holding the same objects. A
large unchanged `self.config` then costs about a comparison per call; before 0.11.0 it was
walked and hashed on every call, so a big `self` alone could make capture 40-100x the
traced code's own time. What still costs is data that is new each call: containers that
change, fresh large strings, rich objects. So when `capture_time` is high on a function
whose `self` is large but unchanged, check the version before blaming the code.

**One function's insides** (debugging a value):
```python
@perf.trace(TRACE_LOCALS=["pkg.mod.func"])        # fnmatch on node names; "Class.method", "Cart.*"
```
TRACE_LOCALS costs time on every line of the matched functions (Python 3.12+), so keep the
pattern narrow. `MAX_LOCALS_EVENTS` (default 1000) caps entries per call. Raise it, or
use a smaller input, when `locals_dropped` shows up.

**A smaller file around what matters:**
- `LOG_FUNCTIONS=["Repo.find", "pkg.mod.handler"]` writes only those calls plus their
  ancestors (everything is still recorded, then pruned at write time). The file has no
  marker for what was pruned: the dropped siblings' time shows up as the **self time of the
  kept ancestors**, and `serial`, `nplus1`, `critical-path` and `to-test --mock-leaves` only
  see the kept calls. Don't use it for timing questions.
- `SKIP_FUNCTIONS=["pkg.noisy.*"]` doesn't instrument those calls or anything under them.
  That cuts overhead too, but their time folds into the caller.

**Bigger or deeper values:** `MAX_DEPTH` (default 20), `MAX_SIZE_CAPTURE` (None = no
limit) and `SPILL_DIR` (oversize values go to their own files), plus
`MAX_YIELDS_CAPTURE` (default 10) for generators.

**Library code too:** `SCAN_VENV=True` instruments stdlib and site-packages. Use it
sparingly: it's much more expensive and noisy, but it splits "self time of the project
function" into the library calls behind it. Scope it to the packages in question with
`perf._SCAN_INCLUDE_PREFIXES = ("httpx", "sqlalchemy")` (or the same key on the decorator):
those packages are traced in addition to your own code, and no other library is.

**Keeping secrets out:**
- `SKIP_VARIABLES=["*password*", "*token*", "api_key"]` drops values by name: kwargs,
  fields, dict keys, and positional args by parameter name.
- `REDACT_PATTERNS=[r"(?<=password=)\w+"]` masks by content everywhere, including reprs,
  exception messages and tracebacks.
Run `secrets` on a sample trace to find what needs covering.

**Stopping runaway calls:** `TIMEOUT=30` cancels async entry points and interrupts sync
ones via SIGALRM (Unix main thread only). Calls cut off show in `unfinished`.

**Where files go:** `OUTPUT_DIR` writes one uuid-named file per call, the default for
services. `OUTPUT_FILE` names a fixed file, overwritten on every call. With
`ENDPOINT_URL`, each trace is POSTed to a collector as the same JSON plus `status`
(`ok`/`error`) and `tags`: save a body to a `.json` file and every command reads it.
Since 0.12.0 no API key is built in: set `perf.API_KEY` or the `PERF_API_KEY` environment
variable if the collector needs one. `SEND_TIMEOUT` (default 5 s) bounds each POST.
`PRINT_REPORT=False` stops the printed tree and hot-spots table (files and POSTs are
unchanged), which suits services that trace every request.

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
- `SUPPRESS_ERRORS=True`: perf swallows exceptions at the innermost traced call and returns
  a default value derived from its return annotation (or `DEFAULT_RETURN`), so a failure
  looks like an empty result. Since 0.12.0 only `Exception` subclasses are swallowed:
  `SystemExit`, `KeyboardInterrupt`, `asyncio.CancelledError` and `TraceTimeout` always
  propagate. Before 0.12.0 these were swallowed too: an older trace can show a `sys.exit()`,
  a Ctrl-C or a task cancellation "suppressed by perf". Such a node has both `exception` and `result`; `errors` and
  `summary` report it as "suppressed by perf". Without a captured result (`CAPTURE_IO=False`,
  `capture_skipped`) it can't be told apart from a caught exception.
- `LOG_FUNCTIONS`: pruned calls fold into their ancestors' self time (see above).
