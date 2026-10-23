"""Exactly when captured content moves into the shared `variables` pool."""

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Content-dedup threshold: containers ≥64 compact-JSON bytes and strings ≥64 chars become {__ref__}; smaller stays inline; changes never resolve to stale content",
    "checks": [
        {"status": "works", "desc": "container of 63 serialized bytes stays inline",
         "node": "__main__.size_63", "raw": True, "result": ["*"]},
        {"status": "works", "desc": "container of exactly 64 serialized bytes becomes a ref",
         "node": "__main__.size_64", "raw": True, "result": {"__ref__": "v*"}},
        {"status": "works", "desc": "container of 65 serialized bytes becomes a ref",
         "node": "__main__.size_65", "raw": True, "result": {"__ref__": "v*"}},
        {"status": "works", "desc": "string of 63 chars stays inline",
         "node": "__main__.echo_str", "raw": True, "args": ["sss*"]},
        {"status": "works", "desc": "string of 64 chars becomes a ref (strings count characters, not quotes)",
         "node": "__main__.echo_str_64", "raw": True, "args": [{"__ref__": "v*"}]},
        {"status": "works", "desc": "refs resolve back to exactly the original value",
         "node": "__main__.size_65", "result": ["*"]},
        {"status": "works", "desc": "the same large content captured repeatedly resolves to the SAME ref (stored once)",
         "stdout": "*same content same ref: True*"},
        {"status": "works", "desc": "changed content gets a NEW ref holding the new value (never stale)",
         "stdout": "*changed content new ref: True new value correct: True*"},
        {"status": "works", "desc": "a nested part can cross the threshold while its small parent stays inline",
         "node": "__main__.nested_crosses", "raw": True, "result": {"big": {"__ref__": "v*"}, "n": 1}},
    ],
}


def size_63():
    return ["a" * 59]          # ["aaa…"] → 59 + 4 = 63 bytes


def size_64():
    return ["a" * 60]          # 64 bytes


def size_65():
    return ["a" * 61]          # 65 bytes


def echo_str(s):
    return len(s)


def echo_str_64(s):
    return len(s)


def echo(doc):
    return doc


def nested_crosses():
    return {"big": list(range(40)), "n": 1}


@perf.trace
def main():
    size_63(), size_64(), size_65(), nested_crosses()
    echo_str("s" * 63), echo_str_64("s" * 64)
    doc = {"payload": list(range(30))}
    echo(doc)
    echo(doc)
    doc["payload"].append(99)
    echo(doc)


if __name__ == "__main__":
    import json

    setup(__file__)
    main()
    d = json.load(open(f"{perf.OUTPUT_DIR}/main.json"))
    echoes = [c for c in d["trace"]["children"] if c["name"] == "__main__.echo"]
    refs = [e["args"][0]["payload"]["__ref__"] for e in echoes]
    print("same content same ref:", refs[0] == refs[1])
    print("changed content new ref:", refs[2] != refs[0],
          "new value correct:", d["variables"][refs[2]] == list(range(30)) + [99])
