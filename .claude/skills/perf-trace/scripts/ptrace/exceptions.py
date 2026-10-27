"""Exceptions and control flow: errors, retries, exc-flow, unfinished."""

from __future__ import annotations

from collections import defaultdict

from .model import Trace, call_line, canon, fmt_t, short


def _catcher(n: dict) -> dict | None:
    """First ancestor that did not re-raise n's exception (None: it reached the root)."""
    exc, p = n.get("exception"), n["_parent"]
    while p is not None and p.get("exception") == exc:
        p = p["_parent"]
    return p


def _is_origin(n: dict) -> bool:
    exc = n.get("exception")
    return exc is not None and not any(c.get("exception") == exc for c in n.get("children", []))


def _error_groups(tr: Trace) -> list[dict]:
    """Exceptions grouped by where they were raised. A node is the origin when none of its
    children recorded the same exception text; its fate is the first ancestor without it."""
    groups: dict[tuple, dict] = {}
    for n in tr.nodes:
        exc = n.get("exception")
        if exc is None or any(c.get("exception") == exc for c in n.get("children", [])):
            continue
        o = n.get("exception_origin") or {}
        p, last = n["_parent"], n
        while p is not None and p.get("exception") == exc:
            last, p = p, p["_parent"]
        fate = "reached the root (the traced call raised)" if p is None else f"caught in #{p['_id']} {p['name']} (did not propagate further)"
        key = (exc, o.get("file"), o.get("line"))
        g = groups.setdefault(key, {"exception": exc, "origin": n, "where": o, "count": 0, "fate": fate, "ids": [], "top": last,
                                    "fates": defaultdict(int)})
        g["count"] += 1
        g["fates"][fate] += 1
        if len(g["ids"]) < 8:
            g["ids"].append(n["_id"])
    return sorted(groups.values(), key=lambda g: -g["count"])


def _translated_from(n: dict) -> list[dict]:
    """Children whose (different) exception n caught before raising its own."""
    return [c for c in n.get("children", []) if c.get("exception") and c.get("exception") != n.get("exception") and _catcher(c) is n]


def cmd_errors(a):
    tr = Trace(a.trace)
    groups = _error_groups(tr)
    if not groups:
        print("no exceptions recorded")
        return
    for g in groups:
        o = g["where"]
        n = g["origin"]
        print(f"{g['count']}× {g['exception']}")
        print(f"   raised in  #{n['_id']} {call_line(n, tr, 120)}")
        if o:
            print(f"   at         {o.get('file')}:{o.get('line')} in {o.get('function')}: {o.get('text')}")
        if len(g["fates"]) > 1:
            for fate, k in g["fates"].items():
                print(f"   fate       {k}× {fate}")
        else:
            print(f"   fate       {g['fate']}")
        cause = (tr.resolve(n.get("exception_debug")) or {}).get("caused_by")
        depth = 0
        while cause and depth < 10:
            print(f"   {'  ' * depth}caused by {short(cause.get('exception'), 120)}"
                  + (f"  ({cause['frames'][-1].get('file')}:{cause['frames'][-1].get('line')})" if cause.get("frames") else ""))
            cause, depth = cause.get("caused_by"), depth + 1
        for c in _translated_from(n):
            chained = (tr.resolve(n.get("exception_debug")) or {}).get("caused_by")
            print(f"   replaces   #{c['_id']} {c['name']} raised {short(c['exception'], 80)}, caught here and turned into this exception"
                  + ("" if chained else " -- NOT chained (`raise ... from None` or raised after the except block): the original"
                                       " traceback is gone from logs; the trace still has it on that node"))
        if g["count"] > 1:
            print(f"   all ids    {' '.join('#%d' % i for i in g['ids'])}{' …' if g['count'] > len(g['ids']) else ''}")
        print()


