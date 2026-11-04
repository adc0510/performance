"""replay: run ONE captured call again, on its own, from the arguments the trace recorded.

The trace holds every call's arguments at entry and its result or exception, so a
single function can be re-run in isolation -- no server, no request, no rest of the
program -- and checked against what it did in the recording:

* its callees can be faked from their recorded results (``--mock leaves`` default,
  ``children`` to isolate it completely, ``none`` to run everything for real); each
  fake also checks it was called with the arguments it got in the recording, which
  points at the first place the replay took a different path;
* ``--pdb`` stops in the debugger on the function's first line;
* ``--locals`` / ``--trace-out`` record the replay itself as a new perf trace (with the
  function's local variables line by line), to compare with the original (``vdiff``).

It generates a small script and runs it with the PROJECT's Python (``--python``,
default ``<root>/.venv/bin/python``), from ``--root``. ``-o`` keeps the script, to edit
the arguments and run it again by hand.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

from .codegen import HELPERS, Emitter, _call_src, _cut_off, _defn_for
from .model import Trace, short
from .source import Index, trace_files

RUNTIME = r'''
import asyncio as _asyncio
import inspect as _inspect
import sys as _sys

_REPORT = {"mock_calls": [], "mismatches": []}


class _Missing:
    """An argument the trace did not capture in full: any use of it fails loudly."""

    def __init__(self, what):
        object.__setattr__(self, "_what", what)

    def _fail(self, *a, **k):
        raise RuntimeError(f"replay: this value was not captured ({object.__getattribute__(self, '_what')}); "
                           "edit the replay script (-o) to supply it")

    __getattr__ = __call__ = __iter__ = __len__ = __getitem__ = __bool__ = __str__ = __int__ = __index__ = _fail
    __eq__ = __lt__ = __add__ = __contains__ = __hash__ = _fail

    def __repr__(self):
        return f"<not captured: {object.__getattribute__(self, '_what')}>"


def _missing(what):
    return _Missing(what)


def _resolve(module, qualname):
    """(owner, attribute name, raw attribute, plain function) for module:qualname."""
    import importlib
    owner = importlib.import_module(module)
    parts = qualname.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    raw = _inspect.getattr_static(owner, parts[-1])
    fn = raw
    if isinstance(fn, (staticmethod, classmethod)):
        fn = fn.__func__
    if isinstance(fn, property):
        fn = fn.fget
    return owner, parts[-1], raw, fn


def _first_diff(expected, got, path="result"):
    """Where two values in perf's captured shape first differ, or None."""
    if expected == got:
        return None
    if isinstance(expected, dict) and isinstance(got, dict):
        for k in expected:
            if k not in got:
                return f"{path}[{k!r}]: recorded {short(expected[k])}, missing now"
            d = _first_diff(expected[k], got[k], f"{path}[{k!r}]")
            if d:
                return d
        for k in got:
            if k not in expected:
                return f"{path}[{k!r}]: new now ({short(got[k])}), not in the recording"
    if isinstance(expected, list) and isinstance(got, list):
        for i, (e, g) in enumerate(zip(expected, got)):
            d = _first_diff(e, g, f"{path}[{i}]")
            if d:
                return d
        if len(expected) != len(got):
            return f"{path}: recorded {len(expected)} items, now {len(got)}"
    return f"{path}: recorded {short(expected)}, now {short(got)}"


def short(v, n=120):
    text = v if isinstance(v, str) and n >= 200 else repr(v)
    return text if len(text) <= n else text[: n - 1] + "…"


