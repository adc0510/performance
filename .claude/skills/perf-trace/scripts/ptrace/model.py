"""Trace loading, timing rules, value rendering and locals replay shared by every command."""

from __future__ import annotations

import fnmatch
import json
import sys
from collections import defaultdict

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
        self.params: dict[str, list] = {}  # function name -> positional parameter names, from any call that recorded them
        for n in self.nodes:
            if n.get("params") and n["name"] not in self.params:
                self.params[n["name"]] = n["params"]
        self.t0 = self.root.get("start", 0)
        self.t_end = self.t0 + (self.root.get("time") or max(
            (n.get("start", 0) + (n.get("time") or 0) - self.t0 for n in self.nodes), default=0))

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

    # ── navigation / time helpers ──

    def span(self, node: dict) -> tuple[float, float]:
        """(start, end) of a call; an unfinished call runs to the end of the trace."""
        s = node.get("start", self.t0)
        t = node.get("time")
        return s, (s + t + (node.get("capture_time") or 0)) if t is not None else self.t_end

    def rel(self, t: float) -> str:
        """Offset from the root's start, for timelines."""
        return fmt_t(max(t - self.t0, 0.0))

    def ancestors(self, node: dict) -> list[dict]:
        out, p = [], node["_parent"]
        while p is not None:
            out.append(p)
            p = p["_parent"]
        return out

    def find(self, pattern: str) -> list[dict]:
        return [n for n in self.nodes if match(n["name"], pattern)]

    def captured(self, node: dict) -> bool:
        return ("args" in node or "kwargs" in node or "result" in node) and not node.get("capture_skipped")


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


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


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
    params = node.get("params") or tr.params.get(node["name"]) or []
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


def match(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name, pattern) if any(ch in pattern for ch in "*?[") else pattern in name


_match = match  # old name


# ── value structure helpers ──────────────────────────────────────────────────


def canon(value) -> str:
    """Canonical JSON text: equal captured values give equal strings."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def walk(value, path: tuple = (), depth: int = 0, max_depth: int = 30):
    """Yield (path, sub-value) for the value and everything inside it."""
    yield path, value
    if depth >= max_depth:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if k != "__type__":
                yield from walk(v, path + (k,), depth + 1, max_depth)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, path + (i,), depth + 1, max_depth)


def fmt_path(path) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if str(p).isidentifier() else f"[{json.dumps(p)}]")
    return out


def is_marker(value) -> bool:
    """perf's placeholder for a value it did not capture in full (see trace-format.md)."""
    if isinstance(value, str):
        return value.startswith("<") and value.endswith(">")
    return isinstance(value, dict) and ("__spilled__" in value or "__ref__" in value)


def is_trivial(value) -> bool:
    """Values too common to say anything about where data came from (None, flags, 0/1, '')."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return -1 <= value <= 1
    if isinstance(value, str):
        return len(value) < 2
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def diff_values(a, b, path: tuple = (), out: list | None = None, limit: int = 200) -> list:
    """[(path, before, after)] where two captured values differ. A missing side is the string '<absent>'."""
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            if k not in b:
                out.append((path + (k,), a[k], "<absent>"))
            elif k not in a:
                out.append((path + (k,), "<absent>", b[k]))
            else:
                diff_values(a[k], b[k], path + (k,), out, limit)
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            if i >= len(b):
                out.append((path + (i,), a[i], "<absent>"))
            elif i >= len(a):
                out.append((path + (i,), "<absent>", b[i]))
            else:
                diff_values(a[i], b[i], path + (i,), out, limit)
    elif a != b or type(a) is not type(b):
        out.append((path, a, b))
    return out


def type_name(value) -> str:
    """Python-ish type of a captured value."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        if value.startswith("<") and value.endswith(">"):
            inner = value[1:-1].split(" ")[0].split(":")[-1]
            return f"{inner} (repr only)" if inner else "str"
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        if "__type__" in value:
            return str(value["__type__"])
        if "__class__" in value:
            return "type"
        if "__enum__" in value:
            return "Enum"
        if "__spilled__" in value:
            return "<spilled>"
        return "dict"
    return type(value).__name__


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
        row = {"line": e["line"], "set": {}, "edited": [], "before": {}}
        for name, value in (e.get("set") or {}).items():
            row["set"][name] = current[name] = tr.resolve(value)
        for name, ops in (e.get("changed") or {}).items():
            value = base(name)
            row["before"][name] = value
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


def replay_state(node: dict, tr: Trace):
    """Yield (row, state) per locals entry; state = every local known right after that line."""
    state = dict(named_args(node, tr))
    for row in expand_locals(node, tr):
        state.update(row["set"])
        for name in row.get("deleted") or ():
            state.pop(name, None)
        yield row, dict(state)


def parse_literal(text: str):
    """A command-line value: JSON when it parses (22, "A", null, [1,2]), else the plain string."""
    try:
        return json.loads(text)
    except ValueError:
        return text
