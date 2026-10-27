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

## Hygiene before sharing a trace

- `secrets TRACE [--emails]`: values that look like credentials (API keys, tokens, JWTs,
  private keys, passwords in URLs, credential-named fields and args), shown masked. It
  also suggests the `SKIP_VARIABLES` / `REDACT_PATTERNS` settings that keep them out of
  future traces. The existing file still holds them: don't attach it to tickets, chats or
  git, and never paste the unmasked values.
- `advise TRACE`: what the capture cost, what got cut (`capture_skipped`,
  `locals_dropped`, `<TypeName>`, spills), and the settings for the next trace. Details in
  `capture.md`.
