# Fixing and verifying

A trace holds the exact inputs a function got. That makes it a ready-made regression test,
and a before/after record for a performance change.

## Regression test from one captured call

```bash
python3 <skill-dir>/scripts/perf_trace.py to-test TRACE ID --root SRC -o tests/test_regression_x.py
python3 <skill-dir>/scripts/perf_trace.py to-test TRACE ID --root SRC --mock-leaves -o ...
```

- `--root` is the directory the code is imported from (repo root, or the dir holding the
  package). Node names map to definitions there, preferring the files the trace itself
  names.
- Arguments are rebuilt from their state at entry. Objects are rebuilt field by field
  (`_build`), so `__init__` isn't needed. The result is compared in perf's captured shape
  (`_plain`), and anything perf didn't capture in full compares as `ANY`.
- A captured exception becomes `pytest.raises(Type, match=message)`.
- `--mock-leaves` replaces the direct leaf calls (DB, HTTP and LLM wrappers, usually) with
  fakes that replay their captured results and exceptions in order. You get a unit test
  that runs without the services.
- The file ends with a TODO list for anything it couldn't rebuild: repr-only library
  objects, nested functions, bound methods. Supply those by hand.
- Tuples and sets were captured as lists. If the code depends on the exact type, adjust it.
- Call shapes it handles: methods (`self` rebuilt), classmethods (called on the class),
  properties and setters (attribute access), generators (the yielded values, then
  exhaustion, or the exception), `@contextmanager` functions (a `with` block checking the
  yielded value), async functions (`asyncio.run`). Async generators get a TODO.
- `__main__.func` names say nothing about the file. When several files under `--root`
  define it, the one defining the most of the trace's other functions is chosen and a
  `note:` on stderr names the alternatives. Check the import line.
- It refuses calls that never finished on their own (cut off by `TIMEOUT`, or still
  running when the trace closed): replaying them would hang.

What a generated test can't reproduce (it fails, or needs editing, for these):
- Values perf dropped by name (`SKIP_VARIABLES`) leave no marker, so the expected value
  lacks those keys while the real result has them.
- A repr string where `MAX_DEPTH` cut a nested value is compared as that string.
- A generator driven by `send()`/`throw()`, or an exception thrown into a context manager
  by its `with` body (TODO): only plain iteration is replayed.
- Code that depends on state outside the arguments (globals, files, time, a pool or
  client passed as a repr-only object, which becomes a TODO).

Workflow: generate the test → run it on the current code. It reproduces the captured
behaviour, so it passes as long as that behaviour still holds. Edit the expected value
to what it **should** be, so the test fails for the bug → fix → the test passes. Keep the
test.

## Replay one call on its own

```bash
python3 <skill-dir>/scripts/perf_trace.py replay TRACE ID --root SRC                    # rerun it, compare
python3 <skill-dir>/scripts/perf_trace.py replay TRACE ID --root SRC --mock children    # it alone, every callee faked
python3 <skill-dir>/scripts/perf_trace.py replay TRACE ID --root SRC --locals           # + a new trace with its locals
python3 <skill-dir>/scripts/perf_trace.py replay TRACE ID --root SRC --pdb              # debugger on its first line
python3 <skill-dir>/scripts/perf_trace.py replay TRACE ID --root SRC -o replay_x.py --no-run   # keep the script, edit args
```

`replay` runs ONE captured call again, right now, without the rest of the program: no
server, no request, no test framework. The arguments are rebuilt the same way as for
`to-test`, it runs with the project's Python (`--python`, default `SRC/.venv/bin/python`)
from `SRC`, and it compares the outcome with the recording:

- `SAME as recorded` (exit 0) / `DIFFERENT from recorded` (exit 1, with the first
  difference: `result['total']: recorded 43.63, now 65.45`) / nothing recorded to compare
  (exit 2). An exception counts as the same when its class matches and the recorded
  message is part of the new one.
- **Fakes.** `--mock leaves` (default) fakes the call's direct callees that call nothing
  traced themselves (DB, HTTP, LLM wrappers, usually); `children` fakes every direct
  callee, so only the function's own lines run; `none` runs everything for real.
  `--keep NAME` exempts a callee. Each fake answers the recorded calls in order, and is
  patched everywhere the function is referenced (`from x import fn` copies too).
- **`DIVERGED: fetch_price call 1: args[0]: recorded 'A', now 'a'`**: a fake was called
  with different arguments than in the recording, or more/fewer times (`called 2x <-
  recorded 1`). That is the first point where the replay took another path, often more
  telling than the result itself.
- **Not rebuilt.** An argument perf kept only as a repr (`<Connection>`) is passed as a
  placeholder that fails loudly the moment the function uses it, naming the value; a
  `warning:` lists them up front. Edit the script (`-o`) to pass a real one. Callees that
  are generators, context managers, properties, nested functions or defined in a script
  run for real (`not faked:` says which).
- **Refused:** calls that never finished (cut off by `TIMEOUT` or still running), calls
  with no captured arguments, generator / context-manager functions (use `to-test`), and
  functions defined in a script that runs code when imported (replaying would run the
  program).
- Everything `to-test` can't reproduce applies here too (state outside the arguments,
  `SKIP_VARIABLES`, tuples captured as lists).

Use it to: see inside a call that had no `TRACE_LOCALS` (`--locals`, then `lines` on the
new trace), step through it (`--pdb`: interactive, so the user runs it in their
terminal), or check a fix on the exact input that failed: after the change, the bad call's
replay should say `DIFFERENT` in the intended way and the good calls `SAME`.

## Golden master over many calls

```bash
python3 <skill-dir>/scripts/perf_trace.py snapshot TRACE --fn NAME --root SRC -o tests/test_snapshot_name.py
```

This writes one test per distinct captured call (args → result or exception). Use it
before refactoring or optimizing a function, to show that its behaviour didn't change.
Calls whose arguments weren't fully captured are skipped unless you pass `--keep-todo`.

## Proving a performance change

1. Keep the "before" trace. Files in `OUTPUT_DIR` are uuid-named, so new runs don't
   overwrite it.
2. Re-run the same input with the fix, using the same capture settings. Timing with
   heavier or lighter capture isn't comparable.
3. Compare:
   - `diff BEFORE AFTER`: per-function self time and call counts.
   - `sdiff BEFORE AFTER`: call paths that appeared or vanished (the removed N+1 query,
     the cache that now hits), exceptions that stopped or started, and a changed root
     result.
   - `vdiff BEFORE AFTER --fn NAME`: the same call, value by value. Confirms the result
     didn't change.
   - `critical-path AFTER`: the new chain. Another part may now dominate.
4. For remote-heavy code, take several traces of each side and compare `stats`, or at
   least report the spread. Mention run-to-run noise if the effect is within it.

If you can't re-run (no access to the environment or services), say clearly that the fix
is unverified. Give the exact steps to verify it: the input to replay, the settings, and
which command output should change and how.