def cmd_retries(a):
    """Sibling calls of one function with equal captured args where some attempts raised."""
    tr = Trace(a.trace)
    found = 0
    for parent in tr.nodes:
        runs: dict[tuple, list] = defaultdict(list)
        for c in parent.get("children", []):
            if tr.captured(c):
                key = (c["name"], canon([c.get("args"), c.get("kwargs")]))
            else:
                key = (c["name"], None)  # args unknown: group by name only
            runs[key].append(c)
        for (name, argkey), calls in runs.items():
            fails = [c for c in calls if "exception" in c]
            if len(calls) < 2 or not fails:
                continue
            found += 1
            last = calls[-1]
            outcome = "finally succeeded" if "exception" not in last else "every attempt failed"
            s0, _ = tr.span(calls[0])
            s_last, _ = tr.span(last)
            wasted = sum(c.get("time") or 0 for c in fails)
            print(f"#{parent['_id']} {parent['name']} → {name}: {len(calls)} attempts, {len(fails)} failed, {outcome}"
                  + ("" if argkey is not None else "  (args not captured: grouped by name only)"))
            print(f"   time in failed attempts {fmt_t(wasted)} · first attempt → last attempt start {fmt_t(s_last - s0)} (includes backoff/sleep)")
            for c in calls:
                print(f"   #{c['_id']:<6} +{tr.rel(tr.span(c)[0]):>9} {fmt_t(c.get('time')):>9}  "
                      + (f"!! {short(c['exception'], 70)}" if "exception" in c else f"→ {short(tr.resolve(c.get('result')), 70, True)}"))
    if not found:
        print("no retried calls: no parent calls one function repeatedly with equal args where an attempt raised")


def cmd_exc_flow(a):
    """Exceptions raised and caught right away, over and over: exceptions used as control flow."""
    tr = Trace(a.trace)
    groups: dict[tuple, dict] = {}
    for n in tr.nodes:
        if not _is_origin(n):
            continue
        p = _catcher(n)
        if p is None:
            continue
        exc_type = n["exception"].split("(", 1)[0]
        g = groups.setdefault((n["name"], exc_type, p["name"]), {"count": 0, "time": 0.0, "parents": set(), "ids": []})
        g["count"] += 1
        g["time"] += sum(x.get("time") or 0 for x in [n])
        g["parents"].add(p["_id"])
        if len(g["ids"]) < 6:
            g["ids"].append(n["_id"])
    rows = [(k, g) for k, g in groups.items() if g["count"] >= a.min]
    if not rows:
        print(f"no exception was raised and caught {a.min}+ times in this trace")
        return
    print(f"{'count':>6} {'per catcher':>11} {'time':>9}  raised by → exception → caught in   [ids]")
    for (name, exc, catcher), g in sorted(rows, key=lambda r: -r[1]["count"]):
        per = g["count"] / len(g["parents"])
        print(f"{g['count']:6} {per:11.1f} {fmt_t(g['time']):>9}  {name} → {exc} → {catcher}   {' '.join('#%d' % i for i in g['ids'])}")
    print("\nper catcher = exceptions per call of the catching function: well above 1 means a loop that uses the exception"
          " as normal control flow. time = duration of the raising calls (the raise/unwind cost itself is inside it).")


def cmd_unfinished(a):
    tr = Trace(a.trace)
    open_nodes = [n for n in tr.nodes if n.get("time") is None]
    if not open_nodes:
        print("every call finished")
        return
    root_exc = tr.root.get("exception") or ""
    for n in open_nodes:
        p = n["_parent"]
        s, _ = tr.span(n)
        if n is tr.root:
            why = "the root itself never finished"
        elif p is not None and p.get("time") is not None:
            why = (f"outlived its caller #{p['_id']} {p['name']} (which finished): a task never awaited, a background"
                   " thread, or a generator never exhausted")
        else:
            why = "its caller was still running too"
        print(f"#{n['_id']} {n['name']}  started +{tr.rel(s)}, still running {fmt_t(tr.t_end - s)}+ when the trace closed")
        print(f"   {why}")
        print(f"   path {' → '.join(x['name'] for x in reversed(tr.ancestors(n)))}")
        if n.get("args") or n.get("kwargs"):
            print(f"   call {call_line(n, tr, 140)}")
    if "Timeout" in root_exc or "Cancelled" in root_exc:
        print(f"\nroot raised {short(root_exc, 80)}: TIMEOUT likely cut these calls off")
