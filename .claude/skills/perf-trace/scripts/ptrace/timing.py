"""Time and concurrency: critical-path, timeline, serial, nplus1, dupes, cache, scaling, outliers, gens, payload."""

from __future__ import annotations

import json
import math
from collections import defaultdict

from .model import (Trace, call_line, canon, canon_shared, diff_values, fmt_path, fmt_t, is_marker, is_trivial, named_args,
                    percentile, self_time, short, walk)

EPS = 1e-7


def _end(tr: Trace, n: dict) -> float:
    return tr.span(n)[1]


def _start(tr: Trace, n: dict) -> float:
    return tr.span(n)[0]


# ── critical path ────────────────────────────────────────────────────────────


def _critical_segments(tr: Trace, p: dict) -> list[tuple]:
    """Walk back from p's end: each step takes the child that ended last before the cursor.
    Returns [("call", child) | ("gap", start, end)] in time order, covering p's whole duration."""
    s = _start(tr, p)
    e = s + (p.get("time") if p.get("time") is not None else tr.t_end - s)
    kids = [c for c in p.get("children", []) if _start(tr, c) >= s - EPS and (c.get("time") is not None or p.get("time") is None)]
    segs, cursor = [], e
    while cursor > s + EPS:
        cands = [c for c in kids if min(_end(tr, c), e) <= cursor + EPS and _start(tr, c) < cursor - EPS]
        if not cands:
            segs.append(("gap", s, cursor))
            break
        c = max(cands, key=lambda c: min(_end(tr, c), e))
        ce = min(_end(tr, c), e)
        if ce < cursor - EPS:
            segs.append(("gap", ce, cursor))
        segs.append(("call", c))
        cursor = max(_start(tr, c), s)
        kids = [k for k in kids if k is not c]
    segs.reverse()
    return segs


def cmd_critical_path(a):
    tr = Trace(a.trace)
    top = tr.node(a.id) if a.id is not None else tr.root
    total = top.get("time") if top.get("time") is not None else tr.t_end - _start(tr, top)
    leaves = []  # (node, duration) of leaf calls on the path
    gaps = defaultdict(float)  # parent -> own time on the path
    print(f"critical path of #{top['_id']} {top['name']} ({fmt_t(total)}): the chain of calls the end of the call waited on")
    print(f"{'start':>10} {'on path':>9}  call")

    def show(p, depth, indent):
        for seg in _critical_segments(tr, p):
            if seg[0] == "gap":
                gaps[p["_id"]] += seg[2] - seg[1]
                continue
            c = seg[1]
            dur = min(_end(tr, c), tr.t_end) - _start(tr, c)
            pct = dur / total * 100 if total else 0
            siblings = [k for k in p.get("children", []) if k is not c and _start(tr, k) < _end(tr, c) and _end(tr, k) > _start(tr, c)]
            if pct >= a.min_pct:
                extra = f"   ({len(siblings)} concurrent sibling calls off the path)" if siblings else ""
                cap = c.get("capture_time") or 0
                if cap >= dur * 0.2:
                    extra = f"   [{fmt_t(cap)} of it is perf capturing values]" + extra
                print(f"{'+' + tr.rel(_start(tr, c)):>10} {fmt_t(dur):>9}  {indent}#{c['_id']} {call_line(c, tr, 90)}{extra}")
            if c.get("children") and depth < a.depth:
                show(c, depth + 1, indent + "  ")
            else:
                leaves.append((c, dur))
    show(top, 1, "")
    in_leaves = sum(d for _, d in leaves)
    skipped = [n for n in top.get("children", []) if n.get("time") is None]
    if skipped and top.get("time") is not None:
        print(f"(not waited on: {len(skipped)} unfinished child call(s), e.g. #{skipped[0]['_id']} {skipped[0]['name']} -- see `unfinished`)")
    in_gaps = sum(gaps.values())
    print(f"\naccounted: {fmt_t(in_leaves)} ({in_leaves / total * 100 if total else 0:.0f}%) inside leaf calls on the path"
          f" + {fmt_t(in_gaps)} ({in_gaps / total * 100 if total else 0:.0f}%) in parents' own code between calls"
          f" = {fmt_t(in_leaves + in_gaps)} of {fmt_t(total)}")
    if gaps:
        print("own time of parents on the path (their code between calls, or untraced library calls they made):")
        for nid, g in sorted(gaps.items(), key=lambda kv: -kv[1])[:8]:
            if g / (total or 1) * 100 >= a.min_pct:
                print(f"  {fmt_t(g):>9}  #{nid} {tr.nodes[nid]['name']}")
    print("\nshortening a call OFF this path does not shorten the request; shortening one ON it does, until another"
          " chain becomes the longest. Leaf time is the call's own work or a wait on untraced code (DB, HTTP, LLM).")


