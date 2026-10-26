#!/usr/bin/env python3
"""Read perf trace files (JSON written by the `perf` tracer) without loading them whole into context.

Standard library only -- runs with any python3, perf need not be installed.

    perf_trace.py ls DIR                          trace files in DIR, newest first
    perf_trace.py summary TRACE                   overview: totals, hot spots, errors, perf overhead
    perf_trace.py tree TRACE [--id N] [--depth D] [--min-pct P] [--no-merge]
    perf_trace.py hot TRACE [-n N] [--sort self|wall|calls|cumul]
    perf_trace.py find TRACE PATTERN [-n N] [--errors]
    perf_trace.py node TRACE ID [--width W]       one call: args, result, exception, locals, children
    perf_trace.py errors TRACE                    every exception: where raised, how far it propagated
    perf_trace.py locals TRACE ID                 TRACE_LOCALS of one call, line by line with source text
    perf_trace.py path TRACE ID                   the chain of calls from the root down to node ID
    perf_trace.py where TRACE ID|NAME --root DIR  source file:line of a node's function
    perf_trace.py diff TRACE_A TRACE_B [-n N]     per-function time, before vs after

Node IDs (#N) are preorder positions in the trace's tree: stable for a given file, so
an ID printed by one command can be passed to another.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# ── loading ───────────────────────────────────────────────────────────────────


class Trace:
    def __init__(self, path: str):
        self.path = path
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if "trace" not in data:
            sys.exit(f"{path}: not a perf trace (no top-level 'trace' key)")
        self.variables = data.get("variables", {})
        self.sources = data.get("sources", {})
        self.root = data["trace"]
        self.nodes: list[dict] = []  # preorder; node["_id"], node["_parent"], node["_depth"]
        stack = [(self.root, None, 0)]
        while stack:
            node, parent, depth = stack.pop()
            node["_id"], node["_parent"], node["_depth"] = len(self.nodes), parent, depth
            self.nodes.append(node)
            stack.extend((c, node, depth + 1) for c in reversed(node.get("children", [])))
        self._resolved: dict = {}

    def node(self, ident: str | int) -> dict:
        try:
            i = int(str(ident).lstrip("#"))
            return self.nodes[i]
        except (ValueError, IndexError):
            sys.exit(f"no node #{ident} (this trace has #0..#{len(self.nodes) - 1})")

    def resolve(self, value, depth: int = 0):
        """Value with every {"__ref__"} replaced by its pooled content."""
        if depth > 100:
            return value
        if isinstance(value, dict):
            if "__ref__" in value:
                ref = value["__ref__"]
                if ref not in self._resolved:
                    if ref not in self.variables:
                        return value
                    self._resolved[ref] = None  # cycle guard
                    self._resolved[ref] = self.resolve(self.variables[ref], depth + 1)
                target = self._resolved[ref]
                if len(value) > 1 and isinstance(target, dict):  # {"__type__": T, "__ref__": r}
                    return {**{k: self.resolve(v, depth + 1) for k, v in value.items() if k != "__ref__"}, **target}
                return target
            return {k: self.resolve(v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, depth + 1) for v in value]
        return value

    def line_text(self, node: dict, line: int) -> str:
        src = self.sources.get(node.get("source") or "", {})
        return src.get("lines", {}).get(str(line), "")

    def source_file(self, node: dict) -> str | None:
        return self.sources.get(node.get("source") or "", {}).get("file")


# ── timing (same rules as perf's own report) ─────────────────────────────────


def wall(intervals) -> float:
    """Union length of (start, duration) intervals: elapsed time, overlap counted once."""
    spans = sorted((s, s + (t or 0)) for s, t in intervals)
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def self_time(node: dict) -> float:
    """Time inside this call excluding its children (and their capture), like perf's report.
    Generators count only their active time (not the consumer's work between yields)."""
    t = node.get("active_time", node.get("time") or 0) - (node.get("locals_capture_time") or 0)
    kids = node.get("children") or []
    if kids:
        t -= wall((c.get("start", 0), (c.get("time") or 0) + (c.get("capture_time") or 0)) for c in kids)
    return max(t, 0.0)


def fmt_t(seconds) -> str:
    if seconds is None:
        return "unfinished"
    if seconds >= 1:
        return f"{seconds:.2f} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.1f} ms"
    return f"{seconds * 1e6:.0f} µs"


def aggregate(tr: Trace) -> dict[str, dict]:
    stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "self": 0.0, "cumul": 0.0, "iv": [], "errors": 0, "ids": []})
    for n in tr.nodes:
        s = stats[n["name"]]
        s["calls"] += 1
        s["self"] += self_time(n)
        s["cumul"] += n.get("time") or 0
        s["iv"].append((n.get("start", 0), n.get("time") or 0))
        s["errors"] += "exception" in n
        if len(s["ids"]) < 5:
            s["ids"].append(n["_id"])
    for s in stats.values():
        s["wall"] = wall(s.pop("iv"))
    return stats


