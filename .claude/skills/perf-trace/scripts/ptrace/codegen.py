"""Fixing and verifying: to-test (optionally with mocked leaf calls), snapshot."""

from __future__ import annotations

import builtins
import re
import sys
from pathlib import Path

from .model import Trace, named_args, short
from .source import Defn, Index, trace_files

HELPERS = '''
class _Any:
    """Stands for a value the trace did not capture in full: compares equal to anything."""
    def __eq__(self, other):
        return True
    def __repr__(self):
        return "ANY"


ANY = _Any()


def _build(cls, fields):
    """An instance with the captured fields, without relying on __init__'s signature."""
    try:
        return cls(**fields)
    except Exception:
        obj = cls.__new__(cls)
        for k, v in fields.items():
            object.__setattr__(obj, k, v)
        return obj


def _plain(value):
    """A value in the shape perf captures it, so it can be compared with the recorded result."""
    import dataclasses, enum, inspect
    if isinstance(value, enum.Enum):
        return {"__enum__": f"{type(value).__qualname__}.{value.name}", "value": _plain(value.value)}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if inspect.isroutine(value):
        return {"__type__": "function", "qualname": value.__qualname__, "module": value.__module__}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {"__type__": type(value).__name__, **{f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}}
    if hasattr(value, "model_dump"):
        return {"__type__": type(value).__name__, **_plain(value.model_dump())}
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {"__type__": type(value).__name__, **{k: _plain(v) for k, v in vars(value).items()}}
    return value
'''


