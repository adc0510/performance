"""TRACE_LOCALS across statement forms and kinds of mutation (Python 3.12+)."""

import perf
from _common import OUT, locals_entries, setup, trace_nodes

G = 0

EXPECT = {
    "new": True,
    "summary": "TRACE_LOCALS statement forms: walrus, comprehensions, except-as, with-as, multi-line, semicolons, star-unpack, item/attr assignment, aliasing, mutation by a callee, nonlocal, global, __slots__, oversized values, loops with break/else, augmented assignment, rebinding vs equal values",
    "checks": [
        {"status": "works", "min_python": [3, 12], "desc": "walrus inside a condition: recorded at that line", "stdout": "*walrus: [(1, 'if (n := len(items)) > 2:', {'n': 3})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "comprehension / generator-expression variables don't leak into the function's locals", "stdout": "*comprehension: [(1, 'squares = [y * y for y in range(3)]', {'squares': [0, 1, 4]}), (2, 'total = sum(x for x in squares)', {'total': 5})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "except ... as e: e is recorded; Python's implicit `del e` at the end of the block is attributed to the block's last line", "stdout": "*except_as: [(3, 'except ValueError as e:', {'e': \"ValueError('x')\"}), (4, 'msg = str(e)', {'msg': 'x'}, 'deleted', ['e'])]*"},
        {"status": "works", "min_python": [3, 12], "desc": "with ... as f: recorded at the with line", "stdout": "*with_as: [(1, 'with io.StringIO(\"hi\") as f:', {'f': '<StringIO>'}), (2, 'data = f.read()', {'data': 'hi'})]*"},
        {"status": "works", "fixes": "a multi-line statement's entry showed only its first physical line", "min_python": [3, 12], "desc": "a statement spanning several lines is recorded at its first line, with its whole text", "stdout": "*multiline: [(1, 'value = (1 + 2 + 3)', {'value': 6}), (4, 'other = [ value, value * 2, ]', {'other': [6, 12]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "several statements on one line (;) share one entry", "stdout": "*semicolons: [(1, 'a = 1; b = 2', {'a': 1, 'b': 2})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "star-unpacking", "stdout": "*star: [(1, 'first, *rest = [1, 2, 3]', {'first': 1, 'rest': [2, 3]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "item and attribute assignment show the container / object with its new content", "stdout": "*item_attr: [(1, 'd = {}', {'d': {}}), (2, 'd[\"k\"] = 1', {'d': {'k': 1}}), (3, 'o = Obj()', {'o': '<Obj>'}), (4, 'o.x = 5', {'o': {'__type__': 'Obj', 'x': 5}})]*"},
        {"status": "works", "fixes": "an instance of a class defined inside a function was captured as its raw repr, memory address included (<f.<locals>.C object at 0x…>)", "min_python": [3, 12], "desc": "an instance of a local class is shown like any other object, never as a memory address", "stdout": "*local class: [(1, 'class Local:', {'Local': {'__class__': '__main__.local_class.<locals>.Local'}}), (4, 'thing = Local()', {'thing': '<Local>'})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "aliasing: mutating a list through one name records every name bound to it", "stdout": "*aliasing: [(1, 'a = []', {'a': []}), (2, 'b = a', {'b': []}), (3, 'a.append(1)', {'a': [1], 'b': [1]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "a value mutated by a CALLED function is recorded at the call line", "stdout": "*by_call: [(1, 'items = [1]', {'items': [1]}), (2, 'mutate(items)', {'items': [1, 9]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "nonlocal: the outer function sees the change at the line of the call that made it", "stdout": "*nonlocal outer: [(1, 'count = 0', {'count': 0}), (2, 'def bump():', {'bump': *}), (6, 'bump()', {'count': 1}), (7, 'bump()', {'count': 2})]*"},
        {"status": "works", "fixes": "a module global the function assigns (global G; G = 5) was not recorded", "min_python": [3, 12], "desc": "global: assignments to names declared `global` are recorded under `globals`", "stdout": "*global: [(2, 'G = 5', None, 'globals', {'G': 5}), (3, 'local = G + 1', {'local': 6})]*"},
        {"status": "works", "fixes": "mutating an object with __slots__ was invisible (no fields captured)", "min_python": [3, 12], "desc": "__slots__ objects: every change of a slot is recorded", "stdout": "*slotted: [(1, 's = Slotted()', {'s': {'__type__': 'Slotted', 'v': 0}}), (2, 's.v = 1', {'s': {'__type__': 'Slotted', 'v': 1}}), (3, 's.v = 2', {'s': {'__type__': 'Slotted', 'v': 2}})]*"},
        {"status": "works", "fixes": "with MAX_SIZE_CAPTURE set, a change past the size cut-off of a value was silently missed", "min_python": [3, 12], "desc": "a value larger than MAX_SIZE_CAPTURE: its change is still detected (it is stored as the size-limit marker)", "stdout": "*oversized: [(1, 'big = list(range(50))', {'big': '<list>'}), (2, 'big.append(999)', {'big': '<list>'})]*"},
        {"status": "limitation", "min_python": [3, 12], "desc": "state hidden inside objects that capture shows only by repr (e.g. an iterator advanced by next()) is not seen as a change", "stdout": "*iterator: [(1, 'it = iter([1, 2, 3])', {'it': '<list_iterator>'}), (2, 'first = next(it)', {'first': 1}), (3, 'second = next(it)', {'second': 2})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "loops with break and for/else: one entry per change", "stdout": "*loop_break: [(1, 'found = None', {'found': None}), (2, 'for i in range(10):', {'i': 0}), (2, 'for i in range(10):', {'i': 1}), (2, 'for i in range(10):', {'i': 2}), (4, 'found = i', {'found': 2})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "augmented assignment: `lst += [...]` mutates in place, `tup += (...)` rebinds — both recorded", "stdout": "*augmented: [(1, 'lst = [1]', {'lst': [1]}), (2, 'lst += [2]', {'lst': [1, 2]}), (3, 'tup = (1,)', {'tup': [1]}), (4, 'tup += (2,)', {'tup': [1, 2]})]*"},
        {"status": "works", "min_python": [3, 12], "desc": "rebinding to an EQUAL but new mutable object is recorded (it is a different object); re-assigning the same immutable value is not", "stdout": "*rebinding: [(1, 'a = [1]', {'a': [1]}), (2, 'a = [1]', {'a': [1]}), (3, 'n = 5', {'n': 5})]*"},
    ],
}


class Obj:
    pass


class Slotted:
    __slots__ = ("v",)

    def __init__(self):
        self.v = 0


def walrus(items):
    if (n := len(items)) > 2:
        return n
    return 0


def comprehension():
    squares = [y * y for y in range(3)]
    total = sum(x for x in squares)
    return total


def except_as():
    try:
        raise ValueError("x")
    except ValueError as e:
        msg = str(e)
    return msg


def with_as():
    with io.StringIO("hi") as f:
        data = f.read()
    return data


def multiline():
    value = (1 +
             2 +
             3)
    other = [
        value,
        value * 2,
    ]
    return other


def semicolons():
    a = 1; b = 2
    return a + b


def star():
    first, *rest = [1, 2, 3]
    return rest


def item_attr():
    d = {}
    d["k"] = 1
    o = Obj()
    o.x = 5
    return d, o


def local_class():
    class Local:
        pass

    thing = Local()
    return thing


def aliasing():
    a = []
    b = a
    a.append(1)
    return b


def mutate(lst):
    lst.append(9)


def by_call():
    items = [1]
    mutate(items)
    return items


def nonlocal_outer():
    count = 0
    def bump():
        nonlocal count
        count += 1
        return count
    bump()
    bump()
    return count


def uses_global():
    global G
    G = 5
    local = G + 1
    return local


def slotted():
    s = Slotted()
    s.v = 1
    s.v = 2
    return s.v


def oversized():
    big = list(range(50))
    big.append(999)
    return len(big)


def iterator():
    it = iter([1, 2, 3])
    first = next(it)
    second = next(it)
    return first + second


def loop_break():
    found = None
    for i in range(10):
        if i == 2:
            found = i
            break
    else:
        found = -1
    return found


def augmented():
    lst = [1]
    lst += [2]
    tup = (1,)
    tup += (2,)
    return lst, tup


def rebinding():
    a = [1]
    a = [1]
    n = 5
    n = 5  # same immutable value: no change
    return a, n


import io  # noqa: E402

SHAPES = [walrus, comprehension, except_as, with_as, multiline, semicolons, star, item_attr, local_class,
          aliasing, by_call, nonlocal_outer, uses_global, slotted, iterator, loop_break, augmented, rebinding]


@perf.trace(TRACE_LOCALS=["__main__.*"])
def main():
    walrus([1, 2, 3])
    for fn in SHAPES[1:]:
        globals()[fn.__name__]()


@perf.trace(TRACE_LOCALS=["__main__.oversized"], MAX_SIZE_CAPTURE=60, OUTPUT_FILE="oversized")
def big():
    oversized()


if __name__ == "__main__":
    setup(__file__)
    main()
    big()
    nodes = trace_nodes(f"{perf.OUTPUT_DIR}/main.json") + trace_nodes(f"{perf.OUTPUT_DIR}/oversized.json")

    def show(label, name, fn):
        node = next((n for n in nodes if n["name"] == name), {})
        print(f"{label}: {locals_entries(node, fn)}")

    for label, fn in [("walrus", walrus), ("comprehension", comprehension), ("except_as", except_as),
                      ("with_as", with_as), ("multiline", multiline), ("semicolons", semicolons), ("star", star),
                      ("item_attr", item_attr), ("local class", local_class), ("aliasing", aliasing),
                      ("by_call", by_call), ("nonlocal outer", nonlocal_outer), ("global", uses_global),
                      ("slotted", slotted), ("oversized", oversized), ("iterator", iterator),
                      ("loop_break", loop_break), ("augmented", augmented), ("rebinding", rebinding)]:
        show(label, f"__main__.{fn.__name__}", fn)
