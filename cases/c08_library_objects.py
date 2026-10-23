"""Library objects, modules and heavily shared object graphs as captured values."""

import json
import logging
import pstats
import threading
import textwrap
import time

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Values that are not the program's data: modules, library objects (engines, clients, loggers…), library dataclasses, heavily shared object graphs",
    "checks": [
        {"status": "works", "fixes": "a module reachable from a captured value was expanded namespace and all (every class, function and regex), recursively through the modules it imports — a captured SQLAlchemy engine (dialect.dbapi is a module) made each call take minutes",
         "desc": "a module is captured as {__module__: name}, never expanded", "node": "__main__.get_module", "result": {"__module__": "json"}},
        {"status": "works", "fixes": "objects of library classes (engines, clients, pools…) were expanded field by field into their internals, reaching connection pools, registries and modules",
         "desc": "a library object with its own repr is captured as that repr", "node": "__main__.get_logger", "result": "<Logger perf.case (WARNING)>"},
        {"status": "works", "desc": "a library object without a repr of its own: a <Type> marker, not its internals", "node": "__main__.get_condition", "result": "<Condition(<unlocked _thread.RLock object owner=0 count=0 at *>, 0)>"},
        {"status": "works", "desc": "your own objects are still expanded, with library objects inside them shown compactly",
         "node": "__main__.get_holder", "result": {"__type__": "Holder", "name": "db", "module": {"__module__": "json"}, "log": "<Logger perf.case (WARNING)>"}},
        {"status": "works", "desc": "library dataclasses are data: still expanded field by field",
         "node": "__main__.get_dataclass", "result": {"__type__": "FunctionProfile", "ncalls": "1", "tottime": 0.5, "percall_tottime": 0.5, "cumtime": 0.5, "percall_cumtime": 0.5, "file_name": "f.py", "line_number": 1}},
        {"status": "works", "fixes": "an object graph where the same sub-objects are reachable along many paths was re-expanded along every path (exponential in MAX_DEPTH)",
         "desc": "a graph of 20 levels where every node shares its children (2^20 paths, 41 objects) converts in well under a second",
         "stdout": "*shared graph converted fast: True*"},
        {"status": "works", "desc": "...and its content is still complete (each shared object appears in full wherever it is referenced)",
         "stdout": "*shared graph leaves reachable: 2*"},
        {"status": "works", "desc": "a library object without its own repr: a <Type> marker (here textwrap.TextWrapper)",
         "node": "__main__.get_wrapper", "result": "<TextWrapper>"},
        {"status": "works", "desc": "SCAN_VENV (+ prefixes) opts that library's objects back into field-by-field expansion",
         "file": "scanned", "node": "__main__.get_wrapper", "result": {"__type__": "TextWrapper", "width": 7, "...": True}},
    ],
}

LOG = logging.getLogger("perf.case")


class Holder:
    def __init__(self):
        self.name = "db"
        self.module = json
        self.log = LOG


class Node:
    def __init__(self, left, right):
        self.left = left
        self.right = right


def shared_graph(levels):
    node = Node("leaf-a", "leaf-b")
    for _ in range(levels):
        node = Node(node, node)  # both children are the SAME object: 2^levels paths
    return node


def get_module():
    return json


def get_logger():
    return LOG


def get_condition():
    return threading.Condition()


def get_wrapper():
    return textwrap.TextWrapper(width=7)


def get_holder():
    return Holder()


def get_dataclass():
    return pstats.FunctionProfile(ncalls="1", tottime=0.5, percall_tottime=0.5, cumtime=0.5, percall_cumtime=0.5,
                                  file_name="f.py", line_number=1)


def get_graph():
    return shared_graph(19)


@perf.trace
def main():
    get_module()
    get_logger()
    get_condition()
    get_wrapper()
    get_holder()
    get_dataclass()
    t = time.perf_counter()
    get_graph()
    print("shared graph converted fast:", time.perf_counter() - t < 1.0)


@perf.trace(SCAN_VENV=True, _SCAN_INCLUDE_PREFIXES=("textwrap",), OUTPUT_FILE="scanned")
def scanned():
    get_wrapper()


if __name__ == "__main__":
    setup(__file__)
    main()
    scanned()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    text = json.dumps(d)
    print("shared graph leaves reachable:", len({leaf for leaf in ("leaf-a", "leaf-b") if leaf in text}))