class Emitter:
    """Turns captured JSON values into Python source, collecting the imports they need."""

    def __init__(self, index: Index, near: Defn | None):
        self.index, self.near = index, near
        self.imports: set[str] = set()
        self.todos: list[str] = []

    def ref(self, qualname: str, module: str | None = None) -> str | None:
        top = qualname.split(".")[0]
        if module and module not in ("__main__",):
            self.imports.add(f"from {module} import {top}")
            return qualname
        d = self.index.lookup_class(top, self.near) or self.index.lookup(top, self.near.file if self.near else None)
        if d is None:
            if hasattr(builtins, top):
                return qualname
            return None
        self.imports.add(f"from {d.module} import {top}")
        return qualname

    def value(self, v, expected: bool = False) -> str:
        if isinstance(v, str) and v.startswith("<") and v.endswith(">"):
            if expected:
                return "ANY"
            self.todos.append(f"{v} was not captured (repr only): supply a real value")
            return f"None  # TODO {v}"
        if v is None or isinstance(v, (bool, int, float, str)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(self.value(x, expected) for x in v) + "]"
        if isinstance(v, dict):
            if "__spilled__" in v:
                self.imports.add("import json")
                return f"json.load(open({v['__spilled__']!r}))"
            if expected:  # compare against the captured shape directly
                return "{" + ", ".join(f"{k!r}: {self.value(x, True)}" for k, x in v.items()) + "}"
            if v.get("__type__") in ("function", "method") and "qualname" in v:
                if "<locals>" in str(v["qualname"]) or v["__type__"] == "method":
                    self.todos.append(f"{v['qualname']} is a nested function or bound method: pass it by hand")
                    return f"None  # TODO {v['qualname']}"
                name = self.ref(v["qualname"], v.get("module"))
                return name or f"None  # TODO import {v['qualname']}"
            if "__class__" in v:
                mod, _, q = v["__class__"].rpartition(".")
                return self.ref(q, mod) or f"None  # TODO class {v['__class__']}"
            if "__enum__" in v:
                q = v["__enum__"]
                name = self.ref(q.split(".")[0])
                return q if name else f"None  # TODO enum {q}"
            if "__functions__" in v:
                self.todos.append("a function registry was captured only by its keys")
                return f"None  # TODO registry of {v['__functions__']}"
            if "__type__" in v:
                cls = v["__type__"]
                fields = {k: x for k, x in v.items() if k != "__type__"}
                name = self.ref(cls)
                body = "{" + ", ".join(f"{k!r}: {self.value(x)}" for k, x in fields.items()) + "}"
                if not name:
                    self.todos.append(f"class {cls} not found under --root: import it and build it by hand")
                    return f"{body}  # TODO was a {cls}"
                return f"_build({name}, {body})"
            return "{" + ", ".join(f"{k!r}: {self.value(x)}" for k, x in v.items()) + "}"
        return repr(v)

    def exception(self, text: str) -> tuple[str, str]:
        """(class source, message) for a captured exception repr like OutOfStock('C: wanted 5')."""
        m = re.match(r"^([\w.]+)\((.*)\)$", text, re.S)
        cls = m.group(1) if m else "Exception"
        msg = ""
        if m and m.group(2):
            try:
                import ast
                args = ast.literal_eval("(" + m.group(2) + ",)")
                msg = str(args[0]) if args else ""
            except (ValueError, SyntaxError):
                msg = ""
        name = self.ref(cls.split(".")[-1])
        if not name:
            self.todos.append(f"exception class {cls} not found: using Exception")
            name = "Exception"
        return name, msg


def _ident(text: str) -> str:
    return re.sub(r"\W", "_", text)


def _defn_for(tr: Trace, n: dict, index: Index) -> Defn:
    hint = tr.source_file(n) or (n.get("exception_origin") or {}).get("file")
    d = index.lookup(n["name"], hint, trace_files(tr))
    if d is None:
        sys.exit(f"could not find {n['name']!r} under {index.root}: pass --root (the directory you import from)")
    if not d.importable:
        sys.exit(f"{n['name']} is a nested function: it cannot be imported. Test its enclosing function instead.")
    return d


def _call_src(tr: Trace, n: dict, d: Defn, em: Emitter, indent: str) -> tuple[list[str], str]:
    """Statements that rebuild the arguments, and the call expression."""
    lines, pos, kw = [], [], []
    args = named_args(n, tr)
    kwargs = set((n.get("kwargs") or {}).keys())
    is_method = "." in d.qualname and d.kind == "function"
    owner = d.qualname.rsplit(".", 1)[0] if is_method else None
    target = d.top
    em.imports.add(f"from {d.module} import {target}")
    for name, v in args:
        var = re.sub(r"\W", "_", name)
        lines.append(f"{indent}{var} = {em.value(v)}")
        (kw if name in kwargs else pos).append((name, var))
    callee = d.qualname
    if is_method and pos and pos[0][0] in ("self", "cls"):
        callee = f"{pos[0][1]}.{d.qualname.rsplit('.', 1)[1]}"
        pos = pos[1:]
    elif is_method:
        callee = d.qualname  # static/class method or self not captured
    expr = f"{callee}(" + ", ".join([v for _, v in pos] + [f"{k}={v}" for k, v in kw]) + ")"
    if d.is_async:
        em.imports.add("import asyncio")
        expr = f"asyncio.run({expr})"
    return lines, expr


def _assert_src(tr: Trace, n: dict, em: Emitter, expr: str, indent: str) -> list[str]:
    if "exception" in n:
        cls, msg = em.exception(n["exception"])
        em.imports.add("import pytest")
        em.imports.add("import re")
        return [f"{indent}with pytest.raises({cls}, match=re.escape({msg!r})):", f"{indent}    {expr}"]
    if "result" not in n:
        return [f"{indent}{expr}  # result was not captured (capture_skipped or CAPTURE_IO=False)"]
    return [f"{indent}result = {expr}", f"{indent}assert _plain(result) == {em.value(tr.resolve(n['result']), expected=True)}"]


def _mock_src(tr: Trace, n: dict, index: Index, em: Emitter, indent: str) -> list[str]:
    """monkeypatch every direct leaf child with a fake that replays its captured results in order."""
    out = []
    groups: dict[str, list[dict]] = {}
    for c in n.get("children", []):
        if not c.get("children"):
            groups.setdefault(c["name"], []).append(c)
    for name, calls in groups.items():
        d = index.lookup(name, None, [em.near.file] + trace_files(tr) if em.near else trace_files(tr))
        if d is None or not d.importable:
            out.append(f"{indent}# not mocked: {name} (definition not found or nested)")
            continue
        var = "_" + re.sub(r"\W", "_", d.qualname)
        replies = []
        for c in calls:
            if "exception" in c:
                cls, msg = em.exception(c["exception"])
                replies.append(f"(None, {cls}({msg!r}))")
            elif "result" in c:
                replies.append(f"({em.value(tr.resolve(c['result']))}, None)")
            else:
                replies.append("(None, None)  # TODO result not captured")
        out.append(f"{indent}{var}_replies = iter([")
        out += [f"{indent}    {r}," for r in replies]
        out.append(f"{indent}])")
        out.append(f"{indent}{'async ' if d.is_async else ''}def {var}_fake(*args, **kwargs):")
        out.append(f"{indent}    result, exc = next({var}_replies)")
        out.append(f"{indent}    if exc is not None:")
        out.append(f"{indent}        raise exc")
        out.append(f"{indent}    return result")
        if "." in d.qualname:
            cls, meth = d.qualname.rsplit(".", 1)
            em.imports.add(f"from {d.module} import {cls.split('.')[0]}")
            out.append(f"{indent}monkeypatch.setattr({cls}, {meth!r}, {var}_fake)")
        else:
            em.imports.add(f"import {d.module} as _mod_{d.module.replace('.', '_')}")
            out.append(f"{indent}monkeypatch.setattr(_mod_{d.module.replace('.', '_')}, {d.qualname!r}, {var}_fake)")
        out.append("")
    return out


def _header(tr: Trace, what: str, em: Emitter, index: Index) -> list[str]:
    lines = [f'"""{what}', "", f"Generated by perf_trace.py from {tr.path}.",
             "Arguments are rebuilt from their captured state at entry; the expected value is the captured result.",
             "Captured tuples/sets come back as lists and objects are rebuilt field by field: review before trusting.",
             f"Run from {index.root} so the imports resolve (python -m pytest <this file>).", '"""', ""]
    plain = sorted(i for i in em.imports if i.startswith("import "))
    froms: dict[str, set] = {}
    for i in em.imports:
        if i.startswith("from "):
            mod, _, names = i[5:].partition(" import ")
            froms.setdefault(mod, set()).add(names)
    lines += plain + [f"from {mod} import {', '.join(sorted(names))}" for mod, names in sorted(froms.items())]
    return lines


def cmd_to_test(a):
    tr = Trace(a.trace)
    index = Index(a.root)
    n = tr.node(a.id)
    d = _defn_for(tr, n, index)
    em = Emitter(index, d)
    body = []
    fixture = "monkeypatch" if a.mock_leaves else ""
    body.append(f"def test_{_ident(d.qualname)}_trace_node_{n['_id']}({fixture}):")
    if a.mock_leaves:
        body += _mock_src(tr, n, index, em, "    ")
    lines, expr = _call_src(tr, n, d, em, "    ")
    body += lines
    body += _assert_src(tr, n, em, expr, "    ")
    out = _header(tr, f"Regression test for {n['name']} from trace node #{n['_id']}.", em, index) + [HELPERS] + body
    if em.todos:
        out += ["", "# TODO before this test can pass:"] + [f"#   - {t}" for t in dict.fromkeys(em.todos)]
    _write(a, "\n".join(out) + "\n")


def cmd_snapshot(a):
    tr = Trace(a.trace)
    index = Index(a.root)
    calls = [n for n in tr.nodes if n["name"] == a.fn or (a.fn not in [x["name"] for x in tr.nodes] and a.fn in n["name"])]
    calls = [n for n in calls if tr.captured(n) and ("result" in n or "exception" in n)]
    if not calls:
        sys.exit(f"no captured calls of {a.fn!r} (need args and a result or exception)")
    d = _defn_for(tr, calls[0], index)
    em = Emitter(index, d)
    seen, cases, skipped = set(), [], 0
    for n in calls:
        key = short([tr.resolve(n.get("args")), tr.resolve(n.get("kwargs"))], 10 ** 9)
        if key in seen:
            continue
        seen.add(key)
        todo_before = len(em.todos)
        lines, expr = _call_src(tr, n, d, em, "    ")
        if len(em.todos) > todo_before and not a.keep_todo:
            skipped += 1
            em.todos = em.todos[:todo_before]
            continue
        cases.append((n, lines, expr))
        if len(cases) >= a.n:
            break
    body = []
    for n, lines, expr in cases:
        body.append(f"def test_{_ident(d.qualname)}_call_{n['_id']}():")
        body += lines
        body += _assert_src(tr, n, em, expr, "    ")
        body.append("")
        body.append("")
    out = _header(tr, f"Golden-master tests for {d.qualname}: {len(cases)} captured calls"
                      + (f" ({skipped} skipped: their arguments were not fully captured)" if skipped else "") + ".", em, index)
    out += [HELPERS, ""] + body
    if em.todos:
        out += ["# TODO before these tests can pass:"] + [f"#   - {t}" for t in dict.fromkeys(em.todos)]
    _write(a, "\n".join(out).rstrip() + "\n")


def _write(a, text: str) -> None:
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"wrote {a.output}")
    else:
        sys.stdout.write(text)
