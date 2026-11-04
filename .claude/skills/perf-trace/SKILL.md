---
name: perf-trace
argument-hint: "[help|summary|slow|debug|errors|replay|attach|detach|draw-diagram|sequence|explain|test|compare|share|setup|advise] [trace] [details]"
description: Debug, fix and optimize Python code using perf trace files -- the JSON call trees written by the `perf` tracer (`@perf.trace`, github.com/adc0510/performance), usually `perf_traces/<uuid>.json` or a path set by `perf.OUTPUT_DIR` / `OUTPUT_FILE`. A trace records every project call with timing, arguments at entry, results, exceptions (including swallowed ones) and, under TRACE_LOCALS, every local variable line by line. Use it to find why a request or job is slow (critical path, N+1 calls, serialized awaits, duplicate work, cache misses, perf's own overhead), where a wrong value or exception really came from, what a function received and held, to generate a regression test from a captured call, to compare runs before/after a fix, to map which code a request actually runs, and to export a trace as an interactive offline HTML page (call tree, call graph with timeline, flame) for a person to explore or share. Use this skill whenever the user mentions perf traces, perf_traces, a trace JSON with "variables"/"trace" keys, `@perf.trace`, TRACE_LOCALS, or asks to debug, explain, test or speed up code that is traced with perf -- even if they only say "check the trace" or "why is process_message slow".
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

## Options (`/perf-trace <option> ...`)

When the user types `/perf-trace` with words after it, they reach you as `ARGUMENTS:` at
the end of this file. The first word picks the task below; the rest is the trace and
details. With no arguments, work out the task from the conversation as usual. If the
first word is not an option, treat all of it as the request in plain words.

**Which trace:** a path or uuid among the arguments wins. Otherwise use the newest file
from `ls` on `perf_traces/` (or `perf.OUTPUT_DIR`), and say which one you picked. If
there is none, offer `setup`.

| option | what to do |
|---|---|
| `help` | List these options with one line each, and the trace you would use by default. Run nothing else. |
| `summary [trace]` | Core workflow step 2: `summary`, then report the totals, perf's recording share, exceptions and unfinished calls. |
| `slow [trace] [function]` | Optimize: follow `references/optimize.md`, scoped to the function if given (`find`, then `critical-path --id`). |
| `debug [trace] <what's wrong>` | Debug: follow `references/debug.md` with the user's description as the symptom. |
| `errors [trace]` | `errors` (and `exc-flow`, `retries` when they apply): each exception's origin, fate and cause, swallowed ones included. |
| `replay [trace] <id or function> [what to check]` | Run that ONE captured call again on its own with `replay` (`references/fix-and-verify.md`): find the id with `find` if a name is given (its first call, or the failing one), replay it, report SAME/DIFFERENT, the first difference and any `DIVERGED` callee. Add `--locals` to see inside it; for `--pdb`, give the user the command to run in their own terminal (it's interactive). |
| `attach <pid or process> <function> [calls/seconds]` | Trace a process that is already running, without restarting it: "The app is already running" in `references/capture.md`. Confirm the pid (`ps`/`pgrep -af python`) and the function with the user before attaching, then read the traces it writes. |
| `detach <pid> [function]` | Stop a live attach early: `python -m perf detach <pid> [function]` with the target's Python. |
| `draw-diagram [trace] [graph\|timeline\|flame\|tree\|sequence]` | Write the page with `html` and give the link to the view asked for (default `#graph`), plus `#n<ID>` links to the calls worth looking at first. `sequence` writes the Mermaid diagram with `sequence` instead. State the page's limits (`references/understand.md`). |
| `sequence [trace] [function]` | Mermaid sequence diagram with `sequence` (`--id` of the function's slowest call if given). |
| `explain [trace] [function]` | Understand: follow `references/understand.md` (`touched`, `types`, `tree`, `path`) and explain what the run does, in order, citing node ids. |
| `test [trace] <id or function>` | Regression test: `to-test` for a call id, `snapshot --fn` for a function (`references/fix-and-verify.md`). |
| `compare <trace A> <trace B>` | Before/after: `diff`, `sdiff` and `vdiff`, and say what changed and by how much. |
| `share [trace]` | Before sharing: `secrets`; if anything is found, also write `html --no-values` and recommend it over the full page. |
| `setup` | No traces yet: follow "No traces yet" in `references/capture.md` for the current repo. |
| `advise [trace]` | `advise`: what capture cost and what was cut, and the settings for the next trace. |

Every option still follows the rules below: cite node ids, account for the whole picture,
and say what the trace could not show.

## When to use it

- **Slow request or job.** Find what the wall time was spent waiting on, and what can be
  overlapped, batched, cached or dropped.
- **Wrong result or a crash.** Find where the bad value or the exception came from,
  including exceptions that were caught and turned into a degraded answer.
- **Fixing.** Turn a captured call into a regression test, then prove the fix with a new
  trace.
- **Unfamiliar code.** See which files and functions a request actually runs, what types
  they really get, and what calls what.
- **Showing the run to people.** `html TRACE` writes one offline page to explore the run
  in a browser. Link the calls you cite as `<page>.html#n<ID>` so the reader lands on them.
- **Before sharing a trace or capturing a new one.** Check for leaked secrets and pick
  cheaper or more targeted settings.

## Core workflow

1. **Pick the trace.** Run `ls DIR`. The directory defaults to `perf_traces/` where the
   service started, or whatever `perf.OUTPUT_DIR` says. Match the user's request by time
   or by the id shown in the first-string-argument column. If several files share an id,
   run `group DIR`: it tells you whether they are the same input processed again or
   different requests that share the id. Never assume either one.
   **No traces at all**, or the app doesn't use `@perf.trace`? Don't stop and don't edit
   the app. Follow "No traces yet" in `references/capture.md`: perf installed in `.venv`
   only (`uv pip install`, then `uv sync --inexact`), a `perf_driver.py` that wraps the
   entry point, and both excluded from git via `.git/info/exclude`. If the app is
   already running and shouldn't be restarted, attach to it instead:
   `python -m perf attach <pid> mod:func` (see "already running" in `capture.md`).
2. **Baseline.** Run `summary TRACE` and note the total, perf's own recording share, the
   exceptions and any unfinished calls. If recording is 20% or more of the elapsed time,
   timings are inflated (see `capture.md`). An exception "suppressed by perf" means
   `SUPPRESS_ERRORS` turned it into a default return value.
3. **Pick the task and load its reference** (table below). Follow its method until every
   claim points at a node id `#N` and a `file:line`.
4. **Read the source.** The trace tells you what happened. The code tells you why. Use
   `where`, `lines`, exception origins and `touched` to get there.
5. **Fix, then prove it.** `replay TRACE ID` the bad call after the fix: it reruns just that
   call from its recorded arguments and says how the result changed. Write a test with
   `to-test`/`snapshot`. Re-run the same input, then compare with `diff`, `sdiff` and `vdiff`. A change isn't done until a test or a
   new trace shows the difference.

## Reference guide

| Task | Reference | Load when |
|---|---|---|
| Debugging | `references/debug.md` | wrong value, wrong branch, exception, "partial"/degraded result, flaky behaviour |
| Optimizing | `references/optimize.md` | slow request/job, latency regression, "what should we optimize" |
| Fixing and verifying | `references/fix-and-verify.md` | writing the fix, regression tests, before/after comparison |
| Understanding code | `references/understand.md` | unfamiliar code path, types, diagrams, an HTML page of the run, secret scan before sharing |
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
| fix | `to-test TRACE ID --root SRC [--mock-leaves]` · `snapshot TRACE --fn NAME --root SRC` · `replay TRACE ID --root SRC [--mock leaves\|children\|none] [--locals] [--pdb]` (run one captured call again, alone, and compare) |
| understand | `types` · `touched --root SRC` · `sequence` (Mermaid) · `html` (one offline page: call tree, call graph, flame) · `secrets` · `advise` |

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
  outside the project, calls pruned by `LOG_FUNCTIONS` (their time sits in the kept
  parents' self time, unmarked).

## MUST NOT DO

- Never treat a missing or cut-off value (`<TypeName>`, `capture_skipped`, a repr at
  depth) as empty or as evidence. Since perf 0.12.0 `capture_skipped` is common (first
  20 calls per function captured by default): when you need such a call's values, re-run
  with `MAX_CAPTURES_PER_FUNCTION=None`.
- Never claim a speed-up from a single trace, or from a trace dominated by recording
  overhead.
- Never conclude "duplicate processing" from a shared id without comparing the inputs
  (`group`).
- Never try to recover `<redacted>` or skipped values, and never paste secrets that
  `secrets` finds.
- Never share an `html` page built with values (the default) before running `secrets` on
  the trace: it carries every captured value, like the trace itself. Otherwise rebuild
  it with `--no-values`.
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
Page: <path>.html -- open #n<ID> for each call above (only if you wrote one with `html`)
```
