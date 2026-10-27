"""Many traces: stats, sdiff, group."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .model import Trace, aggregate, canon, diff_values, fmt_path, fmt_t, match, percentile, short


def _files(d: str, recursive: bool) -> list[Path]:
    p = Path(d)
    if p.is_file():
        return [p]
    files = p.rglob("*.json") if recursive else p.glob("*.json")
    return sorted(files, key=lambda f: f.stat().st_mtime)


def _load(f: Path) -> Trace | None:
    try:
        return Trace(str(f))
    except (SystemExit, OSError, ValueError):
        return None


def _pearson(xs, ys) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else None


def cmd_stats(a):
    per_trace = []  # (file, total, {fn: wall})
    roots = Counter()
    for f in _files(a.dir, a.recursive):
        tr = _load(f)
        if tr is None:
            continue
        roots[tr.root["name"]] += 1
        if a.root and not match(tr.root["name"], a.root):
            continue
        total = tr.root.get("time") or 0
        per_trace.append((f, total, {k: s["wall"] for k, s in aggregate(tr).items()}))
        del tr
    if not a.root and len(roots) > 1:
        print("several entry points in this directory; pick one with --root:")
        for name, k in roots.most_common():
            print(f"  {k:5}  {name}")
        return
    if len(per_trace) < 2:
        print(f"need 2+ traces, found {len(per_trace)}")
        return
    totals = [t for _, t, _ in per_trace]
    print(f"{len(per_trace)} traces · total p50 {fmt_t(percentile(totals, .5))} · p95 {fmt_t(percentile(totals, .95))}"
          f" · max {fmt_t(max(totals))} · min {fmt_t(min(totals))}")
    fns = set().union(*(w.keys() for _, _, w in per_trace))
    rows = []
    for fn in fns:
        vals = [w.get(fn, 0.0) for _, _, w in per_trace]
        r = _pearson(vals, totals)
        rows.append((fn, percentile(vals, .5), percentile(vals, .95), max(vals), sum(1 for v in vals if v), r))
    # which function grows the most between typical and slow traces
    order = sorted(range(len(per_trace)), key=lambda i: totals[i])
    slow = order[-max(1, len(order) // 10):]
    mid = order[: max(1, len(order) // 2)]
    gain = {fn: sum(per_trace[i][2].get(fn, 0) for i in slow) / len(slow) - sum(per_trace[i][2].get(fn, 0) for i in mid) / len(mid)
            for fn in fns}
    key = {"p95": lambda r: -r[2], "p50": lambda r: -r[1], "corr": lambda r: -(r[5] or 0), "tail": lambda r: -gain[r[0]]}[a.sort]
    print(f"\n{'p50 wall':>9} {'p95 wall':>9} {'max':>9} {'in':>5} {'corr':>5} {'tail +':>9}  function")
    for fn, p50, p95, mx, present, r in [r for r in sorted(rows, key=key) if r[4]][: a.n]:
        print(f"{fmt_t(p50):>9} {fmt_t(p95):>9} {fmt_t(mx):>9} {present:5} {('%.2f' % r) if r is not None else '  -':>5}"
              f" {('+' if gain[fn] >= 0 else '-') + fmt_t(abs(gain[fn])):>9}  {fn}")
    print(f"\nwall = the function's elapsed time within one trace. corr = correlation of that with the trace total"
          f" (near 1: when this is slow the request is slow). tail + = how much more it takes in the slowest"
          f" {len(slow)} trace(s) than in the faster half: what makes the slow requests slow.")
    worst = per_trace[order[-1]]
    print(f"slowest: {worst[0]} ({fmt_t(worst[1])})")


def _signature(tr: Trace) -> tuple[Counter, Counter]:
    paths, excs = Counter(), Counter()
    for n in tr.nodes:
        names = [n["name"]] + [p["name"] for p in tr.ancestors(n)]
        paths[" → ".join(reversed(names[:4]))] += 1
        if n.get("exception") and not any(c.get("exception") == n["exception"] for c in n.get("children", [])):
            excs[(n["name"], n["exception"].split("(", 1)[0])] += 1
    return paths, excs


def cmd_sdiff(a):
    ta, tb = Trace(a.a), Trace(a.b)
    pa, ea = _signature(ta)
    pb, eb = _signature(tb)
    print(f"calls {len(ta.nodes)} → {len(tb.nodes)}   total {fmt_t(ta.root.get('time'))} → {fmt_t(tb.root.get('time'))}")
    rows = [(k, pa.get(k, 0), pb.get(k, 0)) for k in set(pa) | set(pb) if pa.get(k, 0) != pb.get(k, 0)]
    rows.sort(key=lambda r: (-(r[1] == 0 or r[2] == 0), -abs(r[2] - r[1])))
    if rows:
        print(f"\n{'before':>7} {'after':>7}  call path (up to 3 callers)")
        for k, x, y in rows[: a.n]:
            tag = "  NEW" if x == 0 else "  GONE" if y == 0 else ""
            print(f"{x:7} {y:7}  {short(k, 140)}{tag}")
        if len(rows) > a.n:
            print(f"... {len(rows) - a.n} more (use -n)")
    else:
        print("same call structure (every call path has the same count)")
    erows = [(k, ea.get(k, 0), eb.get(k, 0)) for k in set(ea) | set(eb) if ea.get(k, 0) != eb.get(k, 0)]
    if erows:
        print(f"\nexceptions raised (before → after)")
        for (fn, exc), x, y in sorted(erows, key=lambda r: -abs(r[2] - r[1])):
            print(f"  {x} → {y}  {exc} in {fn}")
    ra, rb = ta.resolve(ta.root.get("result")), tb.resolve(tb.root.get("result"))
    d = diff_values(ra, rb, limit=8)
    if d:
        print("\nroot result differs:")
        for p, x, y in d:
            print(f"  result{fmt_path(p)}  {short(x, 50, True)} → {short(y, 50, True)}")
    print("\npaired with `diff` (timing) this shows whether a change altered what runs: fewer calls, a removed"
          " query, an exception that stopped or started.")


def _hint(root: dict) -> str:
    for v in list((root.get("kwargs") or {}).values()) + list(root.get("args") or []):
        if isinstance(v, str) and not v.startswith("<"):
            return v
    return ""


def cmd_group(a):
    groups: dict[tuple, list] = defaultdict(list)
    for f in _files(a.dir, a.recursive):
        tr = _load(f)
        if tr is None:
            continue
        ident = _key_value(tr, a.key) if a.key else _hint(tr.root)
        if not ident:  # no string id: the whole input is the id
            ident = canon([tr.resolve(tr.root.get("args")), tr.resolve(tr.root.get("kwargs"))])
        key = (tr.root["name"], ident)
        groups[key].append((f, tr.root.get("start_timestamp"), tr.root.get("time"),
                            tr.resolve(tr.root.get("args")), tr.resolve(tr.root.get("kwargs")), tr.resolve(tr.root.get("result"))))
        del tr
    multi = {k: v for k, v in groups.items() if len(v) > 1}
    print(f"{sum(len(v) for v in groups.values())} traces · {len(groups)} distinct (entry point, id) · {len(multi)} ids seen more than once")
    for (root, ident), items in sorted(multi.items(), key=lambda kv: -len(kv[1])):
        first = items[0]
        same_input = all(canon([x[3], x[4]]) == canon([first[3], first[4]]) for x in items[1:])
        same_result = all(canon(x[5]) == canon(first[5]) for x in items[1:])
        verdict = ("identical input: the same request processed again (retry, redelivery, double submit)" if same_input
                   else "DIFFERENT input under one id: separate requests that share the id, not a duplicate")
        print(f"\n{root}  id {short(ident, 50, True)}  ×{len(items)}  -- {verdict}"
              + ("" if not same_input else ("; same result" if same_result else "; results differ")))
        for f, ts, t, *_ in items:
            print(f"   {ts}  {fmt_t(t):>9}  {f}")
        if not same_input:
            d = diff_values({"args": first[3], "kwargs": first[4]}, {"args": items[1][3], "kwargs": items[1][4]}, limit=6)
            for p, x, y in d:
                print(f"   differs at {fmt_path(p)[1:]}: {short(x, 50, True)} vs {short(y, 50, True)}")
    print("\nthe id is the root's first string argument, or --key PATH into its args (e.g. 0.headers.correlation_id),"
          " or the whole input when there is no string argument."
          " Only the root's captured args are compared: inputs it read from elsewhere (env, DB) are not.")


def _key_value(tr: Trace, path: str):
    v = {"args": tr.resolve(tr.root.get("args")), "kwargs": tr.resolve(tr.root.get("kwargs"))}
    v = v["args"] if path.split(".")[0].isdigit() else v
    for part in path.split("."):
        try:
            v = v[int(part)] if isinstance(v, list) else v[part]
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return json.dumps(v, default=str) if not isinstance(v, str) else v
