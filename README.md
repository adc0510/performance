# perf

A zero-dependency call-tree tracer for Python. Point it at one entry point and
every project function and method reached from it is instrumented
automatically: wall / self / cumulative time per call, the arguments each call
received, what it returned, the exceptions it raised (swallowed ones included)
and, if you ask, every local variable change line by line. Async, threads and
process pools are followed.

## Set up tracing in your repo

Six steps from nothing to a trace you can read, alone or with Claude Code.
Nothing in your project's `pyproject.toml`, lockfile or git history has to
change.

**1. Install perf into the project's virtualenv.** It is a private repository,
so you need read access; on a fresh machine, check
[On a new server or in a new repo](#on-a-new-server-or-in-a-new-repo) first
(git, a C compiler, and how to confirm the fast C extension is in use):

```bash
cd your-project
uv pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
# without uv: .venv/bin/pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

**2. Keep traces and the driver script out of git** (a clone-local exclude,
never committed):

```bash
printf 'perf_traces/\nperf_driver.py\n' >> .git/info/exclude
```

**3. Wrap the entry point.** Either decorate it:

```python
import perf

perf.OUTPUT_DIR = "perf_traces"     # one JSON file per traced call

@perf.trace
def handle_request(payload): ...
```

or, to leave the app's code untouched, wrap it from a driver script:

```python
# perf_driver.py
import runpy, sys
import perf

perf.OUTPUT_DIR = "perf_traces"
# perf.TRACE_LOCALS = ["Pipeline.handle"]          # optional: local variables, line by line

import app.pipeline as m
m.Pipeline.handle = perf.trace(m.Pipeline.handle)  # a method, patched on its class
# m.process = perf.trace(m.process)                # or a module-level function

sys.argv = ["app.main", *sys.argv[1:]]
runpy.run_module("app.main", run_name="__main__")  # same as `python -m app.main ...`
```

Patch the function where callers look it up: a `from app.pipeline import process`
done before the patch keeps the original, so patch before the app imports it, or
patch the importer's name too.

**4. Run the app as usual:**

```bash
uv run python perf_driver.py        # with any arguments the app normally takes
```

Each traced call prints a call tree and hot-spots table and writes
`perf_traces/<uuid>.json`.

**5. Give Claude Code the perf-trace skill** (once per machine; skip it if you
don't use [Claude Code](https://claude.com/claude-code)). The skill is not part
of the pip package: it lives in this repo, and linking it into your personal
skills folder makes it available in every project on the machine, with or
without perf installed there:

```bash
git clone git@github.com:adc0510/performance.git ~/code/performance   # skip if already cloned
mkdir -p ~/.claude/skills
ln -s ~/code/performance/.claude/skills/perf-trace ~/.claude/skills/perf-trace
ls ~/.claude/skills/perf-trace/SKILL.md                                  # check: the link resolves
```

Start a new Claude Code session afterwards. Keep it current with
`git -C ~/code/performance pull`. More in
[Claude Code skill: perf-trace](#claude-code-skill-perf-trace).

**6. Read the trace.** Ask Claude Code about it, e.g. "why is `handle_request`
slow in perf_traces/<uuid>.json" or "where did this wrong value come from": the
skill loads when you mention a perf trace (or name it: "use perf-trace"). Or
open the JSON yourself (see [What a trace contains](#what-a-trace-contains)), or
run the skill's reader directly, which needs only `python3`:

```bash
python3 ~/.claude/skills/perf-trace/scripts/perf_trace.py summary perf_traces/<uuid>.json
```

To explore it in a browser instead, write it out as one HTML page: a call tree
with time bars, each call's arguments, result, exception and locals, a call
graph of which function calls which (click a function to see everything
upstream and downstream of it, or find the call chains between two functions),
with a timeline of when each one ran (both downloadable as SVG), a function table with callers and callees, and a
flame view. The page is a single file that
needs no network access, so it can be opened from a server's disk or attached
to a ticket. It holds every captured value: check it with `secrets` first, or
add `--no-values` to keep only the structure and timing.

```bash
python3 ~/.claude/skills/perf-trace/scripts/perf_trace.py html perf_traces/<uuid>.json   # writes perf_traces/<uuid>.html
```

## Install

perf is hosted in a **private** GitHub repository: you need a GitHub account with
read access (an SSH key, or a personal access token: replace
`git+ssh://git@github.com/` with `git+https://<TOKEN>@github.com/` in the URLs
below). It runs on CPython 3.10+; `TRACE_LOCALS` and the C extension need 3.12+.

