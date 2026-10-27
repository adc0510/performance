---
name: perf-trace
description: Debug, fix and optimize Python code using perf trace files -- the JSON call trees written by the `perf` tracer (`@perf.trace`, github.com/adc0510/performance), usually `perf_traces/<uuid>.json` or a path set by `perf.OUTPUT_DIR` / `OUTPUT_FILE`. A trace records every project call with timing, arguments at entry, results, exceptions (including swallowed ones) and, under TRACE_LOCALS, every local variable line by line. Use it to find why a request or job is slow (critical path, N+1 calls, serialized awaits, duplicate work, cache misses, perf's own overhead), where a wrong value or exception really came from, what a function received and held, to generate a regression test from a captured call, to compare runs before/after a fix, and to map which code a request actually runs. Use this skill whenever the user mentions perf traces, perf_traces, a trace JSON with "variables"/"trace" keys, `@perf.trace`, TRACE_LOCALS, or asks to debug, explain, test or speed up code that is traced with perf -- even if they only say "check the trace" or "why is process_message slow".
---

# Debugging, fixing and optimizing with perf traces

A perf trace is the recording of one call of a `@perf.trace` function. It holds every
project function that call reached, as a tree. For each call it has wall time and self
time, the arguments as they were on entry, the return value, and any exception with the
line that raised it. For functions matched by `TRACE_LOCALS` it also has every
local-variable change, line by line, with the source text. So it can answer what actually
happened: which branch ran, which value was wrong, what the request waited on, which
exception got swallowed.

Traces are big, so don't read them whole. The bundled script decodes them and answers
targeted questions:

```bash
python3 <skill-dir>/scripts/perf_trace.py <command> ...   # stdlib only, python3.9+
python3 <skill-dir>/scripts/perf_trace.py --help          # every command, grouped by task
```

## When to use it

- **Slow request or job.** Find what the wall time was spent waiting on, and what can be
  overlapped, batched, cached or dropped.
- **Wrong result or a crash.** Find where the bad value or the exception came from,
  including exceptions that were caught and turned into a degraded answer.
- **Fixing.** Turn a captured call into a regression test, then prove the fix with a new
  trace.
- **Unfamiliar code.** See which files and functions a request actually runs, what types
  they really get, and what calls what.
- **Before sharing a trace or capturing a new one.** Check for leaked secrets and pick
  cheaper or more targeted settings.

## Core workflow

1. **Pick the trace.** Run `ls DIR`. The directory defaults to `perf_traces/` where the
   service started, or whatever `perf.OUTPUT_DIR` says. Match the user's request by time
   or by the id shown in the first-string-argument column. If several files share an id,
   run `group DIR`: it tells you whether they are the same input processed again or
   different requests that share the id. Never assume either one.
   **No traces at all**, or the app doesn't use `@perf.trace`? Don't stop and don't edit
   the app. Follow "No traces yet" in `references/capture.md`: perf from outside the
   app's dependencies, plus a driver script outside the repo that wraps the entry point.
2. **Baseline.** Run `summary TRACE` and note the total, perf's own recording share, the
   exceptions and any unfinished calls. If recording is 20% or more of the total, timings
   are inflated (see `capture.md`).
3. **Pick the task and load its reference** (table below). Follow its method until every
   claim points at a node id `#N` and a `file:line`.
4. **Read the source.** The trace tells you what happened. The code tells you why. Use
   `where`, `lines`, exception origins and `touched` to get there.
5. **Fix, then prove it.** Write a test with `to-test`/`snapshot`. Re-run the same input,
   then compare with `diff`, `sdiff` and `vdiff`. A change isn't done until a test or a
   new trace shows the difference.

## Reference guide

| Task | Reference | Load when |
|---|---|---|
| Debugging | `references/debug.md` | wrong value, wrong branch, exception, "partial"/degraded result, flaky behaviour |
| Optimizing | `references/optimize.md` | slow request/job, latency regression, "what should we optimize" |
| Fixing and verifying | `references/fix-and-verify.md` | writing the fix, regression tests, before/after comparison |
| Understanding code | `references/understand.md` | unfamiliar code path, types, diagrams, secret scan before sharing |
| Capturing the next trace | `references/capture.md` | the trace lacks what you need, or the overhead is too high |
| Trace file format | `references/trace-format.md` | output shows an unexpected marker (`<TypeName>`, `capture_skipped`, `__spilled__`, a cut-off repr) |

## Commands

| group | commands |
|---|---|
| browse | `ls` `summary` `tree` `hot` `find` `node` `path` `where` `locals` `diff` |
| data and state | `value` (every place a value appears) · `flow` (where args came from and the result went) · `diverge`/`vdiff` (two calls or two traces, down to the first difference) · `at` (all locals after the K-th run of a line) · `iters` (one row per loop iteration) · `mutations` (calls that changed objects they were handed) · `lines` (source annotated with hits and values) |
| exceptions | `errors` (origin, fate, cause chain, translation) · `retries` · `exc-flow` (exceptions used as control flow) · `unfinished` |
| time | `critical-path` · `timeline` · `serial` (independent work done in sequence) · `nplus1` · `dupes` · `cache` · `scaling` (growth with input size) · `outliers` · `gens` · `payload` |
| many traces | `stats DIR` (p50/p95, what drives the slow ones) · `sdiff` (calls that appeared or vanished) · `group DIR` |
| fix | `to-test TRACE ID --root SRC [--mock-leaves]` · `snapshot TRACE --fn NAME --root SRC` |
| understand | `types` · `touched --root SRC` · `sequence` (Mermaid) · `secrets` · `advise` |

Node ids (`#N`) are stable for a file, so an id printed by one command can be passed to
the next. Every command prints its own blind spots (values not captured, matching by equal
value rather than identity, column resolution). Carry those caveats into your conclusions.

## Work from the evidence, and account for all of it

The commands report facts. You draw the conclusions, and you must cover the whole picture
rather than stop at the first familiar thing:

- **Slow:** the critical path has to explain the request's wall time. Time it doesn't
  explain is itself a finding: own code between calls, untraced library calls, a lock, the
  event loop. Don't drop it.
- **Wrong:** follow the bad value back to the first call where it appears with good
  inputs, whatever kind of bug that turns out to be.
- **Name the cause from what the trace and the source show.** Several causes can stack up.
  Report each with its share of the time, or its place in the chain of events.

## MUST DO

- Get a baseline (`summary`) before changing anything, and measure again after every change.
- Cite node ids and `file:line` for every claim, so it can be checked.
- Change one thing per measurement, otherwise the effect can't be attributed.
- Compare several traces (`stats`) before calling a remote-call timing a regression or an
  improvement: DB, HTTP and LLM latency varies run to run.
- Say what the trace could not show: uncaptured values, timing inflated by capture, code
  outside the project.

## MUST NOT DO

- Never treat a missing or cut-off value (`<TypeName>`, `capture_skipped`, a repr at
  depth) as empty or as evidence.
- Never claim a speed-up from a single trace, or from a trace dominated by recording
  overhead.
- Never conclude "duplicate processing" from a shared id without comparing the inputs
  (`group`).
- Never try to recover `<redacted>` or skipped values, and never paste secrets that
  `secrets` finds.
- Never present a pattern-shaped guess ("probably N+1") as a finding without the command
  output that shows it.

## Reporting

Lead with the answer, then the evidence the user can check:

```
<one-sentence finding: what is slow / wrong and why>

Evidence (trace <file>):
- #<id> <call> -- <time / value / exception that shows it>
- ...
Accounted for: <how much of the wall time or of the failure chain this explains; what remains>
Cause: <file>:<line> -- <what the code does there>
Fix: <the change, or the options with their trade-offs>
Verified: <test name / before -> after numbers from a new trace> | Not verified: <why, and how to check>
```