# ── timeline ─────────────────────────────────────────────────────────────────


def cmd_timeline(a):
    tr = Trace(a.trace)
    top = tr.node(a.id) if a.id is not None else tr.root
    s0 = _start(tr, top)
    e0 = s0 + (top.get("time") if top.get("time") is not None else tr.t_end - s0)
    dur = max(e0 - s0, EPS)
    W = a.width
    rows: list[tuple[str, list[dict]]] = []
    if a.no_merge:
        rows = [(f"#{c['_id']} {c['name']}", [c]) for c in top.get("children", [])]
    else:
        by: dict[str, list] = {}
        for c in top.get("children", []):
            if c["name"] not in by:
                by[c["name"]] = []
                rows.append((c["name"], by[c["name"]]))
            by[c["name"]].append(c)
    col = lambda t: min(W - 1, max(0, int((t - s0) / dur * W)))
    busy = [0] * W
    print(f"#{top['_id']} {top['name']}  {fmt_t(dur)} across {W} columns ({fmt_t(dur / W)} each)")
    for label, calls in rows[: a.n]:
        bar = [" "] * W
        for c in calls:
            i, j = col(_start(tr, c)), col(min(_end(tr, c), e0))
            for k in range(i, j + 1):
                bar[k] = "█" if bar[k] == " " else "▓"
        lab = label if len(calls) == 1 or a.no_merge else f"{label} ×{len(calls)}"
        print(f"|{''.join(bar)}| {short(lab, 50)}")
    for c in top.get("children", []):
        i, j = col(_start(tr, c)), col(min(_end(tr, c), e0))
        for k in range(i, j + 1):
            busy[k] += 1
    print("|" + "".join(" " if b == 0 else (str(b) if b < 10 else "+") for b in busy) + "| calls running at once")
    if len(rows) > a.n:
        print(f"... {len(rows) - a.n} more rows (use -n)")
    gaps, k = [], 0
    while k < W:
        if busy[k] == 0:
            j = k
            while j < W and busy[j] == 0:
                j += 1
            gaps.append((k, j))
            k = j
        else:
            k += 1
    big = [(i, j) for i, j in gaps if (j - i) / W * 100 >= a.min_pct]
    if big:
        print("\nidle stretches (no traced child running: the parent's own code, or a wait on untraced code):")
        for i, j in big:
            print(f"  +{fmt_t(i * dur / W):>9} → +{fmt_t(j * dur / W):>9}  {fmt_t((j - i) * dur / W)}")
    print("\n█ one call · ▓ several calls of that row overlapping · the bottom row counts direct children running at once."
          " Column resolution limits accuracy; use `critical-path` for exact times.")


# ── serial: independent work done one after another ──────────────────────────


def _atoms(v) -> set[str]:
    return {canon_shared(sub) for _, sub in walk(v, max_depth=6) if not is_trivial(sub) and not is_marker(sub)}


def _result_atoms(tr: Trace, n: dict) -> set[str]:
    if "_ratoms" not in n:
        n["_ratoms"] = _atoms(tr.resolve(n["result"])) if "result" in n else set()
    return n["_ratoms"]


def _arg_atoms(tr: Trace, n: dict | None) -> set[str]:
    if n is None:
        return set()
    if "_aatoms" not in n:
        n["_aatoms"] = set().union(*(_atoms(v) for _, v in named_args(n, tr)))
    return n["_aatoms"]


def _judgeable(tr: Trace, n: dict) -> bool:
    """Can data flow into this call be seen? Not when its arguments are only reprs/markers (library objects)."""
    args = named_args(n, tr)
    if not tr.captured(n):
        return False
    return all(not is_marker(v) and not (isinstance(v, str) and v.startswith("<") and " at 0x" in v)
               for k, v in args if k not in ("self", "cls"))