### On a new server or in a new repo

**What the machine needs first** (once):

| need | why | how |
|---|---|---|
| Read access to this GitHub repo | it is private | an SSH key on your GitHub account (or a read-only deploy key on the repo); `ssh -T git@github.com` should greet you. Or use a token URL (above). |
| `git` | uv and pip clone the repo with it | `sudo apt install -y git` · `sudo dnf install -y git` |
| A C compiler (`cc`) | builds the C extension, and any dependency without a prebuilt wheel for your Python | `sudo apt install -y build-essential` · `sudo dnf install -y gcc make` · `sudo yum install -y gcc make` · `sudo apk add build-base` (as root, drop `sudo`) |
| Python 3.12+ headers | builds the C extension | Pythons installed by uv already have them. A system Python needs `python3-dev` (apt) or `python3-devel` (dnf). |
| [uv](https://docs.astral.sh/uv/) (recommended) | installs into the project's `.venv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |

Without a compiler or headers the install still succeeds, but perf runs pure
Python (the same traces, slower capture). Fresh servers and container images
often have no compiler at all, so check before installing: `which` should print
two paths, and the header check (run in the project, once its `.venv` exists)
should print `True`:

```bash
which cc gcc
uv run python -c "import sysconfig, os; print(os.path.exists(os.path.join(sysconfig.get_paths()['include'], 'Python.h')))"
```

Install the compiler **before** perf. If perf went in first, the build without
its C extension is cached: reinstall with `--no-cache` (last row of the table
below).

**Prefer a Python your dependencies ship wheels for.** A brand-new Python (say
3.14) often has no prebuilt wheels yet for packages with native code, so uv
compiles them from source. For example, litellm 1.92.0 has wheels only up to
3.13, and on 3.14 its Rust build fails with `linker 'cc' not found` when there
is no compiler. Pin the project to a supported version instead:

```bash
uv python pin 3.13 && rm -rf .venv && uv venv --python 3.13   # then reinstall the project's dependencies
```

**Then, in the repo** (with no other virtualenv activated: `uv pip install`
installs into an active one, not the project's `.venv`, so run `deactivate`
first if your prompt shows one):

```bash
cd your-project
uv venv                      # only if the project has no .venv yet (add --python 3.12 to pick one)
uv pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
uv run python -c "import perf; print(perf.__version__, perf._speedups is not None)"
```

The last line should print the latest version and `True`. `True` means the C
extension loaded; `False` means pure Python. To find out why:

```bash
uv run python -c "import perf, sys, os, sysconfig; print(perf.__file__, sys.version.split()[0], 'free-threaded' if sysconfig.get_config_var('Py_GIL_DISABLED') else 'GIL', os.environ.get('PERF_PURE_PYTHON'))"
```

| what it shows | cause | fix |
|---|---|---|
| `No module named 'perf'` | it went into another virtualenv that was active (`echo $VIRTUAL_ENV` shows it) | `deactivate`, then run the `uv pip install` again in the project |
| a path outside the project's `.venv` | another copy of perf comes first, usually from `PYTHONPATH` (an old machine-wide install) | remove that `PYTHONPATH` entry (check `~/.bashrc`), open a new shell, reinstall |
| Python 3.10 / 3.11 | the C extension needs 3.12+ | recreate the venv with `uv venv --python 3.12` and reinstall |
| `free-threaded` | not built for free-threaded Python | use a regular (GIL) build |
| last value is not `None` | `PERF_PURE_PYTHON` is set | `unset PERF_PURE_PYTHON` |
| everything above is fine | it was built before a compiler or headers were available, and uv reuses that cached build | install them, then `uv pip install --reinstall --no-cache "perf @ git+ssh://git@github.com/adc0510/performance.git"` |

### Into a project's `.venv` only

```bash
uv pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

`pyproject.toml`, `uv.lock` and `git status` stay unchanged (uv's default
`.gitignore` covers `.venv`), and `uv run` / `uv add` keep perf installed. A plain
`uv sync` removes it because it is not in the lockfile: use `uv sync --inexact`,
or run the `uv pip install` again. Add `--upgrade` to move to the latest version.

### As a regular dependency

```bash
uv add "perf @ git+ssh://git@github.com/adc0510/performance.git"
pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

These install the latest version (the `main` branch). To pin one, append
`@<tag>` to the URL (e.g. `...performance.git@v0.11.2`), using a tag from
[Releases](https://github.com/adc0510/performance/releases).

### From a clone, for development

```bash
pip install -e .
```

### Optional C extension

On CPython 3.12+ (with the GIL) the install also compiles `perf._speedups`
(`src/perf/_speedups.c`): the per-call wrapper and the checks that let value
capture reuse unchanged data are done in C. A call made outside any trace costs
~40 ns instead of ~130 ns, and recording a traced call is about twice as cheap.
It needs a C compiler and the Python headers (`python3-dev` on Debian/Ubuntu).
Without them, or on Python 3.10/3.11, PyPy or a free-threaded build, the install
still succeeds and perf runs pure Python, recording exactly the same traces.
Check which one you got:

```bash
python -c "import perf; print(perf._speedups is not None)"
```

If it prints `False`, see the table in
[On a new server or in a new repo](#on-a-new-server-or-in-a-new-repo). Set
`PERF_PURE_PYTHON=1` to ignore an installed extension. After editing
`_speedups.c` in a clone, rebuild it with `pip install -e .` again.

### Claude Code skill: perf-trace

The repo ships a [Claude Code](https://claude.com/claude-code) skill,
`.claude/skills/perf-trace`, for debugging, fixing and optimizing code from
trace files. It finds the critical path, where a wrong value came from,
swallowed exceptions and N+1 calls, and it writes regression tests from
captured calls. It is not part of the pip package, so a project that installs
perf does not get it: install it once per machine by linking it into your
personal skills folder, which Claude Code reads in every project:

```bash
git clone git@github.com:adc0510/performance.git ~/code/performance
mkdir -p ~/.claude/skills
ln -s ~/code/performance/.claude/skills/perf-trace ~/.claude/skills/perf-trace
```

Claude Code picks it up in new sessions. The link follows the clone's checked-out
branch, so keep that clone on `main`. To make it available in one project only,
link it into that project's `.claude/skills/` instead and keep it out of git with
`echo '.claude/skills/perf-trace' >> .git/info/exclude`.

Update it with `git -C ~/code/performance pull`. It needs only `python3` (3.9+);
perf itself does not have to be installed where traces are read. The bundled
reader also works on its own (`--help` lists its 43 commands):

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

`perf.trace` also works as a plain function on anything you can import
(`perf.trace(fn)(...)`, `Class.method = perf.trace(Class.method)`), which is what
the driver script in [Set up tracing in your repo](#set-up-tracing-in-your-repo)
relies on.

### Per-decorator overrides

Any setting in [Configuration](#configuration) can also be passed to
`@trace(...)` to override the global value **just for that function's calls**
(the globals are restored afterwards):

```python
@perf.trace                          # uses the current global config
def a(): ...

@perf.trace(MIN_PCT=0.01, TIMEOUT=5, OUTPUT_FILE="a.json")
def b(): ...                         # these three settings apply only to b()
```

Passing an unknown key raises `TypeError` at decoration time. Overrides work by
temporarily setting the module globals, so two traced calls running
concurrently with *different* overrides can transiently see each other's values.

## What a trace contains

A trace file is `{"variables": {...}, "trace": {...}}` (plus `"sources"` with
`TRACE_LOCALS`). `trace` is the call tree: one node per call, with its timing,
`args` / `kwargs` as received, `result` as returned, `exception` if it raised,
and `children`. Any value of 64+ serialized bytes is stored once in `variables`
and referenced as `{"__ref__": "vN"}`, so an object passed to many calls costs
its size once. Abridged, from a `Service.handle(self, item_id, flag=False)`
call traced with `TRACE_LOCALS = ["Service.handle"]`:

```json
{
  "variables": {
    "v20": {"setting_00": {"__ref__": "v0"}, "...": "..."},
    "v22": {"__type__": "Service", "config": {"__ref__": "v20"}, "cache": {"item-1": 10}, "counter": 1}
  },
  "trace": {"name": "main", "children": [
    {"name": "Service.handle", "time": 0.00071,
     "args": [{"__ref__": "v22"}, 2], "kwargs": {"flag": true},
     "result": {"name": "item-2", "total": 5, "tags": ["new", "item-2", "flagged"]},
     "locals": [
       [28, {"name": "item-2"}],
       [29, 0, {"self": [["=", ["counter"], 2]]}],
       [31, {"tags": ["new", "item-2"]}],
       [33, 0, {"tags": [["+", [], ["flagged"]]]}],
       [34, {"total": 5}]
     ]}
  ]}
}
```

Each `locals` entry is `[line, values set, in-place edits, ...]` (`0` = empty
part): line 29 changed `self.counter` to 2, line 33 appended `"flagged"` to
`tags`. `perf.expand_locals(trace)` rewrites the entries as full values (in
place), and the perf-trace skill's reference `trace-format.md` documents every
field.

## What capture costs

Recording values is most of perf's overhead: every call's arguments (including
`self`) and result are converted to JSON-ready data **at the moment of the
call**, because they can change afterwards, and stored once per distinct
content. Since 0.11.0, data that has not changed since its last capture is
recognised instead of being converted and hashed again: long strings, and
dicts / lists / tuples (16+ entries) of plain values that still hold the same
objects. A large, unchanged `self.config` therefore costs a comparison per
call, not a walk. What still costs is data that really is new: containers that
change every call, fresh large strings, rich objects.

Measured on CPython 3.13 with the C extension, 3,000 calls whose `self` holds
a 2,000-entry dict (`lab/workload_matrix.py`):

| workload | untraced | perf 0.10.3 | perf 0.11.0 |
|---|---|---|---|
| one method per call | 0.72 s | 34.5 s | 1.5 s |
| three nested methods per call | 0.73 s | 104.7 s | 2.3 s |
| TRACE_LOCALS on the method, 300 calls | — | 4.1 s | 0.8 s |

Traces are identical between the two versions. To keep overhead down:

- **Timing only:** `CAPTURE_IO=False` (and no `TRACE_LOCALS`) keeps the tree and
  the times and drops all values.
- **Hot loops:** by default only the first 20 calls of each function are
  captured; the rest are timed only (`MAX_CAPTURES_PER_FUNCTION`, `None` = every call).
- **Huge values:** `MAX_SIZE_CAPTURE` drops (or, with `SPILL_DIR`, spills to a
  file) any single value above a size.
- **TRACE_LOCALS** runs on every line of the functions it matches: keep the
  pattern to the function you are debugging.

Self times in the report exclude capture time; a parent's wall time includes
its children's capture. The perf-trace reader's `summary` and `advise` commands
show how much of a trace was perf's own recording cost.

## Configuration

Set these as module attributes before the traced call (`perf.OUTPUT_DIR = ...`),
or per entry point via [overrides](#per-decorator-overrides).

**Output**

| Setting | Default | Meaning |
|---|---|---|
| `OUTPUT_DIR` | `None` | Write each trace to `<dir>/<uuid>.json` (or `<dir>/<OUTPUT_FILE>.json`). |
| `OUTPUT_FILE` | `None` | Write to this fixed path, overwritten on every traced call. |
| `ENDPOINT_URL` | `None` | POST each trace to this collector URL, as `{"status", "tags", "trace"}`. |
| `API_KEY` | `$PERF_API_KEY` or `None` | Sent as the `X-API-Key` header on every POST to `ENDPOINT_URL`. No key is built in: set it, or export `PERF_API_KEY`. |
| `SEND_TIMEOUT` | `5.0` | Seconds a POST to `ENDPOINT_URL` may take. Error traces are sent before the failing call returns, so this bounds how long a slow collector can delay it. |
| `TAGS` | `["dev"]` | Tags attached to every posted trace. |
| `LOG_FUNCTIONS` | `[]` | Write only these calls (fnmatch on node names) plus their ancestors. Pruned calls fold into their ancestors' self time, so don't use it for timing questions. |
| `MIN_PCT` | `0.0` | Hide printed tree nodes below this fraction of the total time. |
| `HOT_SPOTS` | `15` | Rows in the printed hot-spots table (`0` disables it). |

**What is traced**

| Setting | Default | Meaning |
|---|---|---|
| `SKIP_FUNCTIONS` | `[]` | Don't trace these functions (fnmatch, e.g. `"Class._*"`) or anything under them; their time folds into the caller. |
| `SCAN_VENV` | `False` | Also instrument stdlib / site-packages (expensive). |
| `TIMEOUT` | `None` | Stop a traced call after N seconds, keeping the partial tree. |
| `SUPPRESS_ERRORS` | `False` | Swallow exceptions at the innermost traced call and return a default from its return annotation (or `DEFAULT_RETURN`). Only `Exception` subclasses: `SystemExit`, `KeyboardInterrupt`, `asyncio.CancelledError` and `TraceTimeout` always propagate. |
| `DEFAULT_RETURN` | unset | The value (or a callable producing it) returned for a suppressed exception. |

**Values**

| Setting | Default | Meaning |
|---|---|---|
| `CAPTURE_IO` | `True` | Record each call's arguments and return value. |
| `TRACE_LOCALS` | `[]` | Functions (fnmatch on node names, e.g. `"Cart.*"`) whose local variables are recorded line by line. Python 3.12+. |
| `MAX_LOCALS_EVENTS` | `1000` | Cap on `TRACE_LOCALS` entries per call (beyond it: `locals_dropped`). |
| `MAX_CAPTURES_PER_FUNCTION` | `20` | Capture args/result for only the first N calls of each function per trace; later calls are timed and marked `"capture_skipped": true`. Set `None` to capture every call (much slower on real code: see review/06). |
| `PRINT_REPORT` | `True` | Print the call tree and hot-spots table when a traced call ends. `False` skips building it (services that only read written/sent traces). |
| `MAX_YIELDS_CAPTURE` | `10` | Values recorded per generator (the total count is always kept). |
| `MAX_DEPTH` | `20` | Nesting depth at which captured values are cut off (shown as their repr). |
| `MAX_SIZE_CAPTURE` | `None` | Drop any single captured value above this size (characters of content). |
| `SPILL_DIR` | `None` | With `MAX_SIZE_CAPTURE`: write oversized values to their own files here instead of dropping them. |
| `SKIP_VARIABLES` | `[]` | Leave out values by name (fnmatch: kwargs, object fields, dict keys, locals), e.g. `["*password*", "*token*"]`. |
| `REDACT_PATTERNS` | `[]` | Regexes replaced by `<redacted>` in every captured string (dict keys included), repr, exception message, traceback line and recorded source line. |

Example:

```python
import perf

perf.OUTPUT_DIR = "perf_traces"
perf.SKIP_VARIABLES = ["*password*", "*token*"]
perf.TIMEOUT = 30

@perf.trace
def main() -> None:
    ...
```

Importing perf never writes files or makes network calls on its own: output
starts only once `OUTPUT_DIR`, `OUTPUT_FILE` or `ENDPOINT_URL` is set.
