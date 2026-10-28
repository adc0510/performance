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