def _depends(tr: Trace, later: list[dict], earlier: list[dict]) -> bool:
    """Does a later call take a value only an earlier sibling's result could have given it?
    Values also in the earlier calls' own args or in the parent's args came from the parent."""
    atoms = set().union(*(_result_atoms(tr, e) for e in earlier))
    atoms -= set().union(*(_arg_atoms(tr, e) for e in earlier))
    atoms -= _arg_atoms(tr, earlier[0]["_parent"])
    if not atoms:
        return False
    return any(_arg_atoms(tr, n) & atoms for n in later)


def cmd_serial(a):
    tr = Trace(a.trace)
    total = tr.root.get("time") or 0
    findings: list[tuple[float, list[str]]] = []  # (possible saving, report lines): biggest saving shown first

    def worth(saving: float) -> bool:
        return not total or saving / total * 100 >= a.min_pct

    for p in tr.nodes:
        kids = sorted(p.get("children", []), key=lambda c: _start(tr, c))
        if len(kids) < 2:
            continue
        groups: list[list[dict]] = []
        for c in kids:  # consecutive same-name siblings form one group
            if groups and groups[-1][0]["name"] == c["name"]:
                groups[-1].append(c)
            else:
                groups.append([c])
        # 1. loops: a group whose members ran strictly one after another, independent of each other
        for g in groups:
            if len(g) < 2 or any(_start(tr, g[i + 1]) < _end(tr, g[i]) - EPS for i in range(len(g) - 1)):
                continue
            if len({canon([c.get("args"), c.get("kwargs")]) for c in g}) == 1:
                continue  # same args every time: a retry or a duplicate (see `retries`, `dupes`), not a loop
            if not all(_judgeable(tr, c) for c in g):
                continue  # args are reprs only: dependencies are invisible
            if any(_depends(tr, [g[i + 1]], g[: i + 1]) for i in range(len(g) - 1)):
                continue
            durs = [_end(tr, c) - _start(tr, c) for c in g]
            saving = sum(durs) - max(durs)
            if worth(saving):
                findings.append((saving, [
                    f"#{p['_id']} {p['name']}: {g[0]['name']} ×{len(g)} ran one after another; no call needed another's result",
                    f"   sum {fmt_t(sum(durs))} · longest {fmt_t(max(durs))} · run concurrently or as one batch: up to"
                    f" {fmt_t(saving)} less   ids #{g[0]['_id']}…#{g[-1]['_id']}"]))
        # 2. chains of different calls, each starting after the previous ended, with no data flow between them
        chains, chain = [], [groups[0]]
        for g in groups[1:]:
            sequential = min(_start(tr, c) for c in g) >= max(_end(tr, c) for c in chain[-1]) - EPS
            judgeable = all(_judgeable(tr, c) for c in g) and all(_judgeable(tr, c) for c in chain[-1])
            if sequential and judgeable and not _depends(tr, g, [c for grp in chain for c in grp]):
                chain.append(g)
            else:
                chains.append(chain)
                chain = [g]
        chains.append(chain)
        for chain in chains:
            if len(chain) < 2:
                continue
            durs = [sum(_end(tr, c) - _start(tr, c) for c in g) for g in chain]  # busy time, gaps excluded
            saving = sum(durs) - max(durs)
            if worth(saving):
                findings.append((saving, [f"#{p['_id']} {p['name']}: steps in sequence, none needing a value only an earlier step's result had:"]
                                 + [f"     {fmt_t(d):>9}  #{g[0]['_id']} {g[0]['name']}{' ×' + str(len(g)) if len(g) > 1 else ''}"
                                    for g, d in list(zip(chain, durs))[:10]]
                                 + ([f"     … {len(chain) - 10} more steps"] if len(chain) > 10 else [])
                                 + [f"   overlapped they would take about {fmt_t(max(durs))} instead of {fmt_t(sum(durs))}"
                                    f" (saves up to {fmt_t(saving)})"]))
    for _, lines in sorted(findings, key=lambda f: -f[0])[: a.n]:
        print("\n".join(lines))
    if len(findings) > a.n:
        print(f"... {len(findings) - a.n} more (use -n)")
    if not findings:
        print(f"no independent sequential work above {a.min_pct}% of the total")
    print("\nindependent = no argument equals (part of) an earlier sibling's result unless the caller already had that value"
          " (in its own args, or the earlier call's args). Calls whose args are only reprs of library objects are"
          " skipped: their data flow is invisible. Hidden dependencies"
          " (shared state, ordering rules, rate limits) are invisible here: check the code before overlapping."
          " Only waits (I/O) gain from asyncio/threads; CPU-bound Python does not.")


