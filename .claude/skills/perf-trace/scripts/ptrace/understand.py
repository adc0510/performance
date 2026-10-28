"""Understanding code and hygiene: types, touched, sequence, secrets, advise."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .model import Trace, aggregate, expand_locals, fmt_path, fmt_t, match, named_args, short, type_name, walk
from .source import Index, trace_files

# ── types ────────────────────────────────────────────────────────────────────


def _shape(v, depth: int = 0) -> str:
    t = type_name(v)
    if depth >= 2:
        return t
    if t == "list" and v:
        inner = sorted({_shape(x, depth + 1) for x in v[:50]})
        return f"list[{' | '.join(inner)}]"
    if t == "dict" and v:
        inner = sorted({_shape(x, depth + 1) for x in list(v.values())[:50]})
        return f"dict[str, {' | '.join(inner)}]"
    return t


def _union(counter: Counter) -> str:
    types = [t for t, _ in counter.most_common()]
    if "None" in types and len(types) > 1:
        rest = [t for t in types if t != "None"]
        return f"{' | '.join(rest)} | None"
    return " | ".join(types)


def cmd_types(a):
    tr = Trace(a.trace)
    by: dict[str, list] = defaultdict(list)
    for n in tr.nodes:
        if tr.captured(n) and (not a.fn or match(n["name"], a.fn)):
            by[n["name"]].append(n)
    if not by:
        print("no captured calls match")
        return
    for name, calls in sorted(by.items()):
        params: dict[str, Counter] = defaultdict(Counter)
        order: list[str] = []
        ret, raises = Counter(), Counter()
        for n in calls:
            for k, v in named_args(n, tr):
                if k not in params:
                    order.append(k)
                params[k][_shape(v)] += 1
            if "exception" in n:
                raises[n["exception"].split("(", 1)[0]] += 1
            elif "result" in n:
                ret[_shape(tr.resolve(n["result"]))] += 1
        sig = ", ".join(f"{k}: {_union(params[k])}" for k in order if k not in ("self", "cls"))
        flags = []
        if len(ret) > 1:
            flags.append("returns more than one type" + (" (sometimes None)" if "None" in ret else ""))
        if any(len(c) > 1 for k, c in params.items() if k not in ("self", "cls")):
            flags.append("an argument arrives with different types")
        print(f"{name}({sig}) -> {_union(ret) or '?'}   [{len(calls)} calls]"
              + (f"  raises {', '.join(f'{e}×{k}' for e, k in raises.items())}" if raises else ""))
        for f in flags:
            print(f"   !! {f}: " + "; ".join(f"{k} " + ", ".join(f"{t}×{c}" for t, c in params[k].most_common())
                                          for k in order if len(params[k]) > 1)
                  if "argument" in f else f"   !! {f}: " + ", ".join(f"{t}×{c}" for t, c in ret.most_common()))
    print("\nobserved in this trace only: other inputs may bring other types. '(repr only)' = a library object perf"
          " showed by its repr; tuples/sets are captured as lists.")


# ── touched ──────────────────────────────────────────────────────────────────


def cmd_touched(a):
    tr = Trace(a.trace)
    index = Index(a.root)
    stats = aggregate(tr)
    per_file: dict[str, list] = defaultdict(list)
    unknown = []
    prefer = trace_files(tr, index)
    for name, s in stats.items():
        hint = next((tr.source_file(n) for n in tr.nodes if n["name"] == name and tr.source_file(n)), None)
        d = index.lookup(name, hint, prefer)
        if d is None:
            unknown.append((name, s))
        else:
            per_file[d.file].append((d, name, s))
    total = tr.root.get("time") or 0
    print(f"{len(stats)} functions ran, in {len(per_file)} files under {index.root}")
    for f, items in sorted(per_file.items(), key=lambda kv: -sum(x[2]["self"] for x in kv[1])):
        own = sum(x[2]["self"] for x in items)
        rel = Path(f).resolve().relative_to(index.root) if Path(f).resolve().is_relative_to(index.root) else f
        print(f"\n{rel}   self {fmt_t(own)} ({own / total * 100 if total else 0:.0f}%)")
        for d, name, s in sorted(items, key=lambda x: x[0].line):
            print(f"  {d.line:5}  {s['calls']:5}×  self {fmt_t(s['self']):>9}  {d.qualname}")
        if a.unreached:
            ran = {d.qualname for d, _, _ in items}
            missing = [d for d in index.files.get(f, []) if d.kind == "function" and d.qualname not in ran]
            if missing:
                print(f"  not reached in this trace: {', '.join(d.qualname for d in missing[:20])}{' …' if len(missing) > 20 else ''}")
    if unknown:
        print(f"\nnot found under {index.root} (library, generated or renamed code): "
              + ", ".join(f"{n} ×{s['calls']}" for n, s in unknown[:15]))
    print("\none trace = one path through the code. 'not reached' means not on THIS request's path, not dead code."
          " Functions excluded by SKIP_FUNCTIONS/LOG_FUNCTIONS also look unreached.")


# ── sequence ─────────────────────────────────────────────────────────────────


def _participant(name: str, by: str) -> str:
    parts = name.replace(".<locals>.", ".").split(".")
    if len(parts) >= 2 and parts[-2][:1].isupper():
        return parts[-2]  # Class.method: the object's lifeline
    if by == "function" or len(parts) < 2:
        return parts[-1]
    return parts[-2]


def _label(name: str) -> str:
    return name.replace(".<locals>.", ".").split(".")[-1]


def cmd_sequence(a):
    tr = Trace(a.trace)
    top = tr.node(a.id) if a.id is not None else tr.root
    total = top.get("time") or 0
    out = ["sequenceDiagram"]
    seen = []

    def part(name):
        p = re.sub(r"\W", "_", _participant(name, a.by)) or "root"
        if p not in seen:
            seen.append(p)
        return p

    def clean(text):
        return short(text, a.width).replace(";", ",").replace("#", "").replace("\n", " ")

    def emit(n, depth, indent):
        caller = part(n["name"])
        kids = n.get("children", [])
        groups: list[list[dict]] = []
        for c in kids:
            if groups and groups[-1][0]["name"] == c["name"]:
                groups[-1].append(c)
            else:
                groups.append([c])
        # overlapping groups run concurrently: wrap them in par blocks
        i = 0
        while i < len(groups):
            block = [groups[i]]
            end = max(tr.span(c)[1] for c in groups[i])
            j = i + 1
            while j < len(groups) and min(tr.span(c)[0] for c in groups[j]) < end:
                block.append(groups[j])
                end = max(end, max(tr.span(c)[1] for c in groups[j]))
                j += 1
            block = [g for g in block if total == 0 or sum(c.get("time") or 0 for c in g) / total * 100 >= a.min_pct]
            if len(block) > 1:
                out.append(f"{indent}par")
            for k, g in enumerate(block):
                if len(block) > 1 and k:
                    out.append(f"{indent}and")
                first = g[0]
                callee = part(first["name"])
                t = sum(c.get("time") or 0 for c in g)
                label = (f"{_label(first['name'])} ×{len(g)}" if len(g) > 1
                         else f"{_label(first['name'])}({', '.join(short(v, 20, True) for _, v in named_args(first, tr))})")
                ind = indent + ("    " if len(block) > 1 else "")
                out.append(f"{ind}{caller}->>+{callee}: {clean(label)}")
                if depth < a.depth and len(g) == 1:
                    emit(first, depth + 1, ind)
                raised = [c for c in g if "exception" in c]
                if raised and len(raised) == len(g):
                    out.append(f"{ind}{callee}--x-{caller}: {clean('raised ' + raised[0]['exception'])}")
                elif raised:
                    out.append(f"{ind}{callee}-->>-{caller}: {clean(f'{fmt_t(t)}, {len(raised)} of {len(g)} raised ' + raised[0]['exception'])}")
                else:
                    res = short(tr.resolve(first.get("result")), 30, True) if len(g) == 1 and "result" in first else ""
                    out.append(f"{ind}{callee}-->>-{caller}: {clean(fmt_t(t) + (' ' + res if res else ''))}")
            if len(block) > 1:
                out.append(f"{indent}end")
            i = j

    part(top["name"])
    emit(top, 1, "    ")
    head = ["sequenceDiagram"] + [f"    participant {p}" for p in seen]
    text = "\n".join(head + out[1:])
    print(text)


# ── secrets ──────────────────────────────────────────────────────────────────

PATTERNS = [
    ("AWS access key", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Stripe key", r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{16,}\b"),
    ("OpenAI/Anthropic-style key", r"\bsk-(?:ant-|proj-)?[0-9A-Za-z_-]{20,}\b"),
    ("GitHub token", r"\bgh[pousr]_[0-9A-Za-z]{30,}\b"),
    ("Slack token", r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("JWT", r"\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\b"),
    ("bearer token", r"(?i)\bbearer\s+[0-9A-Za-z._~+/-]{16,}=*"),
    ("private key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("password in URL", r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
    ("email address", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]
SENSITIVE_NAMES = re.compile(r"(?i)(pass(word|wd)?|secret|token|api_?key|auth|credential|cookie|session_?id|private)")
STRONG_NAMES = re.compile(r"(?i)(passw(or)?d|passwd|secret|token|api_?key|credential|private_?key|access_?key)")


def _looks_secret(name: str, value: str) -> bool:
    """A value stored under a sensitive name that looks like a credential, not a word or a type name."""
    if not isinstance(value, str) or (value.startswith("<") and value.endswith(">")) or " " in value.strip():
        return False
    if STRONG_NAMES.search(name):
        # a lowercase word or name ("operator", "block_begin", "name:endfor": a lexer's token types)
        # is a label, not a credential; real ones mix in digits, capitals or symbols
        return len(value) >= 4 and not re.fullmatch(r"#?[a-z]+([_:.-][a-z]+)*", value)
    if len(value) < 12:
        return False
    classes = sum(bool(re.search(r, value)) for r in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]"))
    return classes >= 3 or (classes >= 2 and len(value) >= 20)


def _mask(s: str) -> str:
    return s[:4] + "…" + f"({len(s)} chars)" if len(s) > 6 else "…"


def cmd_secrets(a):
    tr = Trace(a.trace)
    rx = [(label, re.compile(p)) for label, p in PATTERNS if a.emails or label != "email address"]
    hits = []  # (kind, where, masked, name)

    # perf stores identical content once (the "variables" pool) and TRACE_LOCALS rows repeat full
    # values line after line, so the same objects and strings come up again and again: each is
    # scanned once per enclosing key. memo keeps the object alive so its id can't be reused.
    memo: dict = {}

    def find(v, key, depth=0):
        """[(label, path, masked, key)] for v, whose nearest enclosing string key is `key`."""
        if isinstance(v, str):
            mk = (v, key)
            if mk not in memo:
                found = []
                if v and not (v.startswith("<") and v.endswith(">")):
                    for label, r in rx:
                        m = r.search(v)
                        if m:
                            found.append((label, (), _mask(m.group(0)), key))
                            break
                    else:
                        if key and SENSITIVE_NAMES.search(key) and _looks_secret(key, v):
                            found.append((f"value of a field named {key!r}", (), _mask(v), key))
                memo[mk] = found
            return memo[mk]
        if not isinstance(v, (dict, list)) or depth >= 30:
            return []
        mk = (id(v), key)
        hit = memo.get(mk)
        if hit is not None and hit[0] is v:
            return hit[1]
        found = []
        items = ((k, x, k) for k, x in v.items() if k != "__type__") if isinstance(v, dict) else \
            ((i, x, key) for i, x in enumerate(v))
        for step, x, sub_key in items:
            found += [(label, (step,) + path, masked, k) for label, path, masked, k in find(x, sub_key, depth + 1)]
        memo[mk] = (v, found)
        return found

    def scan(v, where):
        for label, path, masked, key in find(v, None):
            hits.append((label, f"{where}{fmt_path(path)}", masked, key))

    for n in tr.nodes:
        tag = f"#{n['_id']} {n['name']}"
        for name, v in named_args(n, tr):
            if SENSITIVE_NAMES.search(name) and _looks_secret(name, v):
                hits.append((f"argument named {name!r}", f"{tag} arg {name}", _mask(v), name))
            else:
                scan(v, f"{tag} arg {name}")
        if "result" in n:
            scan(tr.resolve(n["result"]), f"{tag} result")
        if n.get("exception"):
            scan(n["exception"], f"{tag} exception")
        tb = (tr.resolve(n.get("exception_debug")) or {}).get("formatted")
        if tb:
            scan(tb, f"{tag} traceback")
        if n.get("locals"):
            for row in expand_locals(n, tr):
                for name, v in row["set"].items():
                    if SENSITIVE_NAMES.search(name) and _looks_secret(name, v):
                        hits.append((f"local named {name!r}", f"{tag} local {name} L{row['line']}", _mask(v), name))
                    else:
                        scan(v, f"{tag} local {name} L{row['line']}")
    if not hits:
        print("nothing that looks like a credential" + ("" if a.emails else " (emails not checked: --emails)"))
        return
    groups: dict[tuple, list] = {}
    for kind, where, masked, _ in hits:  # one line per (kind, masked value): the same secret is often in many places
        groups.setdefault((kind, masked), []).append(where)
    print(f"{len(hits)} places hold {len(groups)} values that look sensitive (shown masked):")
    for (kind, masked), wheres in list(groups.items())[: a.n]:
        print(f"  {kind:28} {masked:22} {short(wheres[0], 90)}" + (f"  (+{len(wheres) - 1} more places)" if len(wheres) > 1 else ""))
    if len(groups) > a.n:
        print(f"  ... {len(groups) - a.n} more (use -n)")
    names = sorted({k for *_, k in hits if k and not k.startswith("arg")})
    kinds = sorted({k for k, *_ in hits if k in dict(PATTERNS)})
    print("\nto keep them out of future traces:")
    if names:
        print(f"  perf.SKIP_VARIABLES = {sorted({n if SENSITIVE_NAMES.search(n) else n for n in names})!r}  # by name: dropped, never converted")
    if kinds:
        print(f"  perf.REDACT_PATTERNS = {[dict(PATTERNS)[k] for k in kinds]!r}  # by content, everywhere incl. reprs/tracebacks")
    print("this trace file still holds them: delete it or keep it out of tickets, chats and git."
          " Never paste the unmasked values anywhere.")


# ── advise ───────────────────────────────────────────────────────────────────


def cmd_advise(a):
    tr = Trace(a.trace)
    total = tr.root.get("time") or 0
    cap = defaultdict(float)
    loc = defaultdict(float)
    calls = Counter()
    for n in tr.nodes:
        cap[n["name"]] += n.get("capture_time") or 0
        loc[n["name"]] += n.get("locals_capture_time") or 0
        calls[n["name"]] += 1
    c_tot, l_tot = sum(cap.values()), sum(loc.values())
    skipped = sum(1 for n in tr.nodes if n.get("capture_skipped"))
    dropped = [(n["_id"], n["name"], n["locals_dropped"]) for n in tr.nodes if n.get("locals_dropped")]
    kinds = ("truncated by MAX_SIZE_CAPTURE", "spilled to SPILL_DIR", "dropped (<TypeName>)", "depth")
    type_name = re.compile(r"^<\w[\w.]*>$")
    memo: dict = {}  # pooled values are shared objects, counted once per object (kept alive here)

    def count(v, depth=0) -> tuple:
        """How many of each kind of cut-off marker v holds (same walk as model.walk)."""
        if isinstance(v, str):
            if not v.startswith("<"):
                return (0, 0, 0, 0)
            if v.endswith(">") and "truncated" in v:
                return (1, 0, 0, 0)
            if type_name.match(v):
                return (0, 0, 1, 0)
            return (0, 0, 0, 1) if " object at 0x" in v else (0, 0, 0, 0)
        if not isinstance(v, (dict, list)):
            return (0, 0, 0, 0)
        hit = memo.get((id(v), depth))
        if hit is not None and hit[0] is v:
            return hit[1]
        out = [0, 0, 0, 0]
        if isinstance(v, dict) and "__spilled__" in v:
            out[1] += 1
        if depth < 30:
            for x in (y for k, y in v.items() if k != "__type__") if isinstance(v, dict) else v:
                for i, c in enumerate(count(x, depth + 1)):
                    out[i] += c
        memo[(id(v), depth)] = (v, tuple(out))
        return memo[(id(v), depth)][1]

    totals = [0, 0, 0, 0]
    for n in tr.nodes:
        for _, v in named_args(n, tr) + ([("result", tr.resolve(n["result"]))] if "result" in n else []):
            for i, c in enumerate(count(v)):
                totals[i] += c
    markers = Counter({k: c for k, c in zip(kinds[:3], totals[:3]) if c})
    depth_cut = totals[3]
    total += tr.root.get("capture_time") or 0  # the root's own capture runs outside its time
    pct = (c_tot + l_tot) / total * 100 if total else 0
    print(f"recording cost {fmt_t(c_tot + l_tot)} = {pct:.0f}% of the elapsed {fmt_t(total)}"
          f" (values {fmt_t(c_tot)}, TRACE_LOCALS {fmt_t(l_tot)})")
    tips = []
    if pct >= 20:
        heavy = sorted(cap.items(), key=lambda kv: -kv[1])[:5]
        tips.append("for TIMING: re-run with CAPTURE_IO=False and TRACE_LOCALS=[] -- the numbers in this trace are inflated.")
        tips.append("capture-heaviest functions: " + ", ".join(f"{k} {fmt_t(v)} over {calls[k]} calls" for k, v in heavy if v))
        many = [(k, c) for k, c in calls.most_common(3) if c >= 200]
        if many:
            tips.append(f"or keep values but cap repeats: MAX_CAPTURES_PER_FUNCTION = 50  (top call counts: "
                        + ", ".join(f"{k} ×{c}" for k, c in many) + ")")
    if l_tot:
        lheavy = sorted(loc.items(), key=lambda kv: -kv[1])[:5]
        tips.append("TRACE_LOCALS cost by function: " + ", ".join(f"{k} {fmt_t(v)}" for k, v in lheavy if v)
                    + " -- narrow the pattern to the one function you are debugging.")
    if skipped:
        tips.append(f"{skipped} calls were past MAX_CAPTURES_PER_FUNCTION (timed only): raise it if you need their values.")
    for nid, name, k in dropped[:3]:
        tips.append(f"#{nid} {name} lost {k} locals entries past MAX_LOCALS_EVENTS: raise it, or trace a smaller input.")
    for kind, k in markers.items():
        tips.append(f"{k} values {kind}: raise MAX_SIZE_CAPTURE / set SPILL_DIR if those values matter.")
    if depth_cut:
        tips.append(f"{depth_cut} values are default reprs (MAX_DEPTH reached or a library object): raise MAX_DEPTH if they are project data.")
    if len(tr.nodes) > 20000:
        tips.append(f"{len(tr.nodes)} calls in one file: LOG_FUNCTIONS = ['<the functions you care about>'] keeps them plus"
                    " their callers; SKIP_FUNCTIONS drops noisy helpers entirely (and their cost).")
    if not tips:
        tips.append("capture is cheap and nothing was cut: this setup is fine for both values and timing.")
    for t in tips:
        print(f"  - {t}")
    print("\nsettings: perf.<NAME> = … before the call, or @perf.trace(NAME=…) on the entry point. See references/capture.md.")
