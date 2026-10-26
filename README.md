# perf

A zero-dependency call-tree profiler / tracer for Python. Decorate an entry
point with `@perf.trace` and every project function and method reached during
that call is automatically instrumented, producing a wall / self / cumulative
call tree — with async support, concurrency-aware "wall vs. self" timing, and a
flat hot-spots summary.

## Install

This is a **private** library hosted in a private GitHub repository, so
installing it requires a GitHub account with read access to the repo.

**Over SSH** (uses your configured SSH key):

```bash
pip install "perf @ git+ssh://git@github.com/adc0510/performance.git"
```

**Over HTTPS with a personal access token** (PAT needs `repo` / read access):

```bash
pip install "perf @ git+https://<TOKEN>@github.com/adc0510/performance.git"
```

Pin to a tag or commit by appending `@<ref>`, e.g. `...performance.git@v0.1.0`.

In a `requirements.txt`:

```
perf @ git+ssh://git@github.com/adc0510/performance.git@v0.1.0
```

Or, from a local clone for development:

```bash
pip install -e .
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
