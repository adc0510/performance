---
name: perf-trace
description: Investigate Python code through perf trace files -- the JSON call trees written by the `perf` tracer (`@perf.trace`, github.com/adc0510/performance), usually `perf_traces/<uuid>.json` or a path set by `perf.OUTPUT_DIR` / `OUTPUT_FILE`. Use it to find why a request or job is slow, where an exception really came from (including exceptions that were caught and swallowed), what values a function received, returned or held line by line (TRACE_LOCALS), and to verify a fix or optimization by comparing traces. Use this skill whenever the user mentions perf traces, perf_traces, a trace JSON with "variables"/"trace" keys, `@perf.trace`, TRACE_LOCALS, or asks to debug, explain or speed up code that is traced with perf -- even if they only say "check the trace" or "why is process_message slow".
---

# Investigating code with perf traces

A perf trace is a recording of one call of a `@perf.trace` function: every project
function it reached, as a tree, with wall/self time, arguments (as they were on entry),
return values, exceptions (with the line that raised them), and -- for functions listed in
`TRACE_LOCALS` -- every local variable change, line by line. It answers "what actually
happened" questions that reading code alone can't: which branch ran, which value was
wrong, which call was slow, which exception got swallowed.

Trace files are large (hundreds of KB to tens of MB, mostly captured values). Don't read
them whole -- use the bundled script, which decodes the format and prints compact,
targeted views:

```bash
python3 <skill-dir>/scripts/perf_trace.py <command> ...   # stdlib only, any python3
```

| command | use it to |
|---|---|
| `ls DIR` | list traces newest first, with root call, duration, error count, first string arg (e.g. a request/correlation id) |
| `summary TRACE` | first look: total, perf's own overhead, hot spots, slowest leaf calls, exceptions |
| `tree TRACE [--id N] [--depth D] [--min-pct P] [--no-merge]` | the call tree (same-named siblings merged as `name ×N`); `--id` zooms into a subtree |
| `hot TRACE [--sort self\|wall\|calls\|cumul]` | per-function totals |
| `find TRACE PATTERN [--errors]` | calls by name (substring or glob), with ids, args, result |
| `node TRACE ID [--frames]` | everything about one call: path, timing, named args, result, exception + origin, children |
| `errors TRACE` | each distinct exception: the call that raised it, file:line, and whether it reached the root or was caught (and where) |
| `locals TRACE ID` | TRACE_LOCALS of one call, rebuilt into full values, next to each line's source text |
| `path TRACE ID` | the chain of calls from the root to a node |
| `where TRACE ID --root SRC` / `where NAME --root SRC` | the `file:line` defining a node's function |
| `diff BEFORE AFTER` | per-function time change between two traces |

Node ids (`#N`) are stable for a file, so ids printed by one command feed the next.
Field and marker meanings are in `references/trace-format.md` -- read it when output shows
something unexpected (`<TypeName>`, `capture_skipped`, `{"__type__": ...}`, a cut-off repr).

## Workflow

1. **Pick the trace.** `ls` the directory (default `perf_traces/` relative to where the
   service started, or whatever `perf.OUTPUT_DIR` / `PERF_OUTPUT_DIR` says). Match the
   user's request by time or by the id shown in the first-string-argument column. Several
   files with the same id usually mean the same message was processed twice.
2. **`summary` first, then form a hypothesis** before drilling down. Note the perf
   overhead line: if capture is a large share of the total, timings of capture-heavy
   functions are inflated (see Pitfalls).
3. **Drill down** with `tree --id`, `find`, `node`, `errors`, `locals` until you can point
   at specific calls (`#id`), values and timings.
4. **Map to source.** `where` gives the definition; exception origins give the exact
   raising line; TRACE_LOCALS nodes name their file. Then read the code -- the trace tells
   you what happened, the code tells you why.
5. **Fix, then prove it with a new trace** (see "Verifying a change"). A change to slow or
   buggy code isn't done until a fresh trace shows the difference.

## Reading the numbers

- **self** -- time in the function itself, children excluded. **wall** -- elapsed time of
  the function across its calls, overlaps counted once. **cumul** -- sum of call
  durations. With concurrency (asyncio tasks, threads, `gather`) summed self and cumul
  exceed wall and `%tot` can pass 100%: judge what the request waited on by wall.
- **Large self time on a leaf call usually means waiting**, not computing. Library code
  (DB drivers, HTTP clients, LLM SDKs) isn't traced by default, so a query, an HTTP request
  or a model call shows up as self time of the project function that made it -- e.g.
  `LawRepository.find_law_citations` self 9 s is the SQL behind it. Optimize the query,
  batching, caching or concurrency at that call, not the Python around it. Its arguments
  (ids, query text) are in `node` and usually explain the cost.
