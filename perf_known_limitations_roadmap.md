# `perf` Tracer — Known Limitations and Feature-Test Roadmap

> This note reorganizes limitations already documented in the current test-case note into possible future feature work. It is not a commitment that every item should be implemented. Prioritize based on the tracer's intended users and compatibility requirements.

## How to use this roadmap

For every selected feature:

1. Decide whether the current limitation is acceptable or should be removed.
2. Write an acceptance test before changing the implementation.
3. Define naming, parent-child relationships, argument/result capture, timing, and exception semantics.
4. Preserve current behavior unless the change is intentional and documented.
5. Update the test notes so a previous limitation is not accidentally kept as an expected behavior after it has been fixed.

## Candidate feature work

### F1 — Generator and context-manager execution tracing

**Current documented behavior:** A generator node represents generator creation; work during iteration appears under the consumer. A `@contextmanager` call captures object creation, while setup/teardown and `with`-body calls attach to the containing function.

**Potential target behavior**
- Account for time spent advancing a generator.
- Capture exceptions raised during iteration.
- Define whether yielded values are captured and, if so, how many.
- Represent context-manager setup and teardown consistently.

**Acceptance tests**
- Generator produces several values and completes normally.
- Generator raises before the first yield and after a later yield.
- Generator is only partially consumed.
- Generator is explicitly closed.
- Context-manager setup fails, body fails, or teardown fails.
- `yield from` delegates to another generator.

**Design question:** Should execution time belong to the generator node, the consumer, or both? Avoid double-counting overlapping time.

### F2 — Properties, cached properties, and dunder methods

**Current documented behavior:** `@property`, setters, `cached_property`, and dunder methods are not represented as independent nodes; calls performed inside them attach to the caller.

**Potential target behavior**
- Instrument selected property getters/setters and dunder methods.
- Avoid instrumenting harmless Python protocol calls so aggressively that traces become noisy or recursive.
- Keep special-method names stable and distinguish property access from ordinary methods.

**Acceptance tests**
- Property getter and setter, including a setter that raises.
- `cached_property` on cache miss and cache hit.
- `__enter__` / `__exit__` and an exception inside a `with` block.
- `__len__`, `__iter__`, and a user-defined operator, if these are in scope.

**Design question:** Which special methods are included by default, and which require opt-in?

### F3 — Pre-registered callbacks and registry dispatch

**Current documented behavior:** Registries and instance callbacks stored before instrumentation may retain original function references and therefore may not produce their own trace nodes.

**Potential target behavior**
- Trace callbacks invoked through registry tables, decorator registries, and stored instance attributes.
- Avoid mutating third-party registries unexpectedly.
- Handle the same callback registered in multiple places without double wrapping.

**Acceptance tests**
- Callback registered before trace starts.
- Callback registered after instrumentation.
- Callback stored on an instance before tracing.
- Callback is called directly and through a dispatcher.
- Callback is unregistered or replaced during execution.

**Design question:** Is registration-time wrapping, call-time interception, or explicit instrumentation required?

### F4 — Thread context propagation

**Current documented behavior:** Work on a new thread or ordinary `ThreadPoolExecutor` worker can be lost because the trace context is not propagated. The note documents `contextvars.copy_context().run` as a workaround.

**Potential target behavior**
- Offer an opt-in or documented automatic context-propagation helper.
- Prevent work in one task from attaching to another task's root.
- Specify behavior for nested executors and reused worker threads.

**Acceptance tests**
- `threading.Thread` target.
- `ThreadPoolExecutor.submit` and `.map`.
- Multiple traced roots submitted to a shared pool.
- Worker reuses a thread for successive jobs.
- A worker exception and a worker that outlives the root.

**Design question:** Should context propagation be automatic, opt-in, or left to the caller?

### F5 — `multiprocessing.Pool` integration

**Current documented behavior:** `multiprocessing.Pool` worker calls are not included in the trace, whereas the suite covers `ProcessPoolExecutor` paths.

**Potential target behavior**
- Collect worker traces for supported pool operations.
- Associate each worker result/exception with the corresponding submitted item.
- Support the chosen start methods explicitly.

