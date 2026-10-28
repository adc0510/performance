# perf trace file format

What `perf` writes to `OUTPUT_DIR/<uuid>.json` (or `OUTPUT_FILE`). Read this when a
field or marker in a trace is unclear; `scripts/perf_trace.py` already decodes all of it.

## Contents
- Top level
- Node fields
- How values are captured (markers)
- TRACE_LOCALS entries
- Timing semantics
- Settings that shape what a trace contains

## Top level

```json
{"variables": {"v0": ..., "v1": ...}, "trace": <root node>, "sources": {"s0": {"file": "/abs/path.py", "lines": {"12": "x = f(y)"}}}}
```

- `variables` -- content pool. Any captured container or long string (>= 64 bytes of JSON)
  is stored once here and referenced from the tree as `{"__ref__": "v12"}`. Two captures
  with identical content share one entry; different states of one object are separate
  entries (captures are snapshots, never stale). `{"__type__": "Foo", "__ref__": "v3"}`
  means an object of type Foo whose fields are the dict `variables["v3"]`.
- `trace` -- the root node: the function decorated with `@perf.trace` (one file per call).
- `sources` -- only with TRACE_LOCALS: the text of every recorded line, once per file.
- `status` / `tags` -- only in bodies POSTed to `ENDPOINT_URL`; otherwise the same shape.

With `LOG_FUNCTIONS` set, `children` lists were pruned to the matching calls and their
ancestors before writing, with no marker: the time of what was dropped counts as the
self time of the nodes that remain.

## Node fields

| field | meaning |
|---|---|
| `name` | `module.function`, `Class.method` (methods carry no module), `module.outer.<locals>.inner`, `Class.prop` / `Class.prop.setter`, `Class.__init__`. Same-named sibling calls are separate nodes. |
| `time` | wall seconds of this call (None/null: still running when the trace closed, e.g. TIMEOUT or a background thread) |
| `start` | `perf_counter()` at the real start of the call (after argument capture) -- only meaningful relative to other nodes of the same trace |
| `children` | calls made from inside this call, in start order |
| `args` | positional arguments, captured BEFORE the call ran (state at entry) |
| `kwargs` | keyword arguments (omitted when empty) |
| `params` | positional parameter names, present when TRACE_LOCALS needs them; otherwise args are anonymous (the first arg of a `Class.method` is `self`) |
| `result` | return value, captured at return |
| `capture_time` | seconds perf spent converting this call's values (perf overhead, excluded from self time) |
| `capture_skipped` | true: call past `MAX_CAPTURES_PER_FUNCTION` -- timed, but no args/result |
| `exception` | repr of the exception the call raised (also on every ancestor it propagated through). With `result` on the same node: SUPPRESS_ERRORS swallowed it there and the call returned that default |
| `exception_origin` | `{file, line, function, text}` of the innermost frame -- where it was raised |
| `exception_debug` | `{frames: [...], formatted: "<full traceback>", caused_by: {...}}` -- full chain, untruncated |
| `yields` / `yielded` / `active_time` / `closed` | generators: number of yields, first values yielded (`MAX_YIELDS_CAPTURE`), time actually running (excl. the consumer between yields), closed early by the consumer |
| `cache` | `"hit"` / `"miss"` for `functools.lru_cache` / `cache` functions |
| `locals`, `locals_capture_time`, `locals_dropped`, `source` | TRACE_LOCALS, see below |
| `start_timestamp` / `end_timestamp` | root only: ISO wall-clock time of the trace |

Calls made by perf itself while capturing (a `__repr__`, a property read) are never recorded.

## How values are captured (markers)

- JSON types pass through; tuples/sets become lists; dict keys become strings.
- Project objects (classes defined in the traced codebase), dataclasses, pydantic models:
  `{"__type__": "ClassName", field: value, ...}`.
- Library objects (clients, sessions, engines): their repr string, e.g.
  `"<Whitespace ' ' at 0x7F...>"`, or `"<TypeName>"` when the repr is the default
  `<x.Y object at 0x...>`. Their internals are deliberately not walked.
