"""Data and state: value, flow, diverge, vdiff, at, iters, mutations, lines."""

from __future__ import annotations

import ast
import difflib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .model import (Trace, call_line, canon, canon_shared, diff_values, expand_locals, fmt_path, fmt_t, is_marker, is_trivial,
                    named_args, parse_literal, replay_state, short, walk)

# ── value: where does a value appear ─────────────────────────────────────────


def _same(v, target, contains: bool) -> bool:
    if contains and isinstance(target, str):
        return isinstance(v, str) and target in v
    if isinstance(v, bool) != isinstance(target, bool):
        return False
    if isinstance(target, (list, dict)) or isinstance(v, (list, dict)):
        return type(v) is type(target) and canon(v) == canon(target)
    return v == target and type(v) in (int, float, str, type(None), bool) and (isinstance(v, str) == isinstance(target, str))


def cmd_value(a):
    tr = Trace(a.trace)
    tr.load_spills = True  # search inside values spilled to SPILL_DIR too
    target = parse_literal(a.value)
    events = []
    uncaptured = 0
    for n in tr.nodes:
        s = n.get("start", tr.t0)
        e = s + n["time"] if n.get("time") is not None else tr.t_end
        if n.get("capture_skipped"):
            uncaptured += 1
        for name, v in named_args(n, tr):
            if is_marker(v):
                uncaptured += 1
            for path, sub in walk(v):
                if _same(sub, target, a.contains):
                    events.append((s, 0, 0, n, f"arg   {name}{fmt_path(path)}"))
        if "result" in n:
            r = tr.resolve(n["result"])
            if is_marker(r):
                uncaptured += 1
            for path, sub in walk(r):
                if _same(sub, target, a.contains):
                    events.append((e, 2, 0, n, f"result{fmt_path(path)}"))
        for i, y in enumerate(n.get("yielded") or []):
            for path, sub in walk(tr.resolve(y)):
                if _same(sub, target, a.contains):
                    events.append((s, 1, i, n, f"yielded[{i}]{fmt_path(path)}"))
        if isinstance(target, str) and target in (n.get("exception") or ""):
            events.append((e, 2, 0, n, "exception text"))
        if a.locals and n.get("locals"):
            holding: set = set()
            for i, row in enumerate(expand_locals(n, tr)):
                for name, v in row["set"].items():
                    now = {(name,) + p for p, sub in walk(v) if _same(sub, target, a.contains)}
                    for key in sorted(now - holding, key=str):
                        events.append((s, 1, i, n, f"local {key[0]}{fmt_path(key[1:])} at L{row['line']}: {short(tr.line_text(n, row['line']).strip(), 60)}"))
                    holding = {k for k in holding if k[0] != name} | now
    events.sort(key=lambda ev: (ev[0], ev[1], -ev[3]["_depth"] if ev[1] == 2 else ev[3]["_id"], ev[2]))
    print(f"{len(events)} places hold {short(target, 60, True)}" + (" (substring)" if a.contains else "")
          + ("" if a.locals else "  -- args/results/yields only; add --locals to search TRACE_LOCALS too"))
    for k, (t, _, _, n, where) in enumerate(events[: a.n]):
        tag = "FIRST" if k == 0 else ""
        print(f"{tag:5} +{tr.rel(t):>9}  #{n['_id']:<6} {short(n['name'], 50):50}  {where}")
    if len(events) > a.n:
        print(f"  ... {len(events) - a.n} more (use -n)")
    if events:
        n = events[0][3]
        print(f"\nfirst seen in #{n['_id']} {n['name']}: if it arrived there as an argument, look at the caller"
              f" (`path {a.trace} {n['_id']}`); if as a result or local, the value was made inside that call.")
    print("order: by time; args at call start, results at call end. Locals rows carry their call's start time, so"
          " they are ordered only within their call.")
    if uncaptured:
        print(f"blind spot: {uncaptured} values were not captured (capture_skipped or a <TypeName> marker) -- the value may also be there.")


# ── flow: where an argument came from, where a result went ───────────────────


def _distinctive(v) -> bool:
    if is_trivial(v) or is_marker(v):
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return abs(v) >= 2
    if isinstance(v, str):
        return len(v) >= 3
    return True


def _subs(value, max_depth: int = 6) -> dict[str, tuple]:
    """canon -> path for the distinctive sub-values of a value."""
    out: dict[str, tuple] = {}
    for path, sub in walk(value, max_depth=max_depth):
        if _distinctive(sub) and (path == () or isinstance(sub, (list, dict, str))):
            out.setdefault(canon(sub), path)
    return out