**Acceptance tests**
- `Pool.apply`, `map`, and asynchronous variants that are in scope.
- Empty and multi-item jobs.
- Worker exception and abrupt worker termination.
- Spawn/forkserver behavior where supported by the environment.
- Cleanup on pool close, terminate, and context-manager exit.

**Design question:** How are worker trace payloads returned to and merged by the parent process?

### F6 — Local closures and lambda tracing

**Current documented behavior:** Local nested functions, closures held in local variables, and lambdas are not directly instrumented. Public module-level function references are traced.

**Potential target behavior**
- Assign stable identifiers to local functions without relying on ambiguous names.
- Preserve closure behavior and avoid unexpectedly rewriting function objects.
- Trace local callbacks where they are called, if feasible.

**Acceptance tests**
- Nested function called during the same outer call.
- Factory returns a closure stored locally and called.
- Two closures from the same factory with different captured values.
- Lambda invokes an instrumented module function.
- Comprehension invokes a callback repeatedly.

**Design question:** What identifier distinguishes closures created by the same factory and sharing the same `__qualname__`?

### F7 — Caching decorator instrumentation

**Current documented behavior:** `functools.lru_cache` and `functools.cache` wrappers are not represented as ordinary Python function nodes; internal project-function calls appear on cache misses.

**Potential target behavior**
- Optionally represent cache wrapper calls and cache-hit/miss behavior.
- Avoid tracing the same underlying function twice.
- Make timing semantics clear when a cache hit bypasses the function body.

**Acceptance tests**
- First call/cache miss.
- Repeated call/cache hit.
- Cache clear followed by another miss.
- Recursive cached function.
- Cached function raises and is called again.

**Design question:** Should wrapper overhead and cache-hit events appear as nodes, metrics, or neither?

### F8 — Better class and enum capture representation

**Current documented behavior:** A class object used as `cls` can serialize as its full class dictionary; enum serialization can include the whole enum class. `NamedTuple` field names are lost in a list representation.

**Potential target behavior**
- Use a compact, stable class reference representation.
- Preserve `NamedTuple` field names if that aligns with the serialization contract.
- Keep enum serialization compact and JSON-safe.

**Acceptance tests**
- `@classmethod` input.
- Enum member and enum class passed separately.
- `NamedTuple` as a return value, nested value, and method receiver.
- Two classes with identical names in different modules.
- Slotted instances and dataclass instances.

**Design question:** Should serialized class/object identities include module and qualified name to avoid collisions?

### F9 — Imported/reloaded module and instrumentation lifecycle

**Current documented behavior:** Modules imported during a trace may not have free functions traced until a subsequent trace. Some class methods are patched lazily. Library patching may persist after scan settings change.

**Potential target behavior**
- Define when newly imported modules become eligible for tracing.
- Avoid duplicate wrapping across reloads and repeated trace sessions.
- Provide predictable cleanup or document persistent instrumentation.

**Acceptance tests**
- Import module during a trace, then call its function.
- Call the same function in a second trace.
- Reload an already instrumented module.
- Turn library scanning on and off within one process.
- Start nested traces involving newly imported code.

**Design question:** Is instrumentation intentionally process-global, trace-scoped, or reversible?

## Suggested prioritization

| Priority | Candidate features | Reason |
|---|---|---|
| High | Thread context propagation; generator iteration and exceptions; exception/cancellation completeness | Missing execution can make performance traces incomplete or misleading. |
| Medium | Pre-registered callbacks; import/reload lifecycle; properties/dunders | Improves visibility in common application patterns but requires careful wrapper semantics. |
| Medium | Compact class/enum serialization; closure/lambda instrumentation | Improves clarity and coverage of values/callables. |
| Depends on demand | `multiprocessing.Pool`; cache wrapper nodes | Cross-process merging and cache timing choices may add complexity. |

## Feature completion checklist

- [ ] Behavior and compatibility expectations agreed before implementation.
- [ ] Acceptance test added and fails for the current documented limitation.
- [ ] Implementation passes the new focused test.
- [ ] Existing tests for adjacent behavior continue to pass.
- [ ] Timing and parent-child relationships are checked for double-counting or misplaced nodes.
- [ ] The Markdown case note has been regenerated or updated.
- [ ] Any fixed limitation is reclassified from `limitation` to `works` (or removed/replaced with the new expected behavior).
