"""Capturing the same large containers and strings over and over (an unchanged `self.config` on every call)."""

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Repeated captures of large plain-valued containers/strings reuse earlier work, yet every change in between is recorded: in-place edits, equal-but-different values (1/True/1.0, 0.0/-0.0), a container freed and another built at its address, redaction",
    "checks": [
        {"status": "works", "desc": "an unchanged large dict captured by many calls is stored once (one ref for all)",
         "stdout": "*unchanged dict one ref: True*"},
        {"status": "works", "desc": "a value replaced in place between calls is recorded with its new value",
         "stdout": "*in-place edit recorded: True*"},
        {"status": "works", "desc": "1 -> True -> 1.0 and 0.0 -> -0.0 (equal in Python) are each recorded as what they are",
         "stdout": "*equal-but-different values recorded: True*"},
        {"status": "works", "desc": "a key added then removed again (same objects as before) resolves to the original content",
         "stdout": "*add then remove key: True*"},
        {"status": "works", "desc": "a large list appended to / an element replaced in place is recorded with its new content",
         "stdout": "*list edits recorded: True*"},
        {"status": "works", "desc": "a container freed and a new one with other content built (possibly at the same address) is never mistaken for the old one",
         "stdout": "*rebuilt containers recorded: True*"},
        {"status": "works", "desc": "the same long string reached through many calls is stored once",
         "stdout": "*long string one ref: True*"},
        {"status": "works", "desc": "redaction still applies to a container whose conversion is reused",
         "stdout": "*redaction kept: True*"},
        {"status": "works", "desc": "a tuple of plain values is recorded like the list it converts to",
         "node": "__main__.echo", "select": {"args": [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]]},
         "result": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19]},
    ],
}

BIG = {f"key_{i}": f"value number {i} " + "x" * 80 for i in range(40)}


def echo(value):
    return value


class Holder:
    def __init__(self):
        self.config = dict(BIG)

    def read(self):
        return len(self.config)


@perf.trace
def main():
    h = Holder()
    for _ in range(5):
        h.read()                                   # unchanged self.config
    flags = {f"f{i}": i for i in range(20)}
    echo(flags)
    flags["f1"] = True                             # 1 -> True
    echo(flags)
    flags["f1"] = 1.0                              # True -> 1.0
    echo(flags)
    flags["f0"] = 0.0
    echo(flags)
    flags["f0"] = -0.0                             # 0.0 -> -0.0
    echo(flags)
    cfg = dict(BIG)
    echo(cfg)
    cfg["key_3"] = "edited"                        # in-place edit
    echo(cfg)
    cfg["extra"] = "x"
    del cfg["extra"]                               # back to the same objects as before
    echo(cfg)
    items = list(range(30))
    echo(items)
    items.append(99)
    echo(items)
    items[0] = "zero"
    echo(items)
    for n in range(3):                             # freed and rebuilt each round
        echo({f"k{i}": i * 10 + n for i in range(20)})
    long_text = "lorem ipsum " * 20
    for _ in range(4):
        echo(long_text)
    echo(tuple(range(20)))


@perf.trace
def redacted():
    secrets = {f"token_{i}": i for i in range(20)}
    echo(secrets)
    echo(secrets)


def resolve(value, variables):
    if isinstance(value, dict):
        if "__ref__" in value:
            return resolve(variables[value["__ref__"]], variables)
        return {k: resolve(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, variables) for v in value]
    return value


if __name__ == "__main__":
    import json
    import math

    setup(__file__)
    main()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    variables, nodes = d["variables"], d["trace"]["children"]
    reads = [n for n in nodes if n["name"] == "Holder.read"]
    holder_refs = {json.dumps(r["args"][0]) for r in reads}  # raw: the same ref text every time
    print("unchanged dict one ref:", len(reads) == 5 and len(holder_refs) == 1 and "__ref__" in json.dumps(reads[0]["args"][0])
          and resolve(reads[-1]["args"][0], variables)["config"] == BIG)
    echoes = [resolve(n["args"][0], variables) for n in nodes if n["name"] == "__main__.echo"]
    raw_flags = [n["args"][0] for n in nodes if n["name"] == "__main__.echo"][:5]
    flags = [resolve(f, variables) for f in raw_flags]
    f1 = [f["f1"] for f in flags]
    f0 = flags[4]["f0"]
    print("equal-but-different values recorded:",
          [type(v).__name__ for v in f1] == ["int", "bool", "float", "float", "float"]
          and flags[3]["f0"] == 0.0 and math.copysign(1, f0) == -1.0)
    cfg_before, cfg_after, cfg_back = echoes[5], echoes[6], echoes[7]
    print("in-place edit recorded:", cfg_before["key_3"] == BIG["key_3"] and cfg_after["key_3"] == "edited")
    print("add then remove key:", cfg_back == cfg_after and "extra" not in cfg_back)
    l0, l1, l2 = echoes[8], echoes[9], echoes[10]
    print("list edits recorded:", l0 == list(range(30)) and l1 == list(range(30)) + [99]
          and l2 == ["zero"] + list(range(1, 30)) + [99])
    rebuilt = echoes[11:14]
    print("rebuilt containers recorded:", all(r == {f"k{i}": i * 10 + n for i in range(20)} for n, r in enumerate(rebuilt)))
    texts = [n["args"][0] for n in nodes if n["name"] == "__main__.echo"][14:18]
    print("long string one ref:", len({json.dumps(t) for t in texts}) == 1 and "__ref__" in texts[0])

    perf.REDACT_PATTERNS = [r"token_1\d"]
    perf.OUTPUT_FILE = "redacted"
    redacted()
    r = json.load(open(f"{perf.OUTPUT_DIR}/redacted.json"))
    seen = [resolve(n["args"][0], r["variables"]) for n in r["trace"]["children"]]
    print("redaction kept:", len(seen) == 2 and all("token_12" not in s and "<redacted>" in s and "token_3" in s for s in seen))