def _bindings(parent: dict, tr: Trace, wanted: str, callee: str | None = None) -> tuple[list[tuple], bool]:
    """Where the parent's locals held a value: ([(line, var)], exact). With a callee name, rows whose
    source line calls it are exact; otherwise every row holding an equal value is listed."""
    if not parent or not parent.get("locals"):
        return [], False
    rows = []
    for row, state in replay_state(parent, tr):
        for name in row["set"]:
            if canon(state.get(name)) == wanted:
                rows.append((row["line"], name))
    if callee:
        fn = callee.replace(".<locals>.", ".").rsplit(".", 1)[-1]
        exact = [(ln, v) for ln, v in rows if re.search(rf"\b{re.escape(fn)}\s*\(", tr.line_text(parent, ln))]
        if exact:
            return exact, True
    return rows, False


def cmd_flow(a):
    tr = Trace(a.trace)
    x = tr.node(a.id)
    xs, xe = tr.span(x)
    print(f"#{x['_id']} {call_line(x, tr, 140)}")
    anc = set(id(p) for p in tr.ancestors(x))
    print("\ninputs: where each argument's value was seen before this call")
    for name, v in named_args(x, tr):
        if not _distinctive(v):
            print(f"  {name} = {short(v, 50, True)}  (too common to trace: None/flag/small number/marker)")
            continue
        c = canon(v)
        vsubs = _subs(v)
        found = []
        p = x["_parent"]
        while p is not None:  # handed down from a caller's argument
            for pname, pv in named_args(p, tr):
                subs = _subs(pv)
                if c in subs:
                    found.append(f"passed down from #{p['_id']} {p['name']} arg {pname}{fmt_path(subs[c])}")
                    break
            p = p["_parent"]
        producers = []
        for n in tr.nodes:
            if n is x or id(n) in anc or "result" not in n or tr.span(n)[1] > xs + 1e-9:
                continue
            r = tr.resolve(n["result"])
            if not _distinctive(r):
                continue
            rs = _subs(r)
            if c in rs:
                producers.append((tr.span(n)[1], n, f"result{fmt_path(rs[c])}"))
            elif canon(r) in vsubs:
                producers.append((tr.span(n)[1], n, f"whole result, now at {name}{fmt_path(vsubs[canon(r)])}"))
        for _, n, where in sorted(producers, key=lambda t: -t[0])[: a.n]:
            found.append(f"made by #{n['_id']} {n['name']} ({where}), ended +{tr.rel(tr.span(n)[1])}")
        held, _ = _bindings(x["_parent"], tr, c)
        for line, var in held[-3:]:
            found.append(f"caller's local `{var}` held it (after L{line}: {short(tr.line_text(x['_parent'], line).strip(), 50)})")
        print(f"  {name} = {short(v, 60, True)}")
        for f in found or ["no earlier traced call shows it: built by the caller's own code or untraced code"]:
            print(f"      {f}")
    print("\noutput: where the result went")
    if "result" not in x:
        print("  no result captured" + (" (raised)" if "exception" in x else ""))
    else:
        r = tr.resolve(x["result"])
        if not _distinctive(r):
            print(f"  result {short(r, 60, True)} is too common to follow")
        else:
            rs = _subs(r)
            bound, exact = _bindings(x["_parent"], tr, canon(r), x["name"])
            for line, var in bound[:5]:
                print(f"  {'bound to' if exact else 'equal to'} the caller's local `{var}` at L{line}: {short(tr.line_text(x['_parent'], line).strip(), 60)}"
                      + ("" if exact else "  (equal value only)"))
            p, prev = x["_parent"], x
            while p is not None and "result" in p:
                ps = _subs(tr.resolve(p["result"]))
                if canon(r) not in ps:
                    break
                print(f"  returned up through #{p['_id']} {p['name']} as result{fmt_path(ps[canon(r)])}")
                prev, p = p, p["_parent"]
            consumers = []
            for n in tr.nodes:
                if n is x or tr.span(n)[0] < xe - 1e-9:
                    continue
                for name, v in named_args(n, tr):
                    hit = None
                    for path, sub in walk(v, max_depth=6):
                        if isinstance(sub, (list, dict, str)) or path == ():
                            c = canon(sub) if _distinctive(sub) else None
                            if c is not None and c in rs and (rs[c] == () or isinstance(sub, (list, dict)) or len(str(sub)) >= 8):
                                hit = (path, rs[c])
                                break
                    if hit:
                        consumers.append((n, name, hit))
                        break
            for n, name, (path, rpath) in consumers[: a.n]:
                src = "result" + fmt_path(rpath)
                scalar = not isinstance(r if rpath == () else None, (list, dict)) and rpath == () and not isinstance(r, (list, dict))
                print(f"  +{tr.rel(tr.span(n)[0]):>9} #{n['_id']} {n['name']} got {src} as {name}{fmt_path(path)}"
                      + ("  (equal scalar: may be coincidence)" if scalar else ""))
            if len(consumers) > a.n:
                print(f"  ... {len(consumers) - a.n} more (use -n)")
            if not consumers:
                print("  no later traced call received it")
    print("\nmatching is by equal captured value, not object identity: a coincidentally equal value can show up,"
          " most often small numbers and short strings. 'bound to' (a caller line that calls this function) is exact.")


