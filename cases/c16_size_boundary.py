"""Exactly where MAX_SIZE_CAPTURE (=20 here) drops or spills a value."""

import json
import os

import perf
from _common import OUT, setup

SPILL = OUT / "c16_size_boundary" / "spill"

EXPECT = {
    "new": True,
    "summary": "MAX_SIZE_CAPTURE boundary (=20): size = characters of content (string chars, number digits, reprs) — not JSON syntax; < limit inline, ≥ limit dropped or spilled",
    "checks": [
        {"status": "works", "desc": "string of 19 chars (below the limit): inline",
         "file": "drop", "node": "__main__.str_19", "result": "xxxxxxxxxxxxxxxxxxx"},
        {"status": "works", "desc": "string of exactly 20 chars (at the limit): dropped to a `<str>` marker (only values BELOW the limit are kept)",
         "file": "drop", "node": "__main__.str_20", "result": "<str>"},
        {"status": "works", "desc": "string of 21 chars (above): dropped", "file": "drop", "node": "__main__.str_21", "result": "<str>"},
        {"status": "works", "desc": "list of numbers with 19 digits total: inline (brackets/commas don't count)",
         "file": "drop", "node": "__main__.nums_19", "result": [1234567890, 123456789]},
        {"status": "works", "desc": "list of numbers with 20 digits total: dropped",
         "file": "drop", "node": "__main__.nums_20", "result": "<list>"},
        {"status": "works", "desc": "nested dict: keys don't count, values do (19 chars inline)",
         "file": "drop", "node": "__main__.dict_19", "result": {"key_a": {"key_b": "yyyyyyyyyyyyyyyyyyy"}}},
        {"status": "works", "desc": "nested dict at 20 value chars: dropped",
         "file": "drop", "node": "__main__.dict_20", "result": "<dict>"},
        {"status": "works", "fixes": "documentation only: MAX_SIZE_CAPTURE was documented as UTF-8 bytes; it counts characters", "desc": "non-ASCII strings are measured in characters, not UTF-8 bytes (19 × 'é' inline)",
         "file": "drop", "node": "__main__.unicode_19", "result": "ééééééééééééééééééé"},
        {"status": "works", "desc": "with SPILL_DIR: the value at the limit is spilled; node holds {__spilled__, size}",
         "file": "spill", "node": "__main__.str_20", "raw": True, "result": {"__spilled__": "*.json", "...": True}},
        {"status": "works", "desc": "with SPILL_DIR: small values stay inline",
         "file": "spill", "node": "__main__.str_19", "result": "xxxxxxxxxxxxxxxxxxx"},
        {"status": "works", "desc": "every spill reference points at an existing file holding the FULL value inline, matching `size`",
         "stdout": "*spill files ok: True*"},
    ],
}


def str_19():
    return "x" * 19


def str_20():
    return "x" * 20


def str_21():
    return "x" * 21


def nums_19():
    return [1234567890, 123456789]


def nums_20():
    return [1234567890, 1234567890]


def dict_19():
    return {"key_a": {"key_b": "y" * 19}}


def dict_20():
    return {"key_a": {"key_b": "y" * 20}}


def unicode_19():
    return "é" * 19


def run_all():
    for fn in (str_19, str_20, str_21, nums_19, nums_20, dict_19, dict_20, unicode_19):
        fn()


@perf.trace(MAX_SIZE_CAPTURE=20, OUTPUT_FILE="drop")
def drop():
    run_all()


@perf.trace(MAX_SIZE_CAPTURE=20, SPILL_DIR=str(SPILL), OUTPUT_FILE="spill")
def spill():
    run_all()


def check_spills():
    trace = json.load(open(f"{perf.OUTPUT_DIR}/spill.json"))
    expected = {
        "__main__.str_20": str_20(), "__main__.str_21": str_21(), "__main__.nums_20": nums_20(),
        "__main__.dict_20": dict_20(),
    }
    ok = True
    for node in trace["trace"]["children"][0]["children"]:
        result = node["result"]
        if isinstance(result, dict) and "__spilled__" in result:
            path = result["__spilled__"]
            ok &= os.path.exists(path) and os.path.getsize(path) == result["size"]
            ok &= json.load(open(path)) == expected.pop(node["name"])
    return ok and not expected


if __name__ == "__main__":
    setup(__file__)
    drop()
    spill()
    print("spill files ok:", check_spills())
