"""TRACE_LOCALS: local variables recorded line by line."""

import threading

import perf
from _common import setup


EXPECT = {
    "new": True,
    "summary": "TRACE_LOCALS: per-line variable changes — rebinding, in-place mutation, self attributes, multi-assign, del, loops (+cap), exceptions, generators, recursion, threads, nested functions, redaction",
    "checks": [
        {"status": "works", "min_python": [3, 12], "desc": "each line that changes a variable is recorded with its file line, source text and new value (here: a = 1, b = 2, a = 3)",
         "node": "__main__.func", "result": 5, "has": ["locals"]},
        {"status": "works", "min_python": [3, 12], "desc": "...exact entries", "stdout": "*func locals: [(1, 'a = 1', {'a': 1}), (2, 'b = 2', {'b': 2}), (3, 'a = 3', {'a': 3})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "arguments are not repeated as entries (they are in args), but reassigning one is recorded",
         "stdout": "*params locals: [(1, 'x = x * 10', {'x': 20})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "in-place mutation of a mutable value (list.append) is recorded at the mutating line, with the new content",
         "stdout": "*mutation locals: [(1, 'items = []', {'items': []}), (2, 'items.append(1)', {'items': [1]}), (3, 'items.append(2)', {'items': [1, 2]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a method mutating self records `self` with its new state at that line",
         "stdout": "*method locals: [(1, 'self.count += 1', {'self': {'__type__': 'Counter', 'count': 1}})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "several names changed on one line land in one entry; `del` is recorded as deleted",
         "stdout": "*swap locals: [(1, 'a, b = 1, 2', {'a': 1, 'b': 2}), (2, 'a, b = b, a', {'a': 2, 'b': 1}), (3, 'del a', deleted ['a'])]*"},
        {"status": "works", "min_python": [3, 12], "desc": "re-assigning an equal immutable value is not a change, so it is not recorded (entries are changes, not assignments)",
         "stdout": "*same value locals: [(1, 'a = 1', {'a': 1})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "loops: one entry per line run that changed something (loop(3): acc=0, then i/acc per iteration — acc = 0 when it already is 0 is no change)",
         "stdout": "*loop entries: 6*"},
        {"status": "works", "min_python": [3, 12], "desc": "MAX_LOCALS_EVENTS caps the entries; the rest are counted in locals_dropped",
         "file": "capped", "node": "__main__.loop", "locals_dropped": 7},
        {"status": "works", "min_python": [3, 12], "desc": "a function that raises: changes up to the failing line are recorded, and the node records the exception",
         "stdout": "*raises locals: [(1, 'step = 1', {'step': 1})] exception: ValueError('boom')*"},
        {"status": "works", "min_python": [3, 12], "desc": "generators: changes are tracked across yields (total += 0 is no change, so it has no entry)",
         "stdout": "*gen locals: [(1, 'total = 0', {'total': 0}), (2, 'for i in range(3):', {'i': 0}), (2, 'for i in range(3):', {'i': 1}), (3, 'total += i', {'total': 1}), (2, 'for i in range(3):', {'i': 2}), (3, 'total += i', {'total': 3})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "recursion: each call (frame) gets its own list",
         "stdout": "*recursion entries per call: [1, 1, 1]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a matching function run on another thread is tracked too",
         "stdout": "*thread locals: [(1, 'a = 1', {'a': 1}), (2, 'b = 2', {'b': 2}), (3, 'a = 3', {'a': 3})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "nested functions / lambdas traced via events can be matched too (pattern on their node name)",
         "stdout": "*nested locals: [(2, 'z = y + 1', {'z': 6})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "SKIP_VARIABLES applies to locals; REDACT_PATTERNS applies to the recorded source text and values",
         "stdout": "*secret locals: [(1, 'password = <redacted>', {'password': '<skipped:password>'}), (2, 'user = \"ann\"', {'user': 'ann'})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "functions that don't match TRACE_LOCALS get no locals",
         "node": "__main__.not_matched", "lacks": ["locals"]},
        {"status": "works", "min_python": [3, 12], "desc": "the time spent tracking locals is reported (locals_capture_time) and excluded from the function's self time in the report",
         "node": "__main__.func", "has": ["locals_capture_time"]},
        {"status": "works", "min_python": [3, 12], "desc": "the trace's ROOT function can be matched too",
         "file": "root", "node": "root_locals", "has": ["locals"], "result": 7},
        {"status": "works", "min_python": [3, 12], "desc": "...its entries", "stdout": "*root locals: [(2, 'x = 3', {'x': 3}), (3, 'y = x + 4', {'y': 7})]*"},
        {"status": "limitation", "max_python": [3, 11], "desc": "TRACE_LOCALS needs Python 3.12+ (sys.monitoring line events): on older versions it is ignored with a warning",
         "stdout": "*TRACE_LOCALS needs Python 3.12+*"},
    ],
}


