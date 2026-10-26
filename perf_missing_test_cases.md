# Candidate Missing Test Cases — `perf` Tracer

> **Purpose:** Capture high-value test cases suggested by reviewing the current perf test-case note.
>
> **Important:** These are *candidate coverage gaps*, not confirmed missing tests. The note documents 20 cases and 169 checks, but each suggestion should be compared with `cases/run_all.py` and the relevant `cases/cXX_*.py` files before implementation.

## Current baseline

The existing suite documents 20 cases covering functions, recursion, closures, decorators, generators, classes, inheritance, data types, mutation and references, exceptions, imports, callbacks, asyncio, threads, processes, config, tracing imported functions, timeouts, library code, and miscellaneous shapes.

## Priority P0 — Add or verify first

### `c08_graph_shapes` — Shared references and object cycles

**Gap to investigate:** The current notes cover a cyclic list, but do not explicitly describe shared references, self-referencing dictionaries, object-to-object cycles, or mutually recursive structures.

**Test scenarios**
- One object appears in two fields of the same input structure.
- A list references itself.
- A dictionary references itself.
- Two objects reference each other.
- A shared object is also part of a cycle.

**Acceptance criteria**
- Capturing the value completes without recursion failure.
- The resulting trace JSON is valid and can be parsed.
- Shared references are not incorrectly represented as cycles.
- Cycle markers are deterministic according to the existing serialization contract.

### `c08_depth_boundary` — Exact `MAX_DEPTH` behavior

**Gap to investigate:** The current note documents an example beyond the depth limit, not all boundary values.

**Test scenarios**
- Nested value at `MAX_DEPTH - 1`.
- Nested value at `MAX_DEPTH`.
- Nested value at `MAX_DEPTH + 1`.
- Repeat with lists, dictionaries, and mixed container shapes.

**Acceptance criteria**
- The transition to `repr`/fallback representation occurs at the documented boundary.
- The output remains serializable.
- The behavior is consistent across supported container shapes.

### `c09_dedup_boundary` — Content deduplication threshold

**Gap to investigate:** The current note says content of at least 64 serialized bytes is stored in the `variables` pool, but does not document exact boundary checks.

**Test scenarios**
- Serialized values of 63, 64, and 65 bytes.
- Same large value captured repeatedly.
- Value changes between captures.
- Nested value crosses the threshold while the outer value does not.

**Acceptance criteria**
- The `__ref__` behavior matches the documented threshold exactly.
- Repeated identical content resolves correctly to the same pooled content/reference where expected.
- Changed content is stored as the new value and does not resolve to stale content.

### `c16_size_boundary` — `MAX_SIZE_CAPTURE` boundaries

**Gap to investigate:** Oversized values are covered, but the exact size boundary is not described.

**Test scenarios**
- Value just below, exactly at, and just above `MAX_SIZE_CAPTURE`.
- Run with and without `SPILL_DIR`.
- Include strings, nested dictionaries, and lists of numbers.

**Acceptance criteria**
- Inline, replacement-marker, and spill-file behavior matches the configured policy.
- Spill files contain the full captured value in the format specified by the existing implementation.
- The trace references an existing spill file when spilling succeeds.
- Small values remain inline.

### `c16_capture_io` — `CAPTURE_IO=True`

**Gap to investigate:** The notes explicitly document `CAPTURE_IO=False`, but do not describe enabled I/O capture.

**Test scenarios**
- Function writes to stdout.
- Function writes to stderr.
- Multiline output and Unicode output.
- No output is emitted.
- Compare enabled and disabled behavior.

**Acceptance criteria**
- Enabled capture follows the actual documented output format.
- Disabled capture does not add captured I/O fields.
- Capturing output does not change the function's return value or exception behavior.
- Captured data does not corrupt the trace JSON.

### `c10_exception_matrix` — `SUPPRESS_ERRORS` fallback matrix

**Gap to investigate:** The note shows a failing function annotated with `-> list`; other return annotations are not documented.

**Test scenarios**
- Functions annotated with `list`, `dict`, `int`, `str`, `bool`, `None`, optional types, and no return annotation.
- Both a handled failure and a failure propagating to the root.

**Acceptance criteria**
- Each annotation produces the documented fallback result, if supported.
- The trace records the original exception when the error is suppressed.
- Callers continue only when suppression is intended.
- Unsupported or ambiguous annotations have an explicit, stable behavior rather than an accidental result.

> Before writing exact expected values for every annotation, inspect the implementation and document the intended fallback contract.

### `c10_exception_groups` — `ExceptionGroup`

**Gap to investigate:** Exception chaining is covered, but exception groups are not mentioned.

**Test scenarios**
- `ExceptionGroup` containing two exceptions.
- Nested exception groups.
- Mixed exception types.
- An exception group raised by a nested function and propagated to the root.

**Acceptance criteria**
- The exception type and useful message are captured.
- The serialized exception is safe to write as JSON.
- Nested causes or group members are retained if the implementation contract promises them.
- Trace writing does not mask the original exception.

### `c13_async_failures` — Async failures and cancellation

**Gap to investigate:** The notes cover `gather`, `create_task`, and `TaskGroup` on successful paths, but do not document a full failure/cancellation matrix.