# ── value rendering ───────────────────────────────────────────────────────────


def short(value, width: int = 160, quote: bool = False) -> str:
    """One-line rendering. With quote=True a string VALUE is shown JSON-quoted (so "A" and A,
    "None" and null differ), except perf's own markers like <LawRepository>, kept bare."""
    if isinstance(value, str) and not (quote and not (value.startswith("<") and value.endswith(">"))):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", "\\n")
    return text if len(text) <= width else text[: width - 1] + "…"


def named_args(node: dict, tr: Trace) -> list[tuple[str, object]]:
    """[(name, value)] for a call's captured arguments (positional names from "params" when known)."""
    params = node.get("params") or []
    raw = node.get("args") or []
    owner = node["name"].rsplit(".", 2)[-2] if node["name"].count(".") >= 1 else ""
    names = []
    for i, a in enumerate(raw):
        if i < len(params):
            names.append(params[i])
        elif i == 0 and owner[:1].isupper() and (a == f"<{owner}>" or (isinstance(a, dict) and a.get("__type__") == owner)):
            names.append("self")  # Class.method called with its instance first
        else:
            names.append(f"arg{i}")
    out = [(names[i], tr.resolve(a)) for i, a in enumerate(raw)]
    out += [(k, tr.resolve(v)) for k, v in (node.get("kwargs") or {}).items()]
    return out


def call_line(node: dict, tr: Trace, width: int = 110) -> str:
    args = ", ".join(f"{k}={short(v, 40, True)}" for k, v in named_args(node, tr)) if ("args" in node or "kwargs" in node) else ""
    head = f"{node['name']}({args})" if args else node["name"]
    return short(head, width)


# ── locals replay (same semantics as perf.expand_locals) ─────────────────────

KEYS = ("line", "set", "changed", "deleted", "globals", "deleted_globals")


def expand_locals(node: dict, tr: Trace) -> list[dict]:
    entries = node.get("locals") or []
    params = node.get("params") or []
    args, kwargs = node.get("args") or [], node.get("kwargs") or {}
    current: dict = {}

    def base(name):
        if name in current:
            return current[name]
        i = params.index(name) if name in params else len(args)
        return tr.resolve(args[i] if i < len(args) else kwargs.get(name))

    def apply(value, op):
        kind, path = op[0], op[1]
        if not path:
            return value + tr.resolve(op[2]) if kind == "+" else value
        root = parent = type(value)(value)
        for key in path[:-1]:
            parent[key] = child = type(parent[key])(parent[key])
            parent = child
        last = path[-1]
        if kind == "+":
            parent[last] = parent[last] + tr.resolve(op[2])
        elif kind == "=":
            parent[last] = tr.resolve(op[2])
        elif kind == "-":
            del parent[last]
        return root

    out = []
    for raw in entries:
        e = dict(zip(KEYS, raw)) if isinstance(raw, list) else dict(raw)
        row = {"line": e["line"], "set": {}, "edited": []}
        for name, value in (e.get("set") or {}).items():
            row["set"][name] = current[name] = tr.resolve(value)
        for name, ops in (e.get("changed") or {}).items():
            value = base(name)
            try:
                for op in ops:
                    value = apply(value, op)
            except Exception as exc:  # base not recorded (e.g. capture was skipped)
                value = f"<could not replay edits: {type(exc).__name__}: {exc}>"
            row["set"][name] = current[name] = value
            row["edited"].append(name)
        for name in e.get("deleted") or ():
            current.pop(name, None)
        for key in ("deleted", "globals", "deleted_globals"):
            if e.get(key):
                row[key] = tr.resolve(e[key])
        out.append(row)
    return out