# ── diverge / vdiff: compare two calls by value and structure ────────────────


def _compare(ta: Trace, na: dict, tb: Trace, nb: dict, depth: int, width: int, indent: str = "") -> None:
    print(f"{indent}A #{na['_id']} {call_line(na, ta, 100)}   {fmt_t(na.get('time'))}")
    print(f"{indent}B #{nb['_id']} {call_line(nb, tb, 100)}   {fmt_t(nb.get('time'))}")
    if na["name"] != nb["name"]:
        print(f"{indent}  different functions")
    aa, ab = dict(named_args(na, ta)), dict(named_args(nb, tb))
    rows = []
    for k in list(aa) + [k for k in ab if k not in aa]:
        for path, x, y in diff_values(aa.get(k, "<absent>"), ab.get(k, "<absent>"), limit=12):
            rows.append(("arg", f"{k}{fmt_path(path)}", x, y))
    ra, rb = ta.resolve(na.get("result", "<none>")), tb.resolve(nb.get("result", "<none>"))
    for path, x, y in diff_values(ra, rb, limit=12):
        rows.append(("result", f"result{fmt_path(path)}", x, y))
    if na.get("exception") != nb.get("exception"):
        rows.append(("raised", "exception", na.get("exception"), nb.get("exception")))
    for kind, where, x, y in rows:
        print(f"{indent}  {kind:6} {where:30} A {short(x, width, True)}   B {short(y, width, True)}")
    if not rows:
        print(f"{indent}  same args, result and exception")
    ta_, tb_ = na.get("time") or 0, nb.get("time") or 0
    if ta_ and tb_ and max(ta_, tb_) / min(ta_, tb_) >= 1.5:
        slower = "B" if tb_ > ta_ else "A"
        print(f"{indent}  time   {slower} is {max(ta_, tb_) / min(ta_, tb_):.1f}× slower"
              + (": with equal values the difference is outside them (a wait, contention, a cold cache)" if not rows else ""))
    ka, kb = na.get("children", []), nb.get("children", [])
    sm = difflib.SequenceMatcher(a=[c["name"] for c in ka], b=[c["name"] for c in kb], autojunk=False)
    pairs = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            pairs += list(zip(ka[i1:i2], kb[j1:j2]))
        else:
            if i2 > i1:
                print(f"{indent}  calls only in A: " + ", ".join(f"#{c['_id']} {c['name']}" for c in ka[i1:i2][:6]) + (" …" if i2 - i1 > 6 else ""))
            if j2 > j1:
                print(f"{indent}  calls only in B: " + ", ".join(f"#{c['_id']} {c['name']}" for c in kb[j1:j2][:6]) + (" …" if j2 - j1 > 6 else ""))
    if depth <= 0:
        return
    for ca, cb in pairs:  # first child pair whose values differ: where the two runs part ways
        da = canon([ta.resolve(ca.get("args")), ta.resolve(ca.get("kwargs")), ta.resolve(ca.get("result")), ca.get("exception")])
        db = canon([tb.resolve(cb.get("args")), tb.resolve(cb.get("kwargs")), tb.resolve(cb.get("result")), cb.get("exception")])
        if da != db:
            print(f"{indent}  first differing child pair:")
            _compare(ta, ca, tb, cb, depth - 1, width, indent + "    ")
            return


def cmd_diverge(a):
    tr = Trace(a.trace)
    _compare(tr, tr.node(a.a), tr, tr.node(a.b), a.depth, a.width)


