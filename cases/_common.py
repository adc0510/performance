"""Shared setup for the case scripts.

Every case calls :func:`setup` before its traced entry point runs, so each
trace lands at ``cases/out/<case>/<OUTPUT_FILE>.json`` where ``run_all.py``
can check it. Output/sending config only; anything a case is *about* (CAPTURE_IO,
SKIP_FUNCTIONS, …) is set by the case itself.
"""

from pathlib import Path

import perf

OUT = Path(__file__).parent / "out"


def setup(case_file: str, **config) -> None:
    perf.OUTPUT_DIR = str(OUT / Path(case_file).stem)
    perf.OUTPUT_FILE = "main"
    perf.ENDPOINT_URL = None
    perf.HOT_SPOTS = 0
    for key, value in config.items():
        setattr(perf, key, value)


def locals_entries(node: dict, fn) -> list:
    """A node's TRACE_LOCALS entries as tuples with line numbers RELATIVE to
    the function's `def` (the trace holds real file lines; relative ones keep
    tests stable when a case file is edited). Each tuple is
    (line, text, {set}) plus ("deleted", [...]) / ("globals", {...}) when present."""
    import inspect

    base = inspect.unwrap(fn).__code__.co_firstlineno
    out = []
    for e in node.get("locals", []):
        item = [e["line"] - base, e.get("text"), e.get("set")]
        if "deleted" in e:
            item += ["deleted", e["deleted"]]
        if "globals" in e:
            item += ["globals", e["globals"]]
        out.append(tuple(item))
    return out


def trace_nodes(path: str) -> list:
    """Every node of a written trace, with {__ref__} values resolved."""
    import json

    from run_all import resolve, walk, with_text

    d = with_text(json.load(open(path)))
    return [resolve(n, d["variables"]) for n in walk(d["trace"])]