def _mock(module, qualname, replies, strip_first, is_async):
    """Replace module:qualname with a fake answering the recorded calls in order --
    on its owner and in every module that imported the same function by name."""
    owner, attr, raw, fn = _resolve(module, qualname)
    state = {"i": 0}

    def answer(args, kwargs):
        i = state["i"]
        state["i"] += 1
        _REPORT["mock_calls"].append(qualname)
        if i >= len(replies):
            raise AssertionError(f"replay: {qualname} was called {i + 1} times; the recording has {len(replies)} call(s)")
        rec_args, rec_kwargs, result, exc = replies[i]
        got = _plain(list(args[1:] if strip_first else args))
        if rec_args is not None and _plain(rec_args) != got:
            _REPORT["mismatches"].append(f"{qualname} call {i + 1}: {_first_diff(rec_args, got, 'args')}")
        if rec_kwargs is not None and rec_kwargs != _plain(dict(kwargs)):
            _REPORT["mismatches"].append(f"{qualname} call {i + 1}: {_first_diff(rec_kwargs, _plain(dict(kwargs)), 'kwargs')}")
        if exc is not None:
            raise exc
        return result

    if is_async:
        async def fake(*args, **kwargs):
            return answer(args, kwargs)
    else:
        def fake(*args, **kwargs):
            return answer(args, kwargs)
    fake.__qualname__ = fn.__qualname__ if fn is not None else qualname
    wrapped = staticmethod(fake) if isinstance(raw, staticmethod) else classmethod(fake) if isinstance(raw, classmethod) else fake
    setattr(owner, attr, wrapped)
    if fn is not None:  # `from mod import fn` copies elsewhere
        for m in list(_sys.modules.values()):
            d = getattr(m, "__dict__", None)
            if isinstance(d, dict):
                for k, v in list(d.items()):
                    if v is fn:
                        d[k] = fake


def _break_in(code):
    """Stop in pdb on the first line of `code` (the replayed function)."""
    import pdb

    def tracer(frame, event, arg):
        if event == "call" and frame.f_code is code:
            _sys.settrace(None)
            pdb.Pdb().set_trace(frame)
            return None
        return None

    _sys.settrace(tracer)
'''

MAIN = r'''

def _main():
    print(f"replaying #{NODE_ID} {NAME}  ({TRACE})")
    _setup_mocks()
    owner, attr, raw, fn = _resolve(MODULE, QUALNAME)
    if TRACE_OUT:
        try:
            import perf
        except ImportError:
            _sys.exit("replay: --locals/--trace-out need perf importable by this Python")
        cfg = {"OUTPUT_DIR": None, "OUTPUT_FILE": TRACE_OUT, "PRINT_REPORT": False}
        if LOCALS:
            cfg["TRACE_LOCALS"] = True
        perf.attach(fn, calls=1, **cfg)
    if PDB:
        _break_in(_inspect.unwrap(fn).__code__)
    try:
        outcome, value = "result", _call()
    except BaseException as e:  # noqa: BLE001 -- the replayed call's own outcome
        outcome, value = "exception", e
    finally:
        _sys.settrace(None)
    verdict = 0
    if outcome == "exception" and EXPECT_KIND == "exception":
        same = type(value).__name__ == EXPECT_CLASS and EXPECT_MESSAGE in str(value)
        print(f"raised   {type(value).__name__}: {short(str(value), 200)}")
        print(f"recorded {EXPECT_CLASS}: {short(EXPECT_MESSAGE, 200)}")
        verdict = 0 if same else 1
    elif outcome == "exception":
        import traceback
        traceback.print_exception(type(value), value, value.__traceback__)
        print(f"raised   {type(value).__name__}: {short(str(value), 200)}")
        print("recorded " + ("a result" if EXPECT_KIND == "result" else "no result (not captured)"))
        verdict = 1 if EXPECT_KIND == "result" else 2
    elif EXPECT_KIND == "exception":
        print(f"returned {short(_plain(value), 200)}")
        print(f"recorded {EXPECT_CLASS}: {short(EXPECT_MESSAGE, 200)}")
        verdict = 1
    elif EXPECT_KIND == "result":
        got = _plain(value)
        diff = _first_diff(EXPECTED, got)
        print(f"returned {short(got, 200)}")
        if diff:
            print(f"recorded {short(EXPECTED, 200)}")
            print(f"first difference: {diff}")
        verdict = 1 if diff else 0
    else:
        print(f"returned {short(_plain(value), 200)}  (the trace has no result to compare with)")
        verdict = 2
    calls = {}
    for q in _REPORT["mock_calls"]:
        calls[q] = calls.get(q, 0) + 1
    for q, (n, why) in MOCKED.items():
        used = calls.get(q, 0)
        note = "" if used == n else f"  <- recorded {n}"
        print(f"  faked {q}: called {used}x{note}")
    for m in _REPORT["mismatches"][:10]:
        print(f"  DIVERGED: {m}")
    if TRACE_OUT:
        print(f"replay trace: {TRACE_OUT}")
    print({0: "SAME as recorded", 1: "DIFFERENT from recorded", 2: "nothing recorded to compare with"}[verdict]
          + (" (but faked calls got different arguments)" if verdict == 0 and _REPORT["mismatches"] else ""))
    _sys.exit(verdict)


if __name__ == "__main__":
    _main()
