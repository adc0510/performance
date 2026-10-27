# Optimizing: what the time was spent on, and what can change it

The goal is to explain all of the wall time, not to find one hot spot. Work top-down and
keep a running tally of what each finding explains.

## Method

1. **Is the timing trustworthy?** In `summary`, if perf's recording cost is 20% or more of
   the total (a WARNING line), the per-function timings are inflated, worst in
   capture-heavy functions. `advise TRACE` shows where the cost went and which settings to
   use. For timing questions, re-run with a timing-only setup (`capture.md`) before
   ranking functions. A trace taken under a debugger (debugpy), coverage or cProfile is
   slow throughout.
2. **Critical path.** Run `critical-path TRACE [--id N]` to get the chain of calls the end
   of the request actually waited on, with the share it accounts for. Under concurrency,
   only work on this path sets the latency. Shortening anything off the path changes
   nothing until another chain becomes the longest. Look at:
   - **leaf calls on the path**: their time is their own work, or a wait on untraced code
     (DB driver, HTTP client, LLM SDK) made from that function. `node ID` shows the
     arguments (query, ids, URL) that usually explain the cost.
   - **parents' own time on the path**: Python between calls, or untraced library work.
     Read that function's source for loops and heavy calls.
   - **`[… of it is perf capturing values]`** marks a call whose time is mostly recording.
3. **Shape of the work** (each command reports facts; decide from the code which apply):
   - `timeline TRACE [--id N]`: a text Gantt of the children. It shows overlap, the
     concurrency level and idle stretches where the parent waited on nothing traced.
   - `serial TRACE`: siblings that ran one after another with no data flow between them.
     If they are waits (I/O), overlapping them (`asyncio.gather`, a thread pool) saves up to
     the amount shown. CPU-bound Python gains nothing. Hidden dependencies (shared state,
     rate limits, ordering) are invisible here, so check the code.
   - `nplus1 TRACE`: one call per item where only one argument changes. This costs when
     each call pays a fixed price (query, round trip). Can the callee take a list?
   - `dupes TRACE`: the same function with equal arguments, repeated. "wasted" is the most
     a cache or hoist could save. "same result: NO" means it's not pure, so don't memoize.
   - `cache TRACE`: `lru_cache` hit rate, and misses for arguments already seen (maxsize
     too small, or unstable keys).
   - `scaling TRACE [--fn NAME]`: time against input size across calls, as an exponent
     (overall, and between the two largest inputs). k≈2 points to a nested loop or a
     repeated scan.
   - `outliers TRACE`: calls far slower than their function's median. Same args but
     slower means the cause is outside the args (contention, GC, a cold cache, a remote
     hiccup).
   - `gens TRACE`: generator active time versus wall, and consumers that stopped early.
   - `payload TRACE`: functions moving or returning the largest values (copies,
     serialization, memory).
   - `hot TRACE --sort self|wall|cumul`: per-function totals, to cross-check.
4. **Account.** Sum what your findings explain against the total. If a big share is
   unexplained, say so and name where it sits (which parent's own time, which idle stretch).
5. **Many requests.** One trace is an anecdote. Run `stats DIR --root NAME` to get p50/p95
   per function, its correlation with the total, and how much more it takes in the
   slowest traces ("tail +"). That separates "always slow" from "slow when the tail is
   slow".

## Reading the numbers

- **self**: time in the function itself, children excluded. **wall**: elapsed time across
  the function's calls, with overlaps counted once. **cumul**: the sum of call durations.
  Under concurrency, summed self and cumul exceed wall and `%tot` can pass 100%, so judge
  what the request waited on by wall and by the critical path.
- Library code isn't traced by default (`SCAN_VENV=False`), so a query or HTTP call shows
  up as self time of the project function that made it.
- Self time already excludes perf's capture. Wall time of parents still includes their
  children's capture.
- Remote calls vary a lot between runs. Compare several traces before claiming a
  regression or an improvement.

## Proposing a fix

For each change, state which finding it addresses, how much time it could save at most
(from the numbers above), and what it risks: correctness with concurrency, memory for
caches, staleness. Then verify (`fix-and-verify.md`).