# ── nplus1 / dupes ───────────────────────────────────────────────────────────


def _arg_vector(tr: Trace, n: dict) -> list[tuple[str, str, object]]:
    return [(k, canon(v), v) for k, v in named_args(n, tr)]


def cmd_nplus1(a):
    tr = Trace(a.trace)
    found = 0
    for p in tr.nodes:
        by: dict[str, list] = defaultdict(list)
        for c in p.get("children", []):
            if tr.captured(c):
                by[c["name"]].append(c)
        for name, calls in by.items():
            if len(calls) < a.min:
                continue
            vecs = [_arg_vector(tr, c) for c in calls]
            if len({len(v) for v in vecs}) != 1:
                continue
            varying = [i for i in range(len(vecs[0])) if len({v[i][1] for v in vecs}) > 1]
            if len(varying) != 1:
                continue
            i = varying[0]
            t_all = sum(c.get("time") or 0 for c in calls)
            if (tr.root.get("time") or 0) and t_all / tr.root["time"] * 100 < a.min_pct:
                continue
            values = [v[i][2] for v in vecs]
            distinct = len({v[i][1] for v in vecs})
            t = sum(c.get("time") or 0 for c in calls)
            w = sum(1 for c in calls) and (max(_end(tr, c) for c in calls) - min(_start(tr, c) for c in calls))
            found += 1
            const = ", ".join(f"{v[0]}={short(v[2], 30, True)}" for j, v in enumerate(vecs[0]) if j != i)
            print(f"#{p['_id']} {p['name']} → {name} ×{len(calls)}: only `{vecs[0][i][0]}` changes"
                  f" ({distinct} distinct{', some repeated' if distinct < len(calls) else ''})")
            print(f"   {vecs[0][i][0]} = {short(values, 120, True)}")
            if const:
                print(f"   same in every call: {short(const, 120)}")
            leaf = all(not c.get("children") for c in calls)
            print(f"   time {fmt_t(t)} summed ({fmt_t(t / len(calls))} per call{', leaf calls' if leaf else ''}) · span {fmt_t(w)}"
                  f" from first start to last end · ids #{calls[0]['_id']}…#{calls[-1]['_id']}")
    if not found:
        print(f"no parent calls one function {a.min}+ times varying only one argument (above {a.min_pct}% of the total)")
    print("\none call per item. It costs when each call pays a fixed price (a query, an HTTP round trip, a connection):"
          " a batch call (WHERE id IN …, bulk endpoint, prefetch) pays it once. Cheap local calls gain little."
          " Check whether the callee (or its backend) can take a list before suggesting it.")


def cmd_dupes(a):
    tr = Trace(a.trace)
    groups: dict[tuple, list] = defaultdict(list)
    for n in tr.nodes[1:]:
        if tr.captured(n) and ("args" in n or "kwargs" in n):
            groups[(n["name"], canon([tr.resolve(n.get("args")), tr.resolve(n.get("kwargs"))]))].append(n)
    rows = []
    for (name, _), calls in groups.items():
        if len(calls) < 2:
            continue
        times = [c.get("time") or 0 for c in calls]
        results = {canon(tr.resolve(c.get("result"))) if "result" in c else "!" + (c.get("exception") or "") for c in calls}
        rows.append((sum(times) - min(times), name, calls, len(results) == 1))
    rows.sort(key=lambda r: -r[0])
    if not rows:
        print("no function was called twice with equal arguments")
        return
    print(f"{'wasted':>9} {'calls':>5}  same result  function(args)   [ids]")
    for wasted, name, calls, same in rows[: a.n]:
        args = ", ".join(f"{k}={short(v, 30, True)}" for k, v in named_args(calls[0], tr))
        repr_only = any(is_marker(v) for _, v in named_args(calls[0], tr))
        cached = sum(1 for c in calls if c.get("cache") == "hit")
        print(f"{fmt_t(wasted):>9} {len(calls):5}  {'yes' if same else 'NO ':11}  {short(name + '(' + args + ')', 90)}"
              f"   {' '.join('#%d' % c['_id'] for c in calls[:6])}{' …' if len(calls) > 6 else ''}"
              + (f"  [{cached} cache hits]" if cached else "") + ("  [repr-only args: may be different objects]" if repr_only else ""))
    print("\nwasted = total time minus the cheapest call: the most a cache/memo/hoist could save. 'same result: NO' means"
          " the function is not pure for these args (time, randomness, shared state) -- do not memoize it blindly.")


