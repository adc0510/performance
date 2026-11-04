"""html: one self-contained page to explore a trace in a browser -- call tree, call graph,
call details, function table and flame view. No network access and no dependencies: the trace data and the
viewer (ptrace/assets/viewer.html) are written into a single file.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .model import Trace, fmt_t, self_time

_TEMPLATE = Path(__file__).parent / "assets" / "viewer.html"
_DATA_MARK = "/*__PERF_DATA__*/"
_TITLE_MARK = "__PERF_TITLE__"

# tree structure and timing go into flat arrays; everything else about a call is its "detail"
_TREE_KEYS = {"name", "time", "start", "children", "capture_time", "_id", "_parent", "_depth"}
# keys that hold captured program values, dropped by --no-values
_VALUE_KEYS = ("args", "kwargs", "result", "yielded", "locals")

# per-call flags (bit field, mirrored in viewer.html)
EXC, RAISED, UNFINISHED, LOCALS, CACHE_HIT, CACHE_MISS, GENERATOR, SUPPRESSED = (1 << i for i in range(8))


def build(tr: Trace, values: bool = True) -> dict:
    fns: list[str] = []
    fidx: dict[str, int] = {}
    f, p, s, t, st, ct, fl, det = [], [], [], [], [], [], [], []
    t0 = tr.t0

    def r9(x):
        return None if x is None else round(x, 9)

    for n in tr.nodes:
        name = n["name"]
        if name not in fidx:
            fidx[name] = len(fns)
            fns.append(name)
        f.append(fidx[name])
        p.append(n["_parent"]["_id"] if n["_parent"] is not None else -1)
        s.append(r9(n.get("start", t0) - t0))
        t.append(r9(n.get("time")))
        st.append(r9(self_time(n)))
        ct.append(r9(n.get("capture_time") or 0))

        flags = 0
        exc = n.get("exception")
        if exc is not None:
            flags |= EXC
            if not any(c.get("exception") == exc for c in n.get("children") or ()):
                flags |= RAISED
            if "result" in n:
                flags |= SUPPRESSED
        if n.get("time") is None:
            flags |= UNFINISHED
        if n.get("locals"):
            flags |= LOCALS
        if n.get("cache") == "hit":
            flags |= CACHE_HIT
        elif n.get("cache") == "miss":
            flags |= CACHE_MISS
        if "yields" in n:
            flags |= GENERATOR
        fl.append(flags)

        d = {k: v for k, v in n.items() if k not in _TREE_KEYS}
        if not values:
            for k in _VALUE_KEYS:
                d.pop(k, None)
        det.append(d or None)

    root = tr.root
    return {
        "meta": {
            "file": str(Path(tr.path).resolve()),
            "root": root["name"],
            "total": tr.t_end - t0,
            "start_timestamp": root.get("start_timestamp"),
            "end_timestamp": root.get("end_timestamp"),
            "values": values,
        },
        "fns": fns,
        "f": f, "p": p, "s": s, "t": t, "st": st, "ct": ct, "fl": fl,
        "detail": det,
        "vars": tr.variables if values else {},
        "sources": tr.sources if values else {},
    }


def _finite(value):
    """Copy of a value with NaN / Infinity (valid for Python's json, not for JSON.parse) as strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite(v) for v in value]
    return value


def render(data: dict) -> str:
    try:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except ValueError:
        payload = json.dumps(_finite(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # "<" only occurs inside JSON strings, where < is the same character: no "</script>"
    payload = payload.replace("<", "\\u003c")
    template = _TEMPLATE.read_text(encoding="utf-8")
    head, tail = template.split(_DATA_MARK)
    title = data["meta"]["root"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return head.replace(_TITLE_MARK, title) + payload + tail


def cmd_html(a):
    tr = Trace(a.trace)
    data = build(tr, values=not a.no_values)
    out = Path(a.output) if a.output else Path(a.trace).with_suffix(".html")
    out.write_text(render(data), encoding="utf-8")
    size = out.stat().st_size
    print(f"wrote {out}  ({size / 1e6:.1f} MB, {len(tr.nodes)} calls, {len(data['fns'])} functions,"
          f" {fmt_t(data['meta']['total'])})")
    print(f"open it in a browser: file://{out.resolve()}")
    if not a.no_values:
        print("note: the page holds every captured argument, result and local value of the trace;"
              " run `secrets` on the trace before sharing it, or rebuild with --no-values")