- `"<TypeName>"` as a whole arg/result: value dropped -- default repr only, or larger than
  `MAX_SIZE_CAPTURE`. Absence of detail is NOT evidence the value was empty.
- `{"__spilled__": path, "size": N}` -- oversize value written to its own file in SPILL_DIR.
- `{"__class__": "pkg.Cls"}` -- a class object. `{"__type__": "function", "qualname", "module"}` -- a function.
- `{"__functions__": [names]}` -- a dict holding only functions (a registry).
- `{"__enum__": ...}` -- an enum member.
- `"<circular list>"` -- a reference back to a container being converted (a cycle).
- A repr string at depth `MAX_DEPTH` -- deeper structure was cut there.
- `"<skipped:name>"` / field missing -- dropped by `SKIP_VARIABLES`; `"<redacted>"` -- masked by `REDACT_PATTERNS`.
- `"<X truncated: size limit reached>"` -- conversion stopped at `MAX_SIZE_CAPTURE`.

## TRACE_LOCALS entries

Only for functions matching `TRACE_LOCALS` patterns (Python 3.12+). Each entry is one executed
line that changed something, positional with `0` for an empty part (trailing parts dropped):

```
[line, set, changed, deleted, globals, deleted_globals]
[12, {"i": 3}]                                     # i rebound to 3
[14, 0, {"rows": [["+", [], [{"id": 3}]]]}]        # rows changed in place: append {"id": 3}
```

- An entry is recorded when execution reaches the NEXT line, so it describes what line
  `line` did (attributed to the line that ran).
- `set`: full new values. `changed`: edit ops against the variable's previous value --
  `["+", path, items]` append, `["=", path, value]` set key/attr/index, `["-", path]` delete;
  `path` is keys/indices, `[]` = the variable itself. The previous value is the name's latest
  entry in this call, or for an argument its value in `args`/`kwargs`.
- `perf.expand_locals(trace)` (or `perf_trace.py locals`) replays them into full values.
- The node's `source` names its file in the top-level `sources`; line text is
  `sources[source]["lines"][str(line)]`.
- `locals_dropped`: entries past `MAX_LOCALS_EVENTS` (default 1000) per call.
- Changes made by a property/operator dunder on a line that can't mutate show up at the
  next line that can. Locks and other address-bearing reprs only count as changed when rebound.

## Timing semantics

- The root's own `capture_time` (its args and result) is outside its `time`: the elapsed
  time of the traced call is `time + capture_time`, and `summary` uses that.
- self = `time` (or `active_time` for generators) minus the union of the children's
  `[start, start + time + capture_time]` intervals, minus `locals_capture_time`.
- wall of a function across calls = union of its calls' intervals; cumul = plain sum.
  Under concurrency (asyncio tasks, threads) cumul and summed self exceed wall.
- Library code is not traced by default (`SCAN_VENV=False`): time spent inside a library
  call (DB driver, HTTP client, LLM SDK) is the SELF time of the project function that
  called it. A leaf with large self time is usually waiting on I/O there.
- Timings include perf's per-call overhead (sub-microsecond to microseconds) but exclude
  capture time; a debugger (debugpy) or `sys.settrace` tool slows everything further.

## Settings that shape what a trace contains

`CAPTURE_IO` (args/results on/off), `MAX_DEPTH`, `MAX_SIZE_CAPTURE`, `SPILL_DIR`,
`MAX_CAPTURES_PER_FUNCTION`, `MAX_YIELDS_CAPTURE`, `SKIP_FUNCTIONS` (whole calls hidden,
including everything under them), `SKIP_VARIABLES`, `REDACT_PATTERNS`, `TRACE_LOCALS`,
`MAX_LOCALS_EVENTS`, `LOG_FUNCTIONS` (file pruned to these names + ancestors), `MIN_PCT` /
`HOT_SPOTS` (printed report only), `SCAN_VENV` (also trace library code), `TIMEOUT`.
Set as module attributes (`perf.TRACE_LOCALS = [...]`) or per decorator
(`@perf.trace(TRACE_LOCALS=["pkg.mod.func"])`).