- **Large self time on a function with children** is its own Python work (or untraced
  library calls between its children). Look for loops in its source and for `×N` children.
- **`name ×N` in the tree** is where repeated work hides: N calls to the same function
  under one parent. Compare N with the data size in the arguments -- N+1 queries, a call
  inside a loop that could be batched, a missing cache (look for `cache: miss` on
  lru_cache functions).
- **Sequential awaits that could be concurrent**: siblings under an async function whose
  intervals don't overlap (wall of the group ~= sum of their times) while they don't depend
  on each other's results.
- Remote calls (DB, LLM, search) vary a lot between runs; one trace is an anecdote. Before
  claiming a regression or improvement from timing alone, compare a few traces.

## Reading the values

- `args` are captured **before** the call runs and `result` at return: an argument the
  function mutates shows its state on entry. That makes "what did this function get"
  exact -- the most direct way to see a wrong value arriving.
- Values are snapshots with limits: `"<TypeName>"` means dropped (default repr, or larger
  than `MAX_SIZE_CAPTURE`), a repr string at depth means `MAX_DEPTH` cut it, and
  `capture_skipped` calls (past `MAX_CAPTURES_PER_FUNCTION`) have timing only. Missing
  detail is not evidence of an empty value -- say so rather than guessing.
- Library objects appear as their repr (`<Whitespace ' ' at 0x...>`); project objects as
  `{"__type__": "Class", ...fields}`. `<redacted>` / skipped fields are secrets masked on
  purpose -- never try to recover them.

## Finding bugs

- Run `errors` even when the user reports "wrong results", not a crash: services often
  catch broad exceptions and return a degraded answer. `errors` shows each exception's fate
  -- "caught in #N X (did not propagate further)" is exactly such a swallowed failure, and
  `node` on the raising call shows the inputs that triggered it.
- For wrong values, walk from the symptom backwards: find the call whose `result` is
  first wrong while its `args` look right -- the bug is inside that function. `path` shows
  how execution got there; the tree shows which branch-specific calls ran.
- When the arguments and result of a function aren't enough to see *where* inside it the
  value goes wrong, ask for (or set up) a re-run with **TRACE_LOCALS scoped to that one
  function** -- `@perf.trace(TRACE_LOCALS=["pkg.mod.func"])`, or `perf.TRACE_LOCALS = [...]`
  before the call -- then `locals TRACE ID` shows every variable after every line. Keep the
  pattern narrow: it costs time on every line of matched functions.

## Verifying a change

1. Keep the "before" trace (copy it aside; `OUTPUT_DIR` files are uuid-named, so new runs
   don't overwrite it).
2. Re-run the same input (same message, request or test) with the fix.
3. `diff BEFORE AFTER` for timing, plus `summary`/`errors`/`node` on the new trace to
   confirm the result, exception or value actually changed. Report the measured numbers,
   and mention run-to-run noise if the effect is within it.

If you can't re-run (no access to the environment, external services), say clearly that
the fix is unverified and give the user the exact steps and what to look for in the new trace.

## Pitfalls

- **perf's own overhead.** `summary` prints how much of the total was capture. If it's
  large (tens of percent), lower the cost before trusting fine-grained timings:
  `MAX_CAPTURES_PER_FUNCTION`, `MAX_DEPTH`, `MAX_SIZE_CAPTURE`, or `CAPTURE_IO=False` for a
  pure timing run. TRACE_LOCALS is the most expensive option.
- **Debuggers and other tracers** (VS Code/debugpy, coverage, cProfile) slow code down;
  traces recorded under them show where time goes, not real durations.
- **Unfinished nodes** (`time` null) were still running when the trace closed: a TIMEOUT,
  a background thread, a task the root didn't await.
- Nested functions, lambdas and functions held in registries are traced on Python 3.12+
  only; on older Pythons they're folded into their caller's self time.

## Reporting findings

Lead with the answer, then the evidence the user can check:

```
<one-sentence finding: what is slow / wrong and why>

Evidence (trace <file>):
- #<id> <call> -- <time / value / exception that shows it>
- ...
Cause: <file>:<line> -- <what the code does there>
Fix: <the change, or the options with their trade-offs>
Verified: <before -> after numbers from a new trace> | Not verified: <why, and how to check>
```

Cite node ids and `file:line` so every claim can be checked against the trace and the code.
