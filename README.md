# perf

A zero-dependency call-tree profiler / tracer for Python. Decorate an entry
point with `@perf.trace` and every project function and method reached during
that call is automatically instrumented, producing a wall / self / cumulative
call tree — with async support, concurrency-aware "wall vs. self" timing, and a
flat hot-spots summary.

## Install

This is a **private** library hosted in a private GitHub repository, so
installing it requires a GitHub account with read access to the repo (SSH key,
or a personal access token: replace `git+ssh://git@github.com/` with
`git+https://<TOKEN>@github.com/` in the URLs below).

### Into a uv project, without changing the project

To trace a project without adding perf to its dependencies, install it into
the project's `.venv` only:

```bash
cd your-project
uv pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

`pyproject.toml`, `uv.lock` and `git status` stay unchanged (uv's default
`.gitignore` covers `.venv`), and `uv run` / `uv add` keep perf installed. A plain `uv sync`
removes it, because it is not in the lockfile: use `uv sync --inexact`, or run
the `uv pip install` again. Add `--upgrade` to that command to move to the
latest version.

Keep trace files, and any driver script you put in the project, out of git with
the clone-local exclude file (never committed):

```bash
printf 'perf_traces/\nperf_driver.py\n' >> .git/info/exclude
```

### As a regular dependency

```bash
uv add "perf @ git+ssh://git@github.com/adc0510/performance.git"
pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

These install the latest version (the `main` branch). To pin a version, append
`@<tag>` to the URL, using a tag from
[Releases](https://github.com/adc0510/performance/releases).

### From a clone, for development

```bash
pip install -e .
```

### Optional C extension

On CPython 3.12+ the install also compiles `perf._speedups`
(`src/perf/_speedups.c`), a C version of the per-call wrapper: a call made
outside any trace costs ~40 ns instead of ~130 ns, and recording a traced call
is about twice as cheap. It needs a C compiler and the Python headers
(`python3-dev` on Debian/Ubuntu). Without them, or on Python 3.10/3.11, PyPy
or a free-threaded build, the install still succeeds and perf runs pure Python,
recording exactly the same traces. Check which one you got:

```bash
python -c "import perf; print(perf._speedups is not None)"
```

Set `PERF_PURE_PYTHON=1` to ignore an installed extension. After editing
`_speedups.c` in a clone, rebuild it with `pip install -e .` again.

### Claude Code skill: perf-trace

The repo ships a [Claude Code](https://claude.com/claude-code) skill,
`.claude/skills/perf-trace`, for debugging, fixing and optimizing code from
trace files. It finds the critical path, where a wrong value came from,
swallowed exceptions and N+1 calls, and it writes regression tests from
captured calls. It is not part of the pip package. Install it once
for every project by linking it into your personal skills folder:

```bash
git clone git@github.com:adc0510/performance.git ~/code/performance
ln -s ~/code/performance/.claude/skills/perf-trace ~/.claude/skills/perf-trace
```

Update it with `git -C ~/code/performance pull`. It needs only `python3`
(3.9+); perf itself does not have to be installed where traces are read. The
bundled reader also works on its own:

```bash
python3 ~/.claude/skills/perf-trace/scripts/perf_trace.py --help
```

## Usage

```python
import perf

@perf.trace
def main() -> None:
    ...

main()
```

Async entry points work the same way:

```python
@perf.trace
async def main() -> None:
    ...
```

On completion the call tree is printed to stdout, e.g.:

```
call                     wall/cumul     self    %tot  %parent
──────────────────────────────────────────────────────────────
─ main                       1.20 s   40.0 ms   100%    —
├─ load_config              120.0 ms  120.0 ms    10%   10%
└─ run ×3                   980.0 ms   12.0 ms    82%   82%
```

### Tracing code you don't want to edit

`perf.trace` also works as a plain function on anything you can import. A small
driver script wraps the entry point at runtime and then starts the app as usual,
so the app's code stays untouched. Keep it in the project but excluded from git
(see Install), or anywhere outside the project:

```python
# perf_driver.py
import runpy, sys
import perf

perf.OUTPUT_DIR = "perf_traces"  # one file per traced call

import app.pipeline as m
m.Pipeline.handle = perf.trace(m.Pipeline.handle)  # a method, patched on its class
# m.process = perf.trace(m.process)                # or a module-level function

sys.argv = ["app.main", *sys.argv[1:]]
runpy.run_module("app.main", run_name="__main__")  # same as `python -m app.main ...`
```

```bash
uv run python perf_driver.py      # with any arguments the app normally takes
```

Everything the wrapped function calls is traced under it. Patch the function
where callers look it up: a `from app.pipeline import process` done before the
patch keeps the original. Patch before the app imports it, or patch the
importer's name too.

## Configuration

Configuration is done via module-level attributes (set them before calling the
traced function):

| Attribute        | Default | Meaning |
|------------------|---------|---------|
| `perf.OUTPUT_FILE`  | `None` | Path to write the raw trace tree as JSON (e.g. `"perf.json"`). |
| `perf.ENDPOINT_URL` | `None` | POST the raw trace tree to this collector URL. |
| `perf.API_KEY`      | `None` | Pre-shared key sent as the `X-API-Key` header on every POST to `ENDPOINT_URL`, for collectors that require API-key auth on their ingestion route. |
| `perf.TAGS`         | `["dev"]` | Tags attached to every posted trace. |
| `perf.TIMEOUT`      | `None` | Auto-terminate a traced call after N seconds. |
| `perf.CAPTURE_IO`   | `True` | Record each call's arguments and return value. |
| `perf.MAX_DEPTH`    | `20` | Max recursion depth when serializing captured values. |
| `perf.MAX_CAPTURES_PER_FUNCTION` | `None` | Capture args/result for only the first N calls of each function per trace (all calls are still timed; later ones get `"capture_skipped": true`). Bounds capture cost in hot loops. |
| `perf.MIN_PCT`      | `0.0` | Hide tree nodes below this fraction of total time. |
| `perf.HOT_SPOTS`    | `15` | Number of functions in the flat hot-spots summary (`0` disables). |
| `perf.SCAN_VENV`    | `False` | Also instrument code under stdlib / site-packages. |

Example:

```python
import perf

perf.OUTPUT_FILE = "perf.json"
perf.ENDPOINT_URL = "http://localhost:7999/api/traces"
perf.TIMEOUT = 30

@perf.trace
def main() -> None:
    ...
```

### Per-decorator overrides

Any of the config attributes above can also be passed directly to `@trace(...)`
to override the global value **just for that function's calls** (the globals are
restored afterwards). Both the bare and parametrized forms work:

```python
@perf.trace                          # uses the current global config
def a(): ...

@perf.trace(MIN_PCT=0.01, TIMEOUT=5, OUTPUT_FILE="a.json")
def b(): ...                         # these three settings apply only to b()
```

Passing an unknown key raises `TypeError` at decoration time. Note: overrides
work by temporarily setting the module globals, so two traced calls running
concurrently with *different* overrides can transiently see each other's values.

> **Note:** unlike the original script, the library defaults `OUTPUT_FILE` and
> `ENDPOINT_URL` to `None` so importing `perf` never writes files or makes
> network calls unless you opt in.