**Test scenarios**
- One coroutine fails under `asyncio.gather()`.
- `asyncio.gather(..., return_exceptions=True)`.
- A task is explicitly cancelled.
- One child fails in a `TaskGroup`.
- A sibling completes before another child fails.

**Acceptance criteria**
- Failed tasks and affected ancestors record the expected exception information.
- Completed sibling nodes are retained when their work finished before failure.
- Cancellation is distinguished from ordinary application exceptions according to the current contract.
- The trace is written when the root exits exceptionally.

### `c16_config_interactions` — Combined configuration settings

**Gap to investigate:** Current notes mainly show individual settings; combinations can interact in unexpected ways.

**Test scenarios**
- `SKIP_FUNCTIONS` with `LOG_FUNCTIONS`.
- `SKIP_VARIABLES` with `CAPTURE_IO`.
- `LOG_FUNCTIONS` with a nested call chain and omitted siblings.
- `MAX_SIZE_CAPTURE` with `SPILL_DIR` and `MAX_DEPTH`.

**Acceptance criteria**
- Skipped calls and their descendants are absent as specified.
- Required ancestors remain when filtering output to selected functions.
- Skipped sensitive values do not reappear in nested fields, return values, or spill files.
- Size/depth limits are applied consistently and the trace stays valid JSON.

### `c18_timeout_cleanup` — Timeout and process-state cleanup

**Gap to investigate:** Timeout behavior is covered, but cleanup and restoration behavior is not described.

**Test scenarios**
- Sync timeout inside nested calls.
- Code with a `finally` block when timeout occurs.
- A pre-existing signal handler/alarm before tracing.
- A later trace after a timed-out trace.
- Async timeout followed by another coroutine on the same event loop.

**Acceptance criteria**
- Completed nodes remain in the partial trace.
- Trace-local state is cleared after timeout.
- Previous signal/alarm state is restored where the implementation changes it.
- A subsequent trace is not polluted by the timed-out trace.
- Signal-based tests run in isolated subprocesses to avoid contaminating the test runner.

## Priority P1 — Verify next

### `c04_stacked_decorators` — Multiple decorators

Test stacked decorators with and without `functools.wraps`, including a wrapper that changes arguments and one that raises. Verify the root name, captured arguments/result, and absence of duplicate instrumentation.

### `c11_reload` — Module reload

Reload an instrumented module with `importlib.reload()`. Verify current function references, pre-reload references, and duplicate wrapper behavior. Define intended behavior before asserting it.

### `c11_rebinding` — Reassignment and aliases

Rebind a module-level function or imported alias during a trace. Verify whether the newly bound function is instrumented and how existing references behave.

### `c11_relative_imports` — Relative package imports

Exercise relative imports in a multi-level package. Assert that node names refer to the defining module and that parent-child paths remain stable.

### `c17_output_failures` — Output write errors

Use an invalid or unwritable output path. Verify whether the tracer raises a clear output error, preserves an original target exception, or follows another documented policy.

### `c14_context_isolation` — Reused thread-pool contexts

Run multiple traced jobs through a reused executor with separate copied contexts. Verify that children attach to the right root and that one job's nodes do not leak into another job's trace.

### `c14_thread_exceptions` — Worker exceptions

Raise from a function running in a thread. Check the trace output and distinguish a worker exception from an exception raised later when the caller retrieves the result.

### `c15_map_edge_cases` — Process map boundaries

Test empty input, one item, multiple chunks, and a fresh worker. Verify node counts, argument/result pairing, and behavior under spawn/forkserver where supported.

### `c15_worker_failure` — Unexpected worker termination

Have a worker exit unexpectedly, rather than raising a normal application exception. Verify parent behavior and whether completed trace data is retained.

## Cross-cutting assertions for all new cases

Where applicable, assert all of the following:

- The trace output exists when the contract says it should.
- The file parses as valid JSON.
- The expected root and call-tree paths are correct.
- Arguments are captured before the call and results at return time.
- Exception paths do not accidentally contain a normal `result` field, unless the schema specifies otherwise.
- No duplicate wrapper nodes appear after repeated/nested traces.
- `__ref__` and spill references resolve to the expected content.
- Tests are deterministic and do not depend on timing except where timing is the explicit subject.
- Concurrency tests check structural outcomes rather than brittle exact durations.

## Suggested implementation order

1. Data serialization: graph shapes, depth boundary, dedup threshold, size threshold.
2. Error paths: exception-group behavior, suppression matrix, async cancellation, timeout cleanup.
3. Config interactions and I/O capture.
4. Import/decorator/alias edge cases.
5. Thread/process edge cases.

## Definition of done

- [ ] Compare every candidate with the existing source tests and remove duplicates.
- [ ] Decide the intended behavior before writing assertions for a suspected limitation or ambiguous contract.
- [ ] Add each test to the relevant existing `cXX` case, or create a new case where separation improves clarity.
- [ ] Run the individual case during development.
- [ ] Run `uv run python cases/run_all.py --md` and review the regenerated summary.
- [ ] Confirm the full suite passes and that expected counts/limitations in the note are updated.