def cmd_vdiff(a):
    ta, tb = Trace(a.a), Trace(a.b)
    if a.fn:
        ca, cb = ta.find(a.fn), tb.find(a.fn)
        if not ca or not cb:
            sys.exit(f"{a.fn!r}: {len(ca)} calls in A, {len(cb)} in B")
        if a.k >= min(len(ca), len(cb)):
            sys.exit(f"--k {a.k}: only {len(ca)} calls in A and {len(cb)} in B")
        print(f"{a.fn}: {len(ca)} calls in A, {len(cb)} in B; comparing call {a.k} of each")
        na, nb = ca[a.k], cb[a.k]
    else:
        na, nb = ta.root, tb.root
    _compare(ta, na, tb, nb, a.depth, a.width)


# ── at / iters: locals state at a moment ─────────────────────────────────────


def _need_locals(n: dict) -> None:
    if not n.get("locals"):
        sys.exit(f"#{n['_id']} {n['name']} has no locals: re-run with TRACE_LOCALS matching it (see references/capture.md)")


def cmd_at(a):
    tr = Trace(a.trace)
    n = tr.node(a.id)
    _need_locals(n)
    hits = [(row, st) for row, st in replay_state(n, tr) if row["line"] == a.line]
    if not hits:
        lines = sorted({row["line"] for row in expand_locals(n, tr)})
        sys.exit(f"L{a.line} never recorded a change in #{n['_id']}. Recorded lines: {', '.join(map(str, lines))}")
    if not 1 <= a.hit <= len(hits):
        sys.exit(f"L{a.line} was recorded {len(hits)} times; --hit must be 1..{len(hits)}")
    row, state = hits[a.hit - 1]
    print(f"#{n['_id']} {n['name']}: state right after execution {a.hit} of {len(hits)} of"
          f" L{a.line}: {tr.line_text(n, a.line).strip()}")
    for name, v in state.items():
        mark = "*" if name in row["set"] else " "
        print(f"  {mark} {name} = {short(v, a.width, True)}")
    print("\n* = changed by this line. Variables never changed so far keep their argument value.")


def cmd_iters(a):
    tr = Trace(a.trace)
    n = tr.node(a.id)
    _need_locals(n)
    rows = expand_locals(n, tr)
    counts = Counter(r["line"] for r in rows)
    header = a.line
    if header is None:
        repeated = sorted(ln for ln, k in counts.items() if k >= 2)
        if not repeated:
            sys.exit("no line ran more than once with a change: no loop to split")
        loops = [ln for ln in repeated if re.match(r"^\s*(async\s+)?(for|while)\b", tr.line_text(n, ln))]
        header = loops[0] if loops else repeated[0]
    if counts.get(header, 0) == 0:
        sys.exit(f"L{header} never recorded a change")
    print(f"#{n['_id']} {n['name']}: iterations split at L{header}: {tr.line_text(n, header).strip()}  ({counts[header]} iterations)")
    chunks, cur, k = [], [], -1
    for row in rows:
        if row["line"] == header:
            chunks.append((k, cur))
            cur, k = [], k + 1
        cur.append(row)
    chunks.append((k, cur))
    for k, chunk in chunks:
        if not chunk:
            continue
        label = "before" if k < 0 else f"iter {k}"
        parts = []
        for row in chunk:
            sets = ", ".join(f"{name}{'~' if name in row['edited'] else '='}{short(v, a.width, True)}" for name, v in row["set"].items())
            if sets:
                parts.append(f"L{row['line']} {sets}")
        print(f"{label:>8}  " + "\n          ".join(parts))
    print("\nthe last chunk also holds what ran after the loop. '~' = changed in place.")


# ── mutations: which calls changed objects they were handed ──────────────────