'''


def _is_script(file: str) -> bool:
    """Whether importing this file would RUN something: top-level calls, loops or
    `with` blocks outside an `if __name__ == "__main__":` guard (a driver or entry
    script). Replay must not import those -- it would re-run the program."""
    import ast

    try:
        tree = ast.parse(Path(file).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return False
    harmless = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Pass)
    for stmt in tree.body:
        if isinstance(stmt, harmless):
            continue
        if isinstance(stmt, ast.If) and "__name__" in ast.unparse(stmt.test):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            value = stmt.value
            # NAME = <literal / name / attribute / a call>: a call at import time is how
            # modules build registries and clients, so only obvious program-running
            # calls count (perf.trace(...)(...), main(), run(...)).
            if value is None or not any(isinstance(n, ast.Call) and _runs_program(n) for n in ast.walk(value)):
                continue
            return True
        if isinstance(stmt, (ast.Try, ast.If)):
            continue  # optional imports, platform switches
        return True  # a bare call, loop, with-block...
    return False


def _runs_program(call) -> bool:
    import ast

    f = call.func
    if isinstance(f, ast.Call):  # perf.trace(fn)(...), decorator()(...)
        return True
    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
    return name in ("main", "run", "run_module", "run_path", "serve_forever")


def _children_to_mock(tr: Trace, n: dict, mode: str, keep: list[str]) -> list[dict]:
    if mode == "none":
        return []
    out = []
    for c in n.get("children", []):
        if mode == "leaves" and c.get("children"):
            continue
        if c["name"] == n["name"]:
            continue  # recursion: that's the function under replay itself
        if any(k == c["name"] or k in c["name"] for k in keep):
            continue
        out.append(c)
    return out


def _mock_setup_src(tr: Trace, n: dict, index: Index, em: Emitter, mode: str, keep: list[str]) -> tuple[list[str], dict, list[str]]:
    lines, mocked, skipped = ["def _setup_mocks():"], {}, []
    groups: dict[str, list[dict]] = {}
    for c in _children_to_mock(tr, n, mode, keep):
        groups.setdefault(c["name"], []).append(c)
    for name, calls in groups.items():
        d = index.lookup(name, tr.source_file(calls[0]), ([em.near.file] if em.near else []) + trace_files(tr, index))
        why = None
        if d is not None and em.near is not None and (d.module, d.qualname) == (em.near.module, em.near.qualname):
            continue  # recursion: the function under replay itself (root and callee names differ: `f` vs `mod.f`)
        if d is None:
            why = "definition not found under --root"
        elif re.fullmatch(r"__\w+__", d.qualname.rsplit(".", 1)[-1]):
            why = "a dunder (__init__ and co. set up real objects): runs for real"
        elif not d.importable:
            why = "nested function"
        elif _is_script(d.file):
            why = f"defined in {d.file}, a script: importing it would run it (runs for real)"
        elif any("yields" in c for c in calls) or d.binding == "contextmanager":
            why = "generator / context manager (runs for real)"
        elif d.binding == "property":
            why = "property (runs for real)"
        if why:
            skipped.append(f"{name}: {why}")
            continue
        method = "." in d.qualname and d.kind == "function"
        strip = method and d.binding != "staticmethod"
        replies = []
        for c in calls:
            args = tr.resolve(c.get("args")) if "args" in c else None
            if strip and isinstance(args, list):
                args = args[1:]
            kwargs = tr.resolve(c.get("kwargs")) if "kwargs" in c else ({} if "args" in c else None)
            rec_args = em.value(args, expected=True) if args is not None else "None"
            rec_kwargs = em.value(kwargs, expected=True) if kwargs is not None else "None"
            if "exception" in c:
                cls, msg = em.exception(c["exception"])
                replies.append(f"({rec_args}, {rec_kwargs}, None, {cls}({msg!r}))")
            elif "result" in c:
                replies.append(f"({rec_args}, {rec_kwargs}, {em.value(tr.resolve(c['result']))}, None)")
            else:
                replies.append(f"({rec_args}, {rec_kwargs}, None, None)")
        lines.append(f"    _mock({d.module!r}, {d.qualname!r}, [")
        lines += [f"        {r}," for r in replies]
        lines.append(f"    ], strip_first={strip!r}, is_async={d.is_async!r})")
        mocked[d.qualname] = (len(calls), "")
    if len(lines) == 1:
        lines.append("    pass")
    return lines, mocked, skipped


def build(a) -> tuple[str, dict]:
    tr = Trace(a.trace)
    index = Index(a.root)
    n = tr.node(a.id)
    why = _cut_off(n, tr.root)
    if why:
        sys.exit(f"#{n['_id']} {n['name']} did not finish on its own ({why}): replaying it would hang the same way."
                 " Replay a call that finished, or add a timeout to the script (-o) first.")
    if not tr.captured(n):
        sys.exit(f"#{n['_id']} {n['name']} has no captured arguments (CAPTURE_IO off, or capture_skipped): nothing to replay")
    d = _defn_for(tr, n, index)
    if _is_script(d.file):
        sys.exit(f"{n['name']} is defined in {d.file}, which runs code when imported (a driver/entry script):"
                 " replaying it would run the program. Move the function into a module, or replay one of its callees.")
    if d.binding == "contextmanager" or "yields" in n:
        sys.exit(f"{n['name']} is a generator / context manager: use `to-test` (it drives those), or replay its caller")
    em = Emitter(index, d, inline=True)
    body, expr = _call_src(tr, n, d, em, "    ")
    mock_lines, mocked, skipped = _mock_setup_src(tr, n, index, em, a.mock, a.keep or [])
    expect_kind, expected, ecls, emsg = "none", "None", "", ""
    if "exception" in n:
        expect_kind = "exception"
        ecls, emsg = em.exception(n["exception"])
    elif "result" in n:
        expect_kind = "result"
        expected = em.value(tr.resolve(n["result"]), expected=True)
    qualname = d.qualname
    trace_out = None
    if a.locals or a.trace_out:
        trace_out = str(Path(a.trace_out or (Path(a.trace).with_suffix("").as_posix() + f".replay-{n['_id']}.json")).resolve())
    header = [
        f'"""Replay of trace node #{n["_id"]} {n["name"]} from {tr.path}.',
        "",
        "Generated by perf_trace.py replay. Edit the arguments in _call() and run it again:",
        f"    {a.python or 'python'} {a.output or 'this_file.py'}",
        '"""',
        "",
        "import sys",
        f"sys.path.insert(0, {str(Path(a.root).resolve())!r})",
    ]
    imports = sorted(i for i in em.imports if i.startswith("import ")) + sorted(i for i in em.imports if i.startswith("from "))
    consts = [
        f"TRACE = {str(tr.path)!r}", f"NODE_ID = {n['_id']!r}", f"NAME = {n['name']!r}",
        f"MODULE = {d.module!r}", f"QUALNAME = {qualname!r}",
        f"EXPECT_KIND = {expect_kind!r}", f"EXPECT_CLASS = {getattr(ecls, '__name__', ecls).split('.')[-1]!r}",
        f"EXPECT_MESSAGE = {emsg!r}", f"MOCKED = {mocked!r}",
        f"PDB = {bool(a.pdb)!r}", f"LOCALS = {bool(a.locals)!r}", f"TRACE_OUT = {trace_out!r}",
    ]
    call = ["def _call():"] + body + [f"    return {expr}"]
    src = "\n".join(header + [HELPERS, RUNTIME] + imports + [""] + consts + ["", f"EXPECTED = {expected}", ""]
                    + mock_lines + [""] + call + [MAIN])
    return src, {"todos": list(dict.fromkeys(em.todos)), "skipped": skipped, "node": n, "defn": d}


def _python(a) -> str:
    if a.python:
        return a.python
    for cand in (Path(a.root) / ".venv" / "bin" / "python", Path(a.root) / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return str(cand.absolute())  # not resolve(): the venv python is a symlink
    return sys.executable


def cmd_replay(a):
    a.python = _python(a)
    src, info = build(a)
    for t in info["todos"]:
        print(f"warning: {t}", file=sys.stderr)
    for s in info["skipped"]:
        print(f"not faked: {s}", file=sys.stderr)
    if a.output:
        Path(a.output).write_text(src, encoding="utf-8")
        script = a.output
        print(f"wrote {a.output}", file=sys.stderr)
    else:
        fd, script = tempfile.mkstemp(prefix="perf_replay_", suffix=".py")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(src)
    if a.no_run:
        return
    cmd = [a.python, script]
    if not a.pdb:
        print("$ " + " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    try:
        rc = subprocess.call(cmd, cwd=a.root)
    finally:
        if not a.output:
            os.unlink(script)
    sys.exit(rc)
