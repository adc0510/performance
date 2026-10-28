# Debugging: wrong values, wrong branches, exceptions

The trace has three kinds of evidence, from cheap to rich:
1. **Calls**: which functions ran, in what order, under which caller (`tree`, `path`).
2. **Values at the boundaries**: arguments at entry and results at return (`node`, `value`, `flow`).
3. **Values inside a call**: TRACE_LOCALS, every variable after every line (`locals`, `at`, `iters`, `lines`).

Use the cheapest level that answers the question. Ask for level 3 only for the one
function where levels 1 and 2 stop explaining things.

## Method

1. **Errors first, even for "wrong result" reports.** Run `errors TRACE`. A fate of
   "caught in #N X (did not propagate further)" is a swallowed failure: the service caught
   it and returned something degraded. `replaces` means the catcher raised a different
   exception. "NOT chained" means the original traceback is lost from logs, but the trace
   still has it on that node. Run `exc-flow` to see exceptions raised and caught in a loop.
   A fate of "suppressed by perf (SUPPRESS_ERRORS)" means perf itself turned the exception
   into a default return value in that call: the code has no handler there, and the
   caller carried on with the default.
2. **Locate the bad value.** Run `value TRACE <literal> [--locals]` with the wrong output
   (a number, a status string, an id). The first hit is where the value entered the
   recording:
   - first seen as an **argument**: it came from the caller. Go up with `path` and repeat.
   - first seen as a **result or local**: the value was made inside that call. That's where to read.
   Use `--contains` for substrings, such as part of an error message.
3. **Follow the data.** Run `flow TRACE ID`. It shows where each argument was seen before
   (passed down from a caller, made by an earlier call, held by a caller's local) and where
   the result went. "bound to" on a caller line that calls the function is exact. Equal
   scalars can be coincidence, and the output says so.
4. **Compare good with bad.** When one call misbehaves and a similar one doesn't, run
   `diverge TRACE GOOD_ID BAD_ID`: it walks down to the first child pair whose values
   differ. Across two runs (yesterday's trace vs today's, passing vs failing input), use
   `vdiff A B --fn NAME --k K`.
5. **Inside the function** (needs TRACE_LOCALS on it):
   - `lines TRACE ID`: the function's source with how often each line changed something
     and the values it set. Lines marked `·` recorded no change, which does not prove they
     didn't run.
   - `iters TRACE ID [--line L]`: one row per loop iteration. Find the iteration where it
     goes wrong.
   - `at TRACE ID LINE --hit K`: every local right after the K-th execution of a line.
   - `locals TRACE ID`: the raw line-by-line log.
   If the function has no locals, ask for (or set up) a re-run with `TRACE_LOCALS`
   narrowed to it (see `capture.md`).
6. **Side effects.** Run `mutations TRACE [--fn NAME]`. It lists objects changed in place
   by a callee, either certain (the callee's own locals show a parameter edited) or
   inferred (`self` differs between method calls). Because arguments are captured at
   entry, a caller's later view of an object can differ from what the callee received.
7. **Map to source** with `where`, the exception origins, the `lines` output and
   `touched`. Then read the code and explain the mechanism: the condition, the
   missing guard, the wrong default, the shared state.

## Reading values correctly

- `args` are snapshots taken **before** the call ran, and `result` is taken at return.
  An argument the function mutates shows its state on entry.
- `"<TypeName>"`, a repr cut at `MAX_DEPTH`, `capture_skipped` and `__spilled__` all mean
  the value wasn't captured in full. Say what's missing. Don't guess.
- Project objects appear as `{"__type__": "Class", ...fields}`. Library objects appear as
  their repr (`<Session at 0x...>`): their state is not in the trace.
- Exceptions on every ancestor that re-raised them share the same text. `errors` groups
  them by origin, so count origins, not nodes.
- A trace is one execution. A bug that depends on timing (threads, tasks) may look
  different in the next trace: compare several with `vdiff`, `sdiff` or `group`.

## Concurrency bugs

- Overlapping siblings (`timeline`, `critical-path` "concurrent sibling calls") share
  whatever state they touch. Check `mutations` for objects changed by more than one of them.
- `unfinished` lists work still running when the trace closed: unawaited tasks,
  background threads, a TIMEOUT. Their results never reached the caller.
- A ThreadPoolExecutor job, a `Thread` started by project code and a ProcessPoolExecutor
  / `multiprocessing.Pool` call are traced under the code that submitted them. Long-lived
  library worker threads are not.