def func():
    a = 1
    b = 2
    a = 3
    return a + b


def not_matched():
    a = 1
    return a


def params(x):
    x = x * 10
    return x


def mutation():
    items = []
    items.append(1)
    items.append(2)
    return items


def swap():
    a, b = 1, 2
    a, b = b, a
    del a
    return b


def same_value():
    a = 1
    a = 1  # noqa — same object again: not a change
    return a


class Counter:
    def __init__(self):
        self.count = 0

    def bump(self):
        self.count += 1
        return self.count


def raises():
    step = 1
    raise ValueError("boom")


def gen():
    total = 0
    for i in range(3):
        total += i
        yield total


def loop(n):
    acc = 0
    for i in range(n):
        acc = i
    return acc


def rec(n):
    m = n - 1
    return 0 if m < 0 else rec(m)


def outer(y):
    def inner():
        z = y + 1
        return z
    return inner()


def secret():
    password = "hunter2"
    user = "ann"
    return user


PATTERNS = ["__main__.func", "__main__.params", "__main__.mutation", "__main__.swap", "__main__.same_value",
            "Counter.bump", "__main__.raises", "__main__.gen", "__main__.loop", "__main__.rec",
            "__main__.outer.<locals>.inner", "__main__.secret"]


@perf.trace(TRACE_LOCALS=PATTERNS, SKIP_VARIABLES=["password"], REDACT_PATTERNS=["hunter2", '"hunter2"'])
def main():
    func()
    not_matched()
    params(2)
    mutation()
    swap()
    same_value()
    Counter().bump()
    try:
        raises()
    except ValueError:
        pass
    list(gen())
    loop(3)
    rec(2)
    t = threading.Thread(target=func)
    t.start()
    t.join()
    outer(5)
    secret()


@perf.trace(TRACE_LOCALS=["__main__.loop"], MAX_LOCALS_EVENTS=3, OUTPUT_FILE="capped")
def capped():
    loop(5)


@perf.trace(TRACE_LOCALS=["root_locals"], OUTPUT_FILE="root")
def root_locals():
    x = 3
    y = x + 4
    return y


def entries(node, fn):
    """(line relative to the function's `def`, source text, change) — the trace
    itself holds real file line numbers; relative ones keep this test stable."""
    import inspect

    base = inspect.unwrap(fn).__code__.co_firstlineno
    out = []
    for e in node.get("locals", []):
        if "deleted" in e:
            out.append((e["line"] - base, e.get("text"), "deleted", e["deleted"]))
        else:
            out.append((e["line"] - base, e.get("text"), e.get("set")))
    return out


if __name__ == "__main__":
    import json

    from run_all import resolve, walk

    setup(__file__)
    main()
    capped()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    variables = d["variables"]
    nodes = [resolve(n, variables) for n in walk(d["trace"]) if n["name"] != "main"]

    def first(name):  # {} when absent (e.g. nested functions get no node before Python 3.12)
        return next((n for n in nodes if n["name"] == name), {})

    def show(label, node, fn):
        text = str(entries(node, fn)).replace("'deleted', ", "deleted ")
        print(f"{label} locals: {text}")

    show("func", first("__main__.func"), func)
    show("params", first("__main__.params"), params)
    show("mutation", first("__main__.mutation"), mutation)
    show("method", first("Counter.bump"), Counter.bump)
    show("swap", first("__main__.swap"), swap)
    show("same value", first("__main__.same_value"), same_value)
    print("loop entries:", len(first("__main__.loop").get("locals", [])))
    r = first("__main__.raises")
    print(f"raises locals: {entries(r, raises)} exception: {r.get('exception')}")
    show("gen", first("__main__.gen"), gen)
    print("recursion entries per call:", [len(n.get("locals", [])) for n in nodes if n["name"] == "__main__.rec"])
    thread_funcs = [n for n in nodes if n["name"] == "__main__.func"]
    show("thread", thread_funcs[1] if len(thread_funcs) > 1 else {}, func)
    show("nested", first("__main__.outer.<locals>.inner"), outer)
    show("secret", first("__main__.secret"), secret)
    root_locals()
    root = json.load(open(f"{perf.OUTPUT_DIR}/root.json"))
    show("root", resolve(root["trace"], root["variables"]), root_locals)