# ── cache ────────────────────────────────────────────────────────────────────


def cmd_cache(a):
    tr = Trace(a.trace)
    by: dict[str, list] = defaultdict(list)
    for n in tr.nodes:
        if "cache" in n:
            by[n["name"]].append(n)
    if not by:
        print("no functools.lru_cache / cache calls recorded (or every call was ambiguous under threads)")
        return
    for name, calls in sorted(by.items(), key=lambda kv: -sum(c.get("time") or 0 for c in kv[1])):
        hits = [c for c in calls if c["cache"] == "hit"]
        misses = [c for c in calls if c["cache"] == "miss"]
        seen, re_miss = set(), []
        for c in sorted(calls, key=lambda c: _start(tr, c)):
            key = canon([tr.resolve(c.get("args")), tr.resolve(c.get("kwargs"))])
            if c["cache"] == "miss" and key in seen:
                re_miss.append(c)
            seen.add(key)
        mt = sum(c.get("time") or 0 for c in misses)
        print(f"{name}: {len(calls)} calls · {len(hits)} hits · {len(misses)} misses"
              f" ({len(hits) / len(calls) * 100:.0f}% hit rate) · time in misses {fmt_t(mt)}")
        if re_miss:
            print(f"   {len(re_miss)} misses for arguments already seen earlier: evicted (maxsize too small) or the"
                  f" key differs in a way the capture hides (kwargs vs positional, float vs int, mutated object)")
            for c in re_miss[:5]:
                print(f"     #{c['_id']} {call_line(c, tr, 100)}")
        if not hits and len(calls) > 1:
            print("   never hit: the cache only costs memory here")


# ── scaling / outliers ───────────────────────────────────────────────────────


def _sizes(tr: Trace, n: dict) -> list[float | None]:
    out = []
    for _, v in named_args(n, tr):
        if isinstance(v, (list, dict, str)) and not is_marker(v):
            out.append(float(len(v)))
        elif isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out.append(float(v))
        else:
            out.append(None)
    return out


def _subtree_calls(n: dict) -> int:
    k, stack = 0, [n]
    while stack:
        x = stack.pop()
        k += 1
        stack.extend(x.get("children", []))
    return k


def _fit(xs, ys) -> float | None:
    """Exponent k in y ~ x^k: Theil-Sen (median of pairwise log-log slopes), robust to a warm-up outlier."""
    pts = [(math.log(x), math.log(y)) for x, y in zip(xs, ys) if x > 0 and y > 0]
    if len({p[0] for p in pts}) < 3:
        return None
    slopes = sorted((q[1] - p[1]) / (q[0] - p[0]) for i, p in enumerate(pts) for q in pts[i + 1:] if q[0] != p[0])
    return percentile(slopes, 0.5) if slopes else None


def _scaling_of(tr: Trace, calls: list[dict]):
    vecs = [_sizes(tr, c) for c in calls]
    if len({len(v) for v in vecs}) != 1 or not vecs[0]:
        return None
    best = None
    for i in range(len(vecs[0])):
        xs = [v[i] for v in vecs]
        if any(x is None for x in xs) or len(set(xs)) < 3 or max(xs) < 4 * min(xs):
            continue
        ys = [c.get("time") or 0 for c in calls]
        k = _fit(xs, ys)
        if k is not None and (best is None or len(set(xs)) > len(set(best[1]))):
            best = (i, xs, ys, k, _fit(xs, [_subtree_calls(c) for c in calls]), _top_slope(xs, ys))
    return best