def cmd_mutations(a):
    tr = Trace(a.trace)
    nodes = [n for n in tr.nodes if not a.fn or n["name"] == a.fn or a.fn in n["name"]]
    shown = 0
    print("certain: the callee's own TRACE_LOCALS show a parameter edited in place (the caller's object changed)")
    for n in nodes:
        if not n.get("locals"):
            continue
        params = set(n.get("params") or []) | set((n.get("kwargs") or {}).keys())
        for row in expand_locals(n, tr):
            for name in row["edited"]:
                if name in params and shown < a.n:
                    d = diff_values(row["before"].get(name), row["set"][name], limit=4)
                    print(f"  #{n['_id']} {n['name']} L{row['line']} {short(tr.line_text(n, row['line']).strip(), 50)}  {name}: "
                          + "; ".join(f"{fmt_path(p) or '(whole)'} {short(x, 30, True)}→{short(y, 30, True)}" for p, x, y in d))
                    shown += 1
    print("\nfrom the caller's locals: a local changed in place on a line that called traced code with it")
    for p in tr.nodes:
        if not p.get("locals"):
            continue
        for row in expand_locals(p, tr):
            text = tr.line_text(p, row["line"])
            for name in row["edited"]:
                before = None  # canonical text, computed only once a child call on this line needs it
                cands = []
                for c in p.get("children", []):
                    if (a.fn and a.fn not in c["name"]) or not tr.captured(c):
                        continue
                    fn = c["name"].replace(".<locals>.", ".").rsplit(".", 1)[-1]
                    if not re.search(rf"\b{re.escape(fn)}\s*\(", text):
                        continue  # this line does not call it
                    if before is None:
                        before = canon_shared(row["before"].get(name))
                    for argname, v in named_args(c, tr):
                        if canon_shared(v) == before:
                            cands.append((c, argname))
                            break
                if cands and shown < a.n:  # the latest matching call is the one this line made
                    c, argname = max(cands, key=lambda t: tr.span(t[0])[0])
                    d = diff_values(row["before"].get(name), row["set"][name], limit=4)
                    print(f"  #{c['_id']} {c['name']} got `{name}` as {argname} at L{row['line']} of #{p['_id']}, and it came back changed: "
                          + "; ".join(f"{fmt_path(q) or '(whole)'} {short(x, 30, True)}→{short(y, 30, True)}" for q, x, y in d))
                    shown += 1
    print("\ninferred: `self` differs between consecutive method calls on the same class (assumes one instance;"
          " the change happened during the earlier call or between the two)")
    last: dict[str, tuple] = {}
    for n in sorted(nodes, key=lambda n: tr.span(n)[0]):
        args = named_args(n, tr)
        if not args or args[0][0] != "self" or not isinstance(args[0][1], dict):
            continue
        cls = args[0][1].get("__type__")
        if cls in last:
            prev, pv = last[cls]
            d = diff_values(pv, args[0][1], limit=5)
            if d and shown < a.n:
                print(f"  {cls}: #{prev['_id']} {prev['name'].rsplit('.', 1)[-1]} → #{n['_id']} {n['name'].rsplit('.', 1)[-1]}: "
                      + "; ".join(f"{fmt_path(q)} {short(x, 30, True)}→{short(y, 30, True)}" for q, x, y in d))
                shown += 1
        last[cls] = (n, args[0][1])
    if shown >= a.n:
        print(f"... stopped at {a.n} (use -n)")


# ── lines: source annotated with what each line did ──────────────────────────


def _function_lines(path: str, first: int, name: str) -> tuple[int, int] | None:
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return None
    best = None
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and f.lineno <= first <= (f.end_lineno or f.lineno):
            if best is None or f.lineno > best.lineno:  # innermost
                best = f
    if best is None:
        return None
    start = min([d.lineno for d in getattr(best, "decorator_list", [])] + [best.lineno])
    return start, best.end_lineno or best.lineno


def cmd_lines(a):
    tr = Trace(a.trace)
    n = tr.node(a.id)
    _need_locals(n)
    rows = expand_locals(n, tr)
    hits = Counter(r["line"] for r in rows)
    values: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        for name, v in r["set"].items():
            vals = values[r["line"]][name]
            s = short(v, 24, True)
            if s not in vals:
                vals.append(s)
    exc_line = (n.get("exception_origin") or {}).get("line") if n.get("exception") else None
    path = tr.source_file(n)
    span = _function_lines(path, min(hits), n["name"].rsplit(".", 1)[-1]) if path else None
    if span:
        text = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        numbered = [(i, text[i - 1]) for i in range(span[0], span[1] + 1)]
    else:
        numbered = [(int(k), v) for k, v in sorted(tr.sources.get(n.get("source") or "", {}).get("lines", {}).items(), key=lambda kv: int(kv[0]))]
        print("(source file not readable: showing only the recorded lines)")
    print(f"#{n['_id']} {n['name']}  {path or ''}")
    for no, src in numbered:
        k = hits.get(no, 0)
        gutter = f"{k}×" if k else ("·" if src.strip() and not src.strip().startswith("#") else "")
        mark = "!!" if no == exc_line else "  "
        vals = "  ".join(f"{name}={'|'.join(vs[:3])}{'…' if len(vs) > 3 else ''}" for name, vs in values.get(no, {}).items())
        print(f"{gutter:>5} {mark} {no:5}  {short(src, 70):70}  {short(vals, a.width)}")
    print("\nN× = times the line ran AND changed a variable · '·' = no recorded change: it ran without changing"
          " anything, or did not run at all (not proof either way) · !! = where this call's exception was raised")