# ── commands ──────────────────────────────────────────────────────────────────


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
        import datetime

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
    if "exception" in root:
        print(f"RESULT  root raised {short(root['exception'], 150)}")
    elif "result" in root:
        print(f"result  {short(tr.resolve(root['result']), 150, True)}")
    overhead = cap + loc
    print(f"perf    capture {fmt_t(cap)} + locals {fmt_t(loc)} = {overhead / total * 100 if total else 0:.0f}% of the total is perf's own"
          f" recording cost{f' · {skipped} calls past MAX_CAPTURES_PER_FUNCTION (timed, not captured)' if skipped else ''}"
          f"{f' · TRACE_LOCALS on {with_locals} calls' if with_locals else ''}")
    unfinished = [n for n in tr.nodes if n.get("time") is None]
    if unfinished:
        print(f"note    {len(unfinished)} calls never finished (still running when the trace closed, or TIMEOUT): e.g. #{unfinished[0]['_id']} {unfinished[0]['name']}")
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


def _match(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern) if any(ch in pattern for ch in "*?[") else pattern in name


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
        dbg = n.get("exception_debug") or {}
        frames = dbg.get("frames") or []
        if frames and a.frames:
            for f in frames:
                print(f"    {f.get('file')}:{f.get('line')} {f.get('function')}: {f.get('text')}")
        elif frames:
            print(f"  {len(frames)} frames recorded (add --frames to list them)")
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
        g = groups.setdefault(key, {"exception": exc, "origin": n, "where": o, "count": 0, "fate": fate, "ids": [], "top": last})
        g["count"] += 1
        if len(g["ids"]) < 8:
            g["ids"].append(n["_id"])
    return sorted(groups.values(), key=lambda g: -g["count"])


def cmd_errors(a):
    tr = Trace(a.trace)
    groups = _error_groups(tr)
    if not groups:
        print("no exceptions recorded")
        return
    for g in groups:
        o = g["where"]
        print(f"{g['count']}× {g['exception']}")
        print(f"   raised in  #{g['origin']['_id']} {call_line(g['origin'], tr, 120)}")
        if o:
            print(f"   at         {o.get('file')}:{o.get('line')} in {o.get('function')}: {o.get('text')}")
        print(f"   fate       {g['fate']}")
        if g["count"] > 1:
            print(f"   all ids    {' '.join('#%d' % i for i in g['ids'])}{' …' if g['count'] > len(g['ids']) else ''}")
        print()


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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ls"); p.add_argument("dir"); p.add_argument("-n", type=int, default=20); p.add_argument("-r", "--recursive", action="store_true"); p.set_defaults(fn=cmd_ls)
    p = sub.add_parser("summary"); p.add_argument("trace"); p.add_argument("-n", type=int, default=12); p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("tree"); p.add_argument("trace"); p.add_argument("--id"); p.add_argument("--depth", type=int, default=6)
    p.add_argument("--min-pct", type=float, default=1.0); p.add_argument("--no-merge", action="store_true"); p.set_defaults(fn=cmd_tree)
    p = sub.add_parser("hot"); p.add_argument("trace"); p.add_argument("-n", type=int, default=25)
    p.add_argument("--sort", choices=["self", "wall", "calls", "cumul"], default="self"); p.set_defaults(fn=cmd_hot)
    p = sub.add_parser("find"); p.add_argument("trace"); p.add_argument("pattern"); p.add_argument("-n", type=int, default=30)
    p.add_argument("--errors", action="store_true"); p.set_defaults(fn=cmd_find)
    p = sub.add_parser("node"); p.add_argument("trace"); p.add_argument("id"); p.add_argument("--width", type=int, default=400)
    p.add_argument("-n", type=int, default=30); p.add_argument("--frames", action="store_true"); p.set_defaults(fn=cmd_node)
    p = sub.add_parser("errors"); p.add_argument("trace"); p.set_defaults(fn=cmd_errors)
    p = sub.add_parser("locals"); p.add_argument("trace"); p.add_argument("id"); p.add_argument("--width", type=int, default=200); p.set_defaults(fn=cmd_locals)
    p = sub.add_parser("path"); p.add_argument("trace"); p.add_argument("id"); p.set_defaults(fn=cmd_path)
    p = sub.add_parser("where"); p.add_argument("trace", nargs="?", default=None); p.add_argument("target")
    p.add_argument("--root", default="."); p.set_defaults(fn=cmd_where)
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("-n", type=int, default=25); p.set_defaults(fn=cmd_diff)
    a = ap.parse_args()
    try:
        a.fn(a)
    except BrokenPipeError:  # piped into head
        pass


if __name__ == "__main__":
    main()
