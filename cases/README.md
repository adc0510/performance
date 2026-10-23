# perf case suite

Executable catalogue of the Python code shapes perf can meet, and what it
actually records for each. It is the ground truth for the perf debugging skill:
every claim in [`TEST_CASES.md`](TEST_CASES.md) is a check that ran against a
real trace.

```bash
uv run python cases/run_all.py          # run everything
uv run python cases/run_all.py c13 c15  # a subset, by name prefix
uv run python cases/run_all.py --md     # also regenerate TEST_CASES.md
```

- `cNN_*.py`: one category per file. Each has a `@perf.trace` entry point and an
  `EXPECT` dict (schema in `run_all.py`) whose checks are tagged:
  - `works`: traced and captured as you'd expect;
  - `limitation`: by-design blind spot; the check documents it and its workaround;
  - `bug`: current wrong behaviour. When it's fixed, the check fails, so flip it
    to `works` and update the expectation.
- `app/`: small multi-module package for cross-module, import, inheritance,
  registry and process-pool cases.
- `out/<case>/`: per-run trace JSON plus `output.txt` (stdout/stderr). Gitignored.

To add a case, copy a similar `cNN_*.py`, write the code shape, run it, and read
`out/<case>/main.json` to write checks that pin down what perf records. Then
decide honestly whether each check is `works`, a `limitation` or a `bug`.
