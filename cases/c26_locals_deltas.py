"""TRACE_LOCALS deltas: an object changed in place is recorded as edits ("changed") to its previous value."""

import inspect
import json
import sys
import threading

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "TRACE_LOCALS deltas: in-place changes stored as edit ops (append, key set/delete, nested attribute, truncate, element) against the previous value or the recorded argument; locks are not changes; positional entries; fallbacks to a full value; perf.expand_locals rebuilds every value",
    "checks": [
        {"status": "works", "min_python": [3, 12], "desc": "list append: the first state is a full value, each later append is [\"+\", path, [new items]]",
         "stdout": "*appends: [(1, 'set', {'items': []}), (2, 'set', {'i': 0}), (3, 'set', {'items': [{'n': 0, 'tag': 'item-0'}]}), (2, 'set', {'i': 1}), (3, 'changed', {'items': [['+', [], [{'n': 1, 'tag': 'item-1'}]]]}), (2, 'set', {'i': 2}), (3, 'changed', {'items': [['+', [], [{'n': 2, 'tag': 'item-2'}]]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "dict: a new key and an updated key are [\"=\", [key], value], a deleted key is [\"-\", [key]]",
         "stdout": "*dict_ops: [(1, 'set', {'counts': {'apple': 1, 'banana': 2, 'cherry': 3}}), (2, 'changed', {'counts': [['=', ['damson'], 4]]}), (3, 'changed', {'counts': [['=', ['apple'], 11]]}), (4, 'changed', {'counts': [['-', ['banana']]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "object attributes: the path goes through the attribute (report.rows.append → [\"+\", [\"rows\"], ...], report.totals[k] = v → [\"=\", [\"totals\", k], v])",
         "stdout": "*nested_object: [(1, 'set', {'report': {'__type__': 'Report', 'rows': [], 'totals': {}}}), (2, 'set', {'sku': 'A'}), (3, 'changed', {'report': [['+', ['rows'], [{'sku': 'A', 'qty': 1}]]]}), (4, 'changed', {'report': [['=', ['totals', 'A'], 1]]}), (2, 'set', {'sku': 'B'}), (3, 'changed', {'report': [['+', ['rows'], [{'sku': 'B', 'qty': 1}]]]}), (4, 'changed', {'report': [['=', ['totals', 'B'], 1]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "list.pop() is [\"-\", [index]]; an element changed in a nested list is [\"=\", [i, j], value]",
         "stdout": "*truncate_and_element: [(1, 'set', {'stack': [10, 20, 30, 40, 50, 60, 70, 80]}), (2, 'changed', {'stack': [['-', [7]]]}), (3, 'changed', {'stack': [['-', [6]]]}), (4, 'set', {'grid': [[0, 0, 0], [0, 0, 0], [0, 0, 0]]}), (5, 'changed', {'grid': [['=', [1, 2], 9]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "full value instead of edits: insert(0, x), a rebinding, edits not smaller than the value, and an edit that == can't tell apart (1 → True)",
         "stdout": "*fallbacks: [*(3, 'set', {'front': [1, 2, 3, 4, 5, 6, 7, 8]}), (4, 'set', {'front': [0, 1, 2, 3, 4, 5, 6, 7, 8]}), (5, 'set', {'front': [9, 9]}), (6, 'set', {'tiny': []}), (7, 'set', {'tiny': [1]}), (8, 'set', {'flags': [1, 2, 3, 4, 5, 6, 7, 8]}), (10, 'set', {'flags': [True, 2, 3, 4, 5, 6, 7, 8, 9]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "an argument's first change is edits against its value in the node's \"args\" (the node lists its positional parameter names in \"params\")",
         "stdout": "*fallbacks: [(1, 'changed', {'arg_list': [['+', [], [99]]]}), (2, 'changed', {'arg_list': [['+', [], [100]]]}),*"},
        {"status": "works", "min_python": [3, 12], "desc": "...so a method changing self records only the edits, in every call (not a full copy of self per call)",
         "stdout": "*deposit: params ['self', 'amount'] [(2, 'changed', {'self': [['=', ['balance'], 5]]}), (3, 'changed', {'self': [['+', ['history'], ['+5']]]})]*deposit: params ['self', 'amount'] [(2, 'changed', {'self': [['=', ['balance'], 12]]}), (3, 'changed', {'self': [['+', ['history'], ['+7']]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a keyword-only argument's base is its value in \"kwargs\"",
         "stdout": "*kwonly: params [] [(1, 'changed', {'bag': [['+', [], ['e']]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a lock taken and released (with lock:) is not a change: its repr's state word flips, its identity doesn't — no entries for the lock, nor for self when self holds one (deposit above)",
         "stdout": "*locking: params ['lock', 'counter'] [(2, 'changed', {'counter': [['=', ['n'], 1]]}), (4, 'changed', {'counter': [['=', ['n'], 2]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a SKIP_VARIABLES argument stays a marker (never an edit against its recorded value)",
         "stdout": "*secret_arg: [(1, 'set', {'secret_list': '<skipped:secret_list>'})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "entries are written positionally: [line, set, changed, deleted, globals, deleted_globals], 0 for an empty part, trailing empty parts left out",
         "stdout": '*raw format: [[*, {"counts": {"apple": 1, "banana": 2, "cherry": 3}}], [*, 0, {"counts": [["=", ["damson"], 4]]}]]*'},
        {"status": "works", "min_python": [3, 12], "desc": "SKIP_VARIABLES: a skipped name stays a marker (never edits); REDACT_PATTERNS applies to the values inside edits",
         "stdout": "*skipped_and_redacted: [(1, 'set', {'secret_tokens': '<skipped:secret_tokens>'}), (2, 'set', {'secret_tokens': '<skipped:secret_tokens>'}), (3, 'set', {'log': ['start', 'connecting to the database', 'connected']}), (4, 'changed', {'log': [['+', [], ['login password=<redacted>']]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "after del and a new binding, edits are against the NEW object",
         "stdout": "*deleted_and_rebound: [(1, 'set', {'buf': [1, 2, 3, 4, 5, 6, 7, 8]}), (2, 'deleted', ['buf']), (3, 'set', {'buf': [7, 7, 7, 7, 7, 7, 7, 7]}), (4, 'changed', {'buf': [['+', [], [8]]]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "after a line with no call, store, in-place operator, iteration or await, locals are compared by identity only (fast) — every rebinding is still recorded",
         "stdout": "*expanded pure_lines: [(1, {'total': 0}), (2, {'x': 3}), (3, {'total': 3}), (4, {'last': 3}), (2, {'x': 4}), (3, {'total': 7}), (4, {'last': 4}), (2, {'x': 5}), (3, {'total': 12}), (4, {'last': 5})]*"},
        {"status": "limitation", "min_python": [3, 12], "desc": "...so a change made by user code an attribute read runs implicitly (a property, __getattr__, an operator dunder) on such a line is recorded at the next line that has a call/store (here line 4, not 2)",
         "stdout": "*expanded property_side_effect: [(1, {'obj': {'__type__': 'Lazy', 'hits': 0}}), (2, {'v': 42}), (3, {'w': 43}), (4, {'obj': {'__type__': 'Lazy', 'hits': 1}})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a big value inside an edit goes to the variables pool like any capture",
         "stdout": "*pooled: appended item is a ref: {'payload': {'__ref__': 'v*'}}*"},
        {"status": "works", "min_python": [3, 12], "desc": "MAX_SIZE_CAPTURE: no edits against a value that was too big to record, nor edits that are themselves too big (full value / marker instead)",
         "stdout": "*oversized: [(1, 'set', {'big': '<list>'}), (2, 'set', {'big': '<list>'}), (3, 'set', {'small': [1]}), (4, 'set', {'small': {'__ref__': 'v*'}})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a 200-row append loop: rows is a full value twice ([] and the first row), then 199 edits",
         "stdout": "*grow: entries 401 set 2 changed 199*"},
        {"status": "works", "min_python": [3, 12], "desc": "...and its trace is a small fraction of the full-values one (each step stored one row, not the whole list)",
         "stdout": "*size: grow(200) with deltas under 10% of full values: True*"},
        {"status": "works", "min_python": [3, 12], "desc": "perf.expand_locals replays the edits: every rebuilt value equals the real state at that line (append, dict, object, pop/element, fallbacks, argument bases, locks, redaction, rebinding, pooled refs, 200 steps)",
         "stdout": "*expanded appends: {'items': [{'n': 0, 'tag': 'item-0'}, {'n': 1, 'tag': 'item-1'}, {'n': 2, 'tag': 'item-2'}]}*"
                   "expanded dict_ops: [{'apple': 1, 'banana': 2, 'cherry': 3}, {'apple': 1, 'banana': 2, 'cherry': 3, 'damson': 4}, {'apple': 11, 'banana': 2, 'cherry': 3, 'damson': 4}, {'apple': 11, 'cherry': 3, 'damson': 4}]*"
                   "expanded nested_object: {'__type__': 'Report', 'rows': [{'sku': 'A', 'qty': 1}, {'sku': 'B', 'qty': 1}], 'totals': {'A': 1, 'B': 1}}*"
                   "expanded truncate_and_element: [{'stack': [10, 20, 30, 40, 50, 60, 70, 80]}, {'stack': [10, 20, 30, 40, 50, 60, 70]}, {'stack': [10, 20, 30, 40, 50, 60]}, {'grid': [[0, 0, 0], [0, 0, 0], [0, 0, 0]]}, {'grid': [[0, 0, 0], [0, 0, 9], [0, 0, 0]]}]*"
                   "expanded fallbacks: [{'arg_list': [1, 2, 3, 4, 5, 6, 7, 8, 99]}, {'arg_list': [1, 2, 3, 4, 5, 6, 7, 8, 99, 100]}, *{'flags': [True, 2, 3, 4, 5, 6, 7, 8, 9]}]*"
                   "expanded skipped_and_redacted: [*{'log': ['start', 'connecting to the database', 'connected', 'login password=<redacted>']}]*"
                   "expanded deleted_and_rebound: [{'buf': [1, 2, 3, 4, 5, 6, 7, 8]}, None, {'buf': [7, 7, 7, 7, 7, 7, 7, 7]}, {'buf': [7, 7, 7, 7, 7, 7, 7, 7, 8]}]*"
                   "expanded deposit: [['opened', 'verified', 'funded'], ['opened', 'verified', 'funded', '+5'], ['opened', 'verified', 'funded', '+5'], ['opened', 'verified', 'funded', '+5', '+7']]*"
                   "expanded kwonly: [{'bag': ['a', 'b', 'c', 'd', 'e']}]*"
                   "expanded locking: [{'counter': {'n': 1, 'label': 'counter'}}, {'counter': {'n': 2, 'label': 'counter'}}]*"
                   "expanded pooled_append: True*"
                   "expanded grow: last rows correct: True every step one row longer: True*"},
    ],
}


class Report:
    def __init__(self):
        self.rows = []
        self.totals = {}


def appends():
    items = []
    for i in range(3):
        items.append({"n": i, "tag": f"item-{i}"})
    return items


def dict_ops():
    counts = {"apple": 1, "banana": 2, "cherry": 3}
    counts["damson"] = 4
    counts["apple"] += 10
    del counts["banana"]
    return counts


def nested_object():
    report = Report()
    for sku in ("A", "B"):
        report.rows.append({"sku": sku, "qty": 1})
        report.totals[sku] = report.totals.get(sku, 0) + 1
    return report


def truncate_and_element():
    stack = [10, 20, 30, 40, 50, 60, 70, 80]
    stack.pop()
    stack.pop()
    grid = [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    grid[1][2] = 9
    return stack, grid


def fallbacks(arg_list):
    arg_list.append(99)                  # an argument's first change: edits against its value in "args"
    arg_list.append(100)                 # ...then against that entry
    front = [1, 2, 3, 4, 5, 6, 7, 8]
    front.insert(0, 0)                   # not an append: full value
    front = [9, 9]                       # a new object: full value
    tiny = []
    tiny.append(1)                       # ops not smaller than [1]: full value
    flags = [1, 2, 3, 4, 5, 6, 7, 8]
    flags[0] = True                      # == can't see 1 → True (not a change)...
    flags.append(9)                      # ...but the delta must not hide it: full value
    return front, tiny, flags


def skipped_and_redacted():
    secret_tokens = ["a", "b"]
    secret_tokens.append("c")            # SKIP_VARIABLES name: never a value, never ops
    log = ["start", "connecting to the database", "connected"]
    log.append("login password=hunter2")
    return log


def deleted_and_rebound():
    buf = [1, 2, 3, 4, 5, 6, 7, 8]
    del buf
    buf = [7, 7, 7, 7, 7, 7, 7, 7]
    buf.append(8)                        # base is the NEW buf
    return buf


def pooled_append():
    rows = [{"payload": "x" * 80}]
    rows.append({"payload": "y" * 80})   # a big appended item goes to the variables pool
    return rows


def grow(n):
    rows = []
    for i in range(n):
        rows.append({"id": i, "sku": f"SKU-{i % 7}", "qty": i % 5})
    return len(rows)


def oversized():
    big = list(range(40))
    big.append(40)                       # base was too big to record: full value (a marker)
    small = [1]
    small.extend(range(2, 30))           # ...and a delta that is itself too big: full value
    return big, small


class Account:
    def __init__(self, owner):
        self.owner = owner
        self.balance = 0
        self.history = ["opened", "verified", "funded"]
        self._lock = threading.Lock()

    def deposit(self, amount):
        with self._lock:                 # self._lock flips to <locked …>: not a change of self
            self.balance += amount
            self.history.append(f"+{amount}")
        return self.balance


def locking(lock, counter):
    with lock:                           # the lock itself: no entry when it is taken/released
        counter["n"] += 1
    with lock:
        counter["n"] += 1


def kwonly(*, bag):
    bag.append("e")                      # keyword-only argument: base is kwargs["bag"]


def secret_arg(secret_list):
    secret_list.append(9)                # SKIP_VARIABLES argument: still a marker, never edits


class Lazy:
    def __init__(self):
        self.hits = 0

    @property
    def value(self):
        self.hits += 1                   # a property that changes its object
        return 42


def noop(x):
    return x


def property_side_effect():
    obj = Lazy()
    v = obj.value                        # no call/store on this line: contents not compared after it...
    w = v + 1
    noop(w)                              # ...so obj.hits = 1 is recorded here, the next line with a call
    return obj


def pure_lines(items):
    total = 0
    for x in items:                      # lines without calls still record every rebinding
        total = total + x
        last = x
    return total, last


FUNCS = [appends, dict_ops, nested_object, truncate_and_element, fallbacks, skipped_and_redacted,
         deleted_and_rebound, pooled_append, locking, kwonly, secret_arg, property_side_effect, pure_lines,
         grow, oversized]


@perf.trace
def main():
    appends()
    dict_ops()
    nested_object()
    truncate_and_element()
    fallbacks([1, 2, 3, 4, 5, 6, 7, 8])
    skipped_and_redacted()
    deleted_and_rebound()
    pooled_append()
    account = Account("ann")
    account.deposit(5)
    account.deposit(7)
    locking(threading.Lock(), {"n": 0, "label": "counter"})
    kwonly(bag=["a", "b", "c", "d"])
    secret_arg([1, 2, 3, 4, 5, 6, 7, 8])
    property_side_effect()
    pure_lines([3, 4, 5])
    grow(200)


@perf.trace(OUTPUT_FILE="size_limit", MAX_SIZE_CAPTURE=60)
def size_limit():
    oversized()


@perf.trace(OUTPUT_FILE="full_values")
def full_values():
    grow(200)


def raw(node, fn):
    """A node's raw TRACE_LOCALS entries, lines relative to the function's def."""
    base = inspect.unwrap(fn).__code__.co_firstlineno
    out = []
    for e in node.get("locals", []):
        item = [e[0] - base]
        for key, part in zip(("set", "changed", "deleted"), e[1:4]):
            if part:
                item += [key, part]
        out.append(tuple(item))
    return out


def walk_all(trace):
    stack = [trace["trace"]]
    while stack:
        n = stack.pop(0)
        yield n
        stack[:0] = n.get("children", [])


def by_name(trace):
    out = {}

    def walk(n):
        out.setdefault(n["name"], n)
        for c in n.get("children", []):
            walk(c)

    walk(trace["trace"])
    return out


if __name__ == "__main__":
    setup(__file__, TRACE_LOCALS=[f"__main__.{f.__name__}" for f in FUNCS] + ["Account.deposit"],
          SKIP_VARIABLES=["secret_*"], REDACT_PATTERNS=[r"(?<=password=)\w+"])
    main()
    size_limit()
    if sys.version_info < (3, 12):  # TRACE_LOCALS needs sys.monitoring: no locals to report
        sys.exit(0)
    path = f"{perf.OUTPUT_DIR}/main.json"
    trace = json.load(open(path))
    nodes = by_name(trace)
    for fn in FUNCS[:-2]:
        print(f"{fn.__name__}: {raw(nodes[f'__main__.{fn.__name__}'], fn)}")
    deposits = [n for n in walk_all(trace) if n["name"] == "Account.deposit"]
    for n in deposits:
        print(f"deposit: params {n.get('params')} {raw(n, Account.deposit)}")
    for name in ("locking", "kwonly"):
        n = nodes[f"__main__.{name}"]
        print(f"{name}: params {n.get('params')} {raw(n, globals()[name])}")
    print("raw format:", json.dumps(nodes["__main__.dict_ops"]["locals"][:2]))
    print("pooled: appended item is a ref:", raw(nodes["__main__.pooled_append"], pooled_append)[-1][2]["rows"][0][2][0])

    g = nodes["__main__.grow"]
    print("grow: entries", len(g["locals"]), "set", sum("rows" in (e[1] or {}) for e in g["locals"]),
          "changed", sum(len(e) > 2 and "rows" in (e[2] or {}) for e in g["locals"]))

    # Every value rebuilt by expand_locals equals the function's real state at that line.
    expanded_trace = perf.expand_locals(json.load(open(path)))
    expanded = by_name(expanded_trace)
    print("expanded appends:", [e.get("set") for e in expanded["__main__.appends"]["locals"]][-1])
    print("expanded dict_ops:", [e["set"]["counts"] for e in expanded["__main__.dict_ops"]["locals"]])
    print("expanded nested_object:", [e["set"]["report"] for e in expanded["__main__.nested_object"]["locals"] if "report" in e["set"]][-1])
    print("expanded truncate_and_element:",
          [e["set"] for e in expanded["__main__.truncate_and_element"]["locals"]])
    print("expanded fallbacks:", [e["set"] for e in expanded["__main__.fallbacks"]["locals"]])
    print("expanded skipped_and_redacted:", [e["set"] for e in expanded["__main__.skipped_and_redacted"]["locals"]])
    print("expanded deleted_and_rebound:", [e.get("set") for e in expanded["__main__.deleted_and_rebound"]["locals"]])
    print("expanded deposit:", [e["set"]["self"]["history"] for n in walk_all(expanded_trace)
                                if n["name"] == "Account.deposit" for e in n["locals"] if "self" in e.get("set", {})])
    print("expanded kwonly:", [e["set"] for e in expanded["__main__.kwonly"]["locals"]])
    print("expanded locking:", [e["set"] for e in expanded["__main__.locking"]["locals"]])
    print("expanded property_side_effect:", [(e["line"] - inspect.unwrap(property_side_effect).__code__.co_firstlineno, e.get("set")) for e in expanded["__main__.property_side_effect"]["locals"]])
    print("expanded pure_lines:", [(e["line"] - inspect.unwrap(pure_lines).__code__.co_firstlineno, e.get("set")) for e in expanded["__main__.pure_lines"]["locals"]])
    print("expanded pooled_append:", expanded["__main__.pooled_append"]["locals"][-1]["set"]["rows"]
          == [{"payload": "x" * 80}, {"payload": "y" * 80}])
    rows = [e["set"]["rows"] for e in expanded["__main__.grow"]["locals"] if "rows" in e.get("set", {})]
    print("expanded grow: last rows correct:",
          rows[-1] == [{"id": i, "sku": f"SKU-{i % 7}", "qty": i % 5} for i in range(200)],
          "every step one row longer:", [len(r) for r in rows] == list(range(201)))

    sized = by_name(json.load(open(f"{perf.OUTPUT_DIR}/size_limit.json")))
    print(f"oversized: {raw(sized['__main__.oversized'], oversized)}")

    # Size: the same grow(200) as full values only (deltas switched off) vs. as written.
    perf_delta = perf._locals_delta
    perf._locals_delta = lambda old, new: None
    try:
        full_values()
    finally:
        perf._locals_delta = perf_delta
    full = json.load(open(f"{perf.OUTPUT_DIR}/full_values.json"))
    size = lambda t: len(json.dumps(t, separators=(",", ":")))
    print(f"size: grow(200) with deltas under 10% of full values: {size(trace) * 10 < size(full)} "
          f"({size(trace)} vs {size(full)} bytes)")
