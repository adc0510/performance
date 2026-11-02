"""The original browsing commands: ls, summary, tree, hot, find, node, locals, path, where, diff."""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

from .exceptions import _error_groups
from .model import Trace, aggregate, call_line, expand_locals, fmt_t, match as _match, named_args, self_time, short, wall


def cmd_ls(a):
    files = sorted(Path(a.dir).rglob("*.json") if a.recursive else Path(a.dir).glob("*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        print(f"no .json files in {a.dir}")
        return
    print(f"{'modified':19}  {'total':>10}  {'calls':>6}  err  root / first string argument   file")
    for p in files[: a.n]:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            root = data["trace"]
        except Exception:
            continue
        calls, errs, stack = 0, 0, [root]
        while stack:
            x = stack.pop()
            calls += 1
            errs += "exception" in x
            stack.extend(x.get("children", []))
        hint = ""
        for v in list((root.get("kwargs") or {}).values()) + list(root.get("args") or []):
            if isinstance(v, str) and not v.startswith("<"):
                hint = v
                break
        ts = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{ts}  {fmt_t(root.get('time')):>10}  {calls:6}  {errs:3}  {short(root['name'] + (' ' + hint if hint else ''), 34):34}  {p}")
    if len(files) > a.n:
        print(f"... {len(files) - a.n} older files (use -n)")


def cmd_summary(a):
    tr = Trace(a.trace)
    root, total = tr.root, tr.root.get("time") or 0
    cap = sum(n.get("capture_time") or 0 for n in tr.nodes)
    loc = sum(n.get("locals_capture_time") or 0 for n in tr.nodes)
    skipped = sum(1 for n in tr.nodes if n.get("capture_skipped"))
    with_locals = sum(1 for n in tr.nodes if "locals" in n)
    stats = aggregate(tr)
    print(f"trace   {tr.path}")
    print(f"root    #0 {call_line(root, tr)}")
    print(f"when    {root.get('start_timestamp')} → {root.get('end_timestamp')}")
    print(f"total   {fmt_t(total)} wall · {len(tr.nodes)} calls · {len(stats)} functions · depth {max(n['_depth'] for n in tr.nodes)}")
    if "exception" in root and "result" in root:
        print(f"RESULT  root raised {short(root['exception'], 110)}, suppressed by perf (SUPPRESS_ERRORS):"
              f" the caller got {short(tr.resolve(root['result']), 60, True)}")
    elif "exception" in root:
        print(f"RESULT  root raised {short(root['exception'], 150)}")
    elif "result" in root:
        print(f"result  {short(tr.resolve(root['result']), 150, True)}")
    overhead = cap + loc
    # the root's own capture (its args and result) runs outside its "time", so the
    # elapsed time the recording cost is a share of is time + that capture
    root_cap = root.get("capture_time") or 0
    elapsed = total + root_cap
    print(f"perf    capture {fmt_t(cap)} + locals {fmt_t(loc)} = {overhead / elapsed * 100 if elapsed else 0:.0f}% of the elapsed {fmt_t(elapsed)} is perf's own"
          f" recording cost{f' · {skipped} calls past MAX_CAPTURES_PER_FUNCTION (timed, not captured)' if skipped else ''}"
          f"{f' · TRACE_LOCALS on {with_locals} calls' if with_locals else ''}")
    if elapsed and overhead / elapsed >= 0.2:
        print(f"WARNING recording cost is {overhead / elapsed * 100:.0f}% of the elapsed time: timings are inflated, worst in"
              f" capture-heavy functions. Without it this run is roughly {fmt_t(max(elapsed - overhead, 0))} (exact only"
              f" for sequential code). Self times below already exclude capture; run `advise` for a cheaper setup.")
    unfinished = [n for n in tr.nodes if n.get("time") is None]
    if unfinished:
        print(f"note    {len(unfinished)} calls never finished (still running when the trace closed, or TIMEOUT): e.g. #{unfinished[0]['_id']} {unfinished[0]['name']}"
              " -- `unfinished` lists them")
    print("\nhot spots by self time (in the function itself, children excluded; summed over calls, so concurrent"
          " calls can add up to more than 100% -- wall is the elapsed footprint)")
    print(f"  {'self':>9} {'%tot':>5} {'wall':>9} {'calls':>6}  function   [first ids]")
    for name, s in sorted(stats.items(), key=lambda kv: -kv[1]["self"])[: a.n]:
        pct = s["self"] / total * 100 if total else 0
        print(f"  {fmt_t(s['self']):>9} {pct:4.0f}% {fmt_t(s['wall']):>9} {s['calls']:6}  {name}   {' '.join('#%d' % i for i in s['ids'][:3])}")
    slow = sorted((n for n in tr.nodes[1:] if not n.get("children")), key=lambda n: -(n.get("time") or 0))[:5]
    if slow:
        print("\nslowest single leaf calls (no traced children: the work or the wait happens right there)")
        for n in slow:
            print(f"  {fmt_t(n.get('time')):>9}  #{n['_id']} {call_line(n, tr, 120)}")
    errs = _error_groups(tr)
    if errs:
        print(f"\nexceptions: {len(errs)} distinct (run `errors` for details)")
        for g in errs[:5]:
            print(f"  {g['count']}× {short(g['exception'], 90)}  raised in #{g['origin']['_id']} {g['origin']['name']}  → {g['fate']}")


def cmd_tree(a):
    tr = Trace(a.trace)
    top = tr.node(a.id) if a.id is not None else tr.root
    total = top.get("time") or 0
    print(f"{'wall':>10} {'self':>9} {'%':>4}  call   (#id)")

    def show(nodes, depth, prefix):
        groups: list[list[dict]] = []
        if a.no_merge:
            groups = [[n] for n in nodes]
        else:
            by_name: dict[str, list] = {}
            for n in nodes:
                if n["name"] not in by_name:
                    by_name[n["name"]] = []
                    groups.append(by_name[n["name"]])
                by_name[n["name"]].append(n)
        hidden = 0
        for g in groups:
            w = wall((n.get("start", 0), n.get("time") or 0) for n in g)
            if total and w / total * 100 < a.min_pct:
                hidden += 1
                continue
            first = g[0]
            label = call_line(first, tr) if len(g) == 1 else f"{first['name']} ×{len(g)}"
            mark = "  !! " + short(first["exception"], 60) if len(g) == 1 and "exception" in first else (
                f"  !! {sum('exception' in n for n in g)} raised" if any("exception" in n for n in g) else "")
            ids = f"#{first['_id']}" if len(g) == 1 else f"#{first['_id']}…#{g[-1]['_id']}"
            sf = sum(self_time(n) for n in g)
            print(f"{fmt_t(w):>10} {fmt_t(sf):>9} {w / total * 100 if total else 0:3.0f}%  {prefix}{label}  ({ids}){mark}")
            if depth < a.depth:
                kids = [c for n in g for c in n.get("children", [])]
                if kids:
                    show(kids, depth + 1, prefix + "  ")
        if hidden:
            print(f"{'':>10} {'':>9} {'':>4}  {prefix}… {hidden} more under {a.min_pct}% each (lower --min-pct)")

    print(f"{fmt_t(total):>10} {fmt_t(self_time(top)):>9} 100%  {call_line(top, tr)}  (#{top['_id']})")
    show(top.get("children", []), 1, "  ")


def cmd_hot(a):
    tr = Trace(a.trace)
    total = tr.root.get("time") or 0
    stats = aggregate(tr)
    key = {"self": "self", "wall": "wall", "calls": "calls", "cumul": "cumul"}[a.sort]
    print(f"{'self':>9} {'wall':>9} {'cumul':>9} {'calls':>6} {'err':>4} {'%tot(self)':>10}  function   [first ids]")
    for name, s in sorted(stats.items(), key=lambda kv: -kv[1][key])[: a.n]:
        print(f"{fmt_t(s['self']):>9} {fmt_t(s['wall']):>9} {fmt_t(s['cumul']):>9} {s['calls']:6} {s['errors']:4} "
              f"{s['self'] / total * 100 if total else 0:9.1f}%  {name}   {' '.join('#%d' % i for i in s['ids'])}")
    print("\nself = in the function itself, summed over calls; wall = elapsed across all its calls (overlap counted once);"
          " cumul = sum of call durations. When calls run concurrently (async tasks, threads) self and cumul"
          " exceed wall, and %tot can pass 100%: compare wall to the total to see what the request waited on.")


def cmd_find(a):
    tr = Trace(a.trace)
    hits = [n for n in tr.nodes if _match(n["name"], a.pattern) and (not a.errors or "exception" in n)]
    print(f"{len(hits)} calls match {a.pattern!r}" + (" with an exception" if a.errors else ""))
    for n in hits[: a.n]:
        res = f"!! {short(n['exception'], 70)}" if "exception" in n else (
            f"→ {short(tr.resolve(n['result']), 70, True)}" if "result" in n else ("(capture skipped)" if n.get("capture_skipped") else ""))
        print(f"  #{n['_id']:<6} {fmt_t(n.get('time')):>9}  {call_line(n, tr, 100)}  {res}")
    if len(hits) > a.n:
        print(f"  ... {len(hits) - a.n} more (use -n)")


def cmd_node(a):
    tr = Trace(a.trace)
    tr.load_spills = True
    n = tr.node(a.id)
    w = a.width
    chain = []
    p = n
    while p is not None:
        chain.append(p)
        p = p["_parent"]
    print(f"#{n['_id']} {n['name']}")
    print("path    " + " → ".join(x["name"] for x in reversed(chain)))
    print(f"time    {fmt_t(n.get('time'))} wall · self {fmt_t(self_time(n))}"
          + (f" · active {fmt_t(n['active_time'])} (generator: time actually running)" if "active_time" in n else "")
          + (f" · perf capture {fmt_t(n.get('capture_time'))}" if n.get("capture_time") else ""))
    if n.get("capture_skipped"):
        print("capture skipped: past MAX_CAPTURES_PER_FUNCTION -- timed, but args/result not recorded")
    args = named_args(n, tr)
    if args:
        print("args    (captured BEFORE the call ran)")
        for k, v in args:
            print(f"  {k} = {short(v, w, True)}")
    if "result" in n:
        print(f"result  {short(tr.resolve(n['result']), w, True)}")
    for k in ("yields", "yielded", "closed", "cache"):
        if k in n:
            print(f"{k:8}{short(tr.resolve(n[k]), w)}")
    if "exception" in n:
        print(f"RAISED  {n['exception']}")
        o = n.get("exception_origin")
        if o:
            print(f"  origin {o.get('file')}:{o.get('line')} in {o.get('function')}: {o.get('text')}")
        dbg = tr.resolve(n.get("exception_debug")) or {}
        frames = dbg.get("frames") or []
        if frames and a.frames:
            for f in frames:
                print(f"    {f.get('file')}:{f.get('line')} {f.get('function')}: {f.get('text')}")
        elif frames:
            print(f"  {len(frames)} frames recorded (add --frames to list them)")
        # perf >= 0.12.0: nodes an exception passes through on its way up share the
        # innermost node's exception_debug (its frames start there, not here).
        raw = n.get("exception_debug")
        if raw is not None and n is not tr.root and any(
                c.get("exception_debug") == raw for c in n.get("children") or []):
            full = " -- the root #0 has the complete traceback" if tr.root.get("exception") == n["exception"] else ""
            print(f"  (traceback shared with the call it came from: it starts at the innermost node that raised{full})")
        cause = dbg.get("caused_by")
        while cause:
            print(f"  caused by {short(cause.get('exception'), w)}")
            cause = cause.get("caused_by")
    if n.get("locals"):
        print(f"locals  {len(n['locals'])} entries{' (+' + str(n['locals_dropped']) + ' dropped past MAX_LOCALS_EVENTS)' if n.get('locals_dropped') else ''}"
              f" -- `locals {a.trace} {n['_id']}` shows them line by line")
    kids = n.get("children") or []
    if kids:
        print(f"children {len(kids)}:")
        for c in kids[: a.n]:
            mark = f"  !! {short(c['exception'], 50)}" if "exception" in c else ""
            print(f"  #{c['_id']:<6} {fmt_t(c.get('time')):>9}  {call_line(c, tr, 100)}{mark}")
        if len(kids) > a.n:
            print(f"  ... {len(kids) - a.n} more (use -n)")


def cmd_locals(a):
    tr = Trace(a.trace)
    n = tr.node(a.id)
    if not n.get("locals"):
        sys.exit(f"#{n['_id']} {n['name']} has no locals (TRACE_LOCALS did not match it, or nothing changed)")
    print(f"#{n['_id']} {n['name']}  ({tr.source_file(n) or 'source file unknown'})")
    for k, v in named_args(n, tr):
        print(f"  arg  {k} = {short(v, a.width, True)}")
    for row in expand_locals(n, tr):
        text = tr.line_text(n, row["line"])
        print(f"L{row['line']:<5} {short(text, 90)}")
        for name, v in row["set"].items():
            how = "~" if name in row["edited"] else "="
            print(f"         {name} {how} {short(v, a.width, True)}")
        if row.get("deleted"):
            print(f"         del {', '.join(row['deleted'])}")
        for name, v in (row.get("globals") or {}).items():
            print(f"         global {name} = {short(v, a.width, True)}")
    if n.get("locals_dropped"):
        print(f"... {n['locals_dropped']} more changes not recorded (MAX_LOCALS_EVENTS cap)")
    print("\n'=' new value bound · '~' same object changed in place (rebuilt from recorded edits)"
          " · each entry is what the line just ABOVE in execution order changed")


def cmd_path(a):
    tr = Trace(a.trace)
    n = tr.node(a.id)
    chain = []
    while n is not None:
        chain.append(n)
        n = n["_parent"]
    for depth, x in enumerate(reversed(chain)):
        print(f"{'  ' * depth}#{x['_id']} {fmt_t(x.get('time'))}  {call_line(x, tr, 120)}")


SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "site-packages", "__pycache__", ".tox", "build", "dist", ".mypy_cache"}


def _py_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


def cmd_where(a):
    name = a.target
    if re.fullmatch(r"#?\d+", a.target):
        tr = Trace(a.trace)
        node = tr.node(a.target)
        name = node["name"]
        o = node.get("exception_origin")
        if tr.source_file(node):
            print(f"{tr.source_file(node)}  (from the node's TRACE_LOCALS source)")
        if o:
            print(f"{o.get('file')}:{o.get('line')}  (where its exception was raised)")
    root = Path(a.root).resolve()
    name = re.sub(r" ×\d+$", "", name)
    parts = [p for p in name.replace(".<locals>.", ".").split(".") if p]
    func = parts[-1]
    if func in ("setter", "deleter") and len(parts) > 1:
        func = parts[-2]
        parts = parts[:-1]
    candidates = []
    # module-qualified name: pkg.mod.func / pkg.mod.Class.method -> pkg/mod.py
    for i in range(len(parts) - 1, 0, -1):
        mod = Path(*parts[:i])
        for f in (root / f"{mod}.py", root / mod / "__init__.py"):
            if f.exists():
                candidates.append(f)
        if candidates:
            break
    files = candidates or list(_py_files(root))
    cls = parts[-2] if len(parts) >= 2 and parts[-2][:1].isupper() else None
    def_rx = re.compile(rf"^\s*(async\s+)?def\s+{re.escape(func)}\s*\(" if func != "<lambda>" else r"lambda\b")
    found = 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        current_cls = None
        for no, text in enumerate(lines, 1):
            m = re.match(r"^\s*class\s+(\w+)", text)
            if m:
                current_cls = m.group(1)
            if def_rx.search(text) and (cls is None or current_cls == cls or not candidates):
                if cls and current_cls != cls:
                    continue
                print(f"{f}:{no}: {text.strip()}")
                found += 1
    if not found:
        print(f"no definition of {name!r} found under {root} (dynamic code, a library, or a renamed function)")


def cmd_diff(a):
    ta, tb = Trace(a.a), Trace(a.b)
    sa, sb = aggregate(ta), aggregate(tb)
    tot_a, tot_b = ta.root.get("time") or 0, tb.root.get("time") or 0
    print(f"total  {fmt_t(tot_a)} → {fmt_t(tot_b)}  ({(tot_b - tot_a) / tot_a * 100 if tot_a else 0:+.0f}%)   calls {len(ta.nodes)} → {len(tb.nodes)}")
    rows = []
    for name in set(sa) | set(sb):
        x, y = sa.get(name), sb.get(name)
        rows.append((name, x["self"] if x else 0.0, y["self"] if y else 0.0, x["calls"] if x else 0, y["calls"] if y else 0))
    rows.sort(key=lambda r: -abs(r[2] - r[1]))
    print(f"\n{'self before':>11} {'self after':>11} {'Δ':>10} {'calls':>13}  function")
    for name, x, y, cx, cy in rows[: a.n]:
        print(f"{fmt_t(x):>11} {fmt_t(y):>11} {('+' if y >= x else '-') + fmt_t(abs(y - x)):>10} {cx:6}→{cy:<6}  {name}"
              + ("  (new)" if cx == 0 else "  (gone)" if cy == 0 else ""))