def _top_slope(xs, ys) -> float | None:
    """Exponent between the two largest input sizes (median time per size): the growth that matters at scale."""
    by: dict[float, list] = defaultdict(list)
    for x, y in zip(xs, ys):
        by[x].append(y)
    sizes = sorted(by)
    if len(sizes) < 2:
        return None
    (x1, x2) = sizes[-2:]
    if x2 < 1.5 * x1:
        return None
    y1, y2 = percentile(by[x1], .5), percentile(by[x2], .5)
    return math.log(y2 / y1) / math.log(x2 / x1) if y1 > 0 and y2 > 0 else None


def cmd_scaling(a):
    tr = Trace(a.trace)
    by: dict[str, list] = defaultdict(list)
    for n in tr.nodes:
        if tr.captured(n) and (not a.fn or n["name"] == a.fn or a.fn in n["name"]):
            by[n["name"]].append(n)
    rows = []
    for name, calls in by.items():
        if len(calls) < 3:
            continue
        r = _scaling_of(tr, calls)
        if r:
            rows.append((name, calls, r))
    if not rows:
        print("no function has 3+ calls whose input size (a len() or a positive number argument) varies")
        return
    if not a.fn:
        rows = [r for r in rows if max(r[2][3], r[2][5] or 0) >= a.min_exp]
        print(f"functions whose time grows like size^k with k ≥ {a.min_exp} (median log-log slope over pairs of calls):")
    for name, calls, (i, xs, ys, k, kc, ktop) in sorted(rows, key=lambda r: -sum(r[2][2]))[: a.n]:
        pname = named_args(calls[0], tr)[i][0]
        print(f"\n{name}: time ~ {pname}^{k:.2f} overall" + (f", ^{ktop:.2f} between the two largest sizes" if ktop is not None else "")
              + (f" · traced calls inside ~ {pname}^{kc:.2f}" if kc is not None else "")
              + f"  ({len(calls)} calls, total {fmt_t(sum(ys))})")
        pts = sorted(zip(xs, ys, calls), key=lambda t: t[0])
        for x, y, c in pts[:: max(1, len(pts) // 8)]:
            print(f"   size {x:>10g}  {fmt_t(y):>9}  {_subtree_calls(c):6} calls   #{c['_id']}")
    print("\nsize = len() of a list/str/dict argument, or a positive number argument (an id can pass for a size: check"
          " the parameter); sizes must span 4×. k≈1 linear, k≈2 quadratic"
          " (nested loop, repeated scan, list.insert(0)/`in list` inside a loop). Calls under ~50 µs are dominated by"
          " fixed overhead, so k reads low there.")


def cmd_outliers(a):
    tr = Trace(a.trace)
    by: dict[str, list] = defaultdict(list)
    for n in tr.nodes[1:]:
        if n.get("time") is not None:
            by[n["name"]].append(n)
    found = 0
    for name, calls in sorted(by.items(), key=lambda kv: -sum(c["time"] for c in kv[1])):
        if len(calls) < a.min:
            continue
        times = [c["time"] for c in calls]
        med = percentile(times, 0.5)
        typical = min(calls, key=lambda c: abs(c["time"] - med))
        slow = [c for c in calls if c["time"] >= a.factor * med and c["time"] - med >= 1e-4]
        if not slow:
            continue
        found += 1
        if found > a.n:
            continue
        print(f"{name}: median {fmt_t(med)} over {len(calls)} calls; {len(slow)} call(s) ≥ {a.factor:g}× slower")
        ta = dict(named_args(typical, tr))
        for c in sorted(slow, key=lambda c: -c["time"])[:5]:
            d = []
            for k, v in named_args(c, tr):
                d += [(f"{k}{fmt_path(p)}", x, y) for p, x, y in diff_values(ta.get(k, "<absent>"), v, limit=3)]
            kids = (len(c.get("children", [])), len(typical.get("children", [])))
            print(f"   #{c['_id']} {fmt_t(c['time'])} ({c['time'] / med:.0f}×)  vs typical #{typical['_id']}: "
                  + ("; ".join(f"{w} {short(x, 25, True)}→{short(y, 25, True)}" for w, x, y in d[:4]) or "same args")
                  + (f"; {kids[0]} child calls vs {kids[1]}" if kids[0] != kids[1] else ""))
    if found > a.n:
        print(f"... {found - a.n} more functions with outliers (use -n)")
    if not found:
        print(f"no function with {a.min}+ calls has a call ≥ {a.factor:g}× its median")
    print("\nsame args but slower: the time went to something outside the args (lock/queue wait, GC, cold cache, a slow"
          " remote call). Different args: compare them with `diverge`.")


# ── generators / payload ─────────────────────────────────────────────────────


def cmd_gens(a):
    tr = Trace(a.trace)
    gens = [n for n in tr.nodes if "yields" in n or "active_time" in n]
    if not gens:
        print("no generator calls recorded")
        return
    print(f"{'active':>9} {'wall':>9} {'yields':>6}  state      generator   (#id)")
    for n in sorted(gens, key=lambda n: -(n.get("active_time") or 0))[: a.n]:
        state = ("closed early" if n.get("closed") else "raised" if "exception" in n
                 else "running" if n.get("time") is None else "exhausted")
        print(f"{fmt_t(n.get('active_time')):>9} {fmt_t(n.get('time')):>9} {n.get('yields', 0):6}  {state:10} {call_line(n, tr, 80)}  (#{n['_id']})")
    print("\nactive = time running the generator's own code; wall = first next() → end, so it includes the consumer's"
          " work between values. 'closed early' = the consumer stopped (break, next() a few times, close()): values"
          " computed but unused, or work cut short on purpose. A generator never iterated has no node at all.")


def _size_fn(tr: Trace):
    memo: dict[str, int] = {}

    def size(v) -> int:
        if isinstance(v, dict):
            if "__ref__" in v:
                r = v["__ref__"]
                if r not in memo:
                    memo[r] = 0
                    memo[r] = size(tr.variables.get(r))
                return memo[r] + sum(size(x) + len(k) + 4 for k, x in v.items() if k != "__ref__")
            if "__spilled__" in v:
                return int(v.get("size") or 0)
            return 2 + sum(len(k) + 4 + size(x) for k, x in v.items())
        if isinstance(v, list):
            return 2 + sum(size(x) + 1 for x in v)
        return len(json.dumps(v, ensure_ascii=False, default=str))

    return size


def cmd_payload(a):
    tr = Trace(a.trace)
    size = _size_fn(tr)
    by: dict[str, dict] = defaultdict(lambda: {"calls": 0, "in": 0, "out": 0, "max_in": (0, None), "max_out": (0, None), "cap": 0.0})
    for n in tr.nodes:
        s = by[n["name"]]
        si = size(n.get("args") or []) + size(n.get("kwargs") or {})
        so = size(n["result"]) if "result" in n else 0
        s["calls"] += 1
        s["in"] += si
        s["out"] += so
        s["cap"] += n.get("capture_time") or 0
        if si > s["max_in"][0]:
            s["max_in"] = (si, n)
        if so > s["max_out"][0]:
            s["max_out"] = (so, n)
    kb = lambda b: f"{b / 1024:.1f} KB" if b >= 1024 else f"{b} B"
    key = {"out": lambda s: -s["max_out"][0], "in": lambda s: -s["max_in"][0], "capture": lambda s: -s["cap"]}[a.sort]
    print(f"{'max in':>10} {'max out':>10} {'total out':>10} {'calls':>6} {'capture':>9}  function   [largest]")
    for name, s in sorted(by.items(), key=lambda kv: key(kv[1]))[: a.n]:
        big = s["max_out"][1] or s["max_in"][1]
        print(f"{kb(s['max_in'][0]):>10} {kb(s['max_out'][0]):>10} {kb(s['out']):>10} {s['calls']:6} {fmt_t(s['cap']):>9}  {name}"
              f"   #{big['_id'] if big else '-'}")
    print("\nsizes are of the captured JSON (a lower bound when values were cut by MAX_DEPTH/MAX_SIZE_CAPTURE or shown as"
          " <TypeName>). Big results passed down several calls, or built and then filtered, are copy/memory costs;"
          " capture = perf's time converting these values (overhead, not your code).")
