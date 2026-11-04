# Understanding code from a trace

A trace is a dynamic call graph with real values. Use it to learn an unfamiliar code path
before touching it.

- `touched TRACE --root SRC [--unreached]`: the files and functions this run executed,
  with call counts and self time, in source order. `--unreached` adds the functions in
  those files that this run didn't reach. That means not on this request's path, which is
  not the same as dead code. Names not found under the root are library, generated or
  renamed code.
- `types TRACE [--fn NAME]`: the argument and return types each function actually got,
  including container element types. Flags functions that return more than one type
  (often `None` sometimes) and arguments that arrive with different types. Only one run's
  types, though: other inputs may bring others.
- `sequence TRACE [--id N] [--depth D] [--by function|module]`: a Mermaid
  `sequenceDiagram` of the request. Repeated calls are merged (`×N`), concurrent siblings
  go in `par` blocks, and returns show time plus a short result or the exception. It can
  be pasted into Markdown or handed to the `archify` skill for a polished diagram.
- `tree`, `path`, `node`: browse the call tree. `where NAME --root SRC`: the `file:line`
  of a definition.

## An HTML page of the run

`html TRACE [-o FILE] [--no-values]` writes one self-contained file, by default next to
the trace as `<trace>.html`. It needs no network and no install, so it opens from a
server's disk or a ticket attachment. Make one when the user wants to look at the run
themselves, show it to their team, or follow the flow visually rather than read your
summary.

What each tab answers:

- **Call tree**: every call in order, with wall / self time and time bars. Repeated
  calls are merged as `×N` and expanded on demand. Selecting a call shows its arguments
  at entry, its result, its exception (where it was raised, which caller caught it) and,
  under TRACE_LOCALS, its locals line by line.
- **Call graph**, *Graph* view: one box per function, with an arrow from caller to callee
  labelled with the call count; thicker arrows carry more time. Clicking a box
  highlights the whole flow through it: the calls that led to it, up to the entry point,
  and everything it ran. **Find link** is a separate mode: pick two functions and it
  lists the real call chains between them (either direction), or the common callers
  where their chains meet.
- **Call graph**, *Timeline* view: the same functions as rows on a time axis, with a
  bar wherever each one was running and a red tick where it raised. This shows the
  phases of the run and what overlapped. To look at something too short to see, zoom
  in: drag across the time axis or the overview strip (a small copy of every lane) to
  pick a range, use the + / − buttons or keys, or double-click a function's name to
  jump to its slowest call (again for the next slowest). Zoom goes to ×10,000,000, and
  zoomed-in ticks count from the window's left edge.
- **Functions**: per-function totals with callers and callees. **Flame**: every call on
  a time axis, for where the time went.

Handing it over: give the path, plus a link to each call you cite, as
`<page>.html#n<ID>` (the same node ids as every other command). `#graph`, `#timeline`,
`#flame` and `#fns` open the page on that view. Large traces are fine:
47k calls make a 19 MB page that opens in about a second. The graph and timeline can be
downloaded as SVG, with their legend, for docs and slides.

Limits to state when you point someone at the page:

- The call graph shows the busiest N functions (30 by default, adjustable). A dashed
  arrow means the calls went through functions that are not shown.
- Each function is drawn under its main caller only: the one that spends the most time
  in it. Other callers are counted as `+N` on the box and drawn on hover or with "All
  callers".
- A generator's timeline bar covers its whole lifetime, including the time its consumer
  spent between yields (the Flame tab has the same caveat).
- The page holds every captured value. Run `secrets` before it is shared, or rebuild it
  with `--no-values` (structure and timing only; exception messages stay).

## Hygiene before sharing a trace

- `secrets TRACE [--emails]`: values that look like credentials (API keys, tokens, JWTs,
  private keys, passwords in URLs, credential-named fields and args), shown masked. It
  also suggests the `SKIP_VARIABLES` / `REDACT_PATTERNS` settings that keep them out of
  future traces. The existing file still holds them: don't attach it to tickets, chats or
  git, and never paste the unmasked values.
- `advise TRACE`: what the capture cost, what got cut (`capture_skipped`,
  `locals_dropped`, `<TypeName>`, spills), and the settings for the next trace. Details in
  `capture.md`.
