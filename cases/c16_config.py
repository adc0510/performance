"""Config knobs that change what gets traced or captured."""

import perf
from _common import OUT, setup

EXPECT = {
    "summary": "SKIP_FUNCTIONS, SKIP_VARIABLES, LOG_FUNCTIONS, CAPTURE_IO, MAX_SIZE_CAPTURE (+SPILL_DIR), MAX_DEPTH, MIN_PCT",
    "checks": [
        {"status": "works", "desc": "SKIP_FUNCTIONS (fnmatch): the matched call AND everything under it disappear",
         "file": "skip_fn", "absent": "__main__.noisy*"},
        {"status": "works", "desc": "SKIP_FUNCTIONS: descendants of a skipped call are gone too",
         "file": "skip_fn", "absent": "__main__.leaf"},
        {"status": "works", "desc": "SKIP_FUNCTIONS: siblings are unaffected",
         "file": "skip_fn", "node": "__main__.useful", "result": 1},
        {"status": "works", "desc": "SKIP_VARIABLES: matching kwargs are dropped, positional args become `<skipped:name>`, nested dict keys / object fields are dropped",
         "file": "skip_var", "node": "__main__.login",
         "args": ["ann", "<skipped:password>", {"__type__": "Conn", "host": "db", "port": 1}], "lacks": ["kwargs"], "result": {"user": "ann"}},
        {"status": "works", "fixes": "was a limitation: SKIP_VARIABLES / _SCAN_INCLUDE_PREFIXES could not be passed to @trace(...)",
         "desc": "SKIP_VARIABLES can be passed per trace: @trace(SKIP_VARIABLES=[...]) redacts in that trace only",
         "file": "skip_var_override", "node": "__main__.login", "args": ["ann", "<skipped:password>", {"__type__": "Conn", "host": "db", "port": 1, "secret_token": "abc"}]},
        {"status": "works", "new": True, "desc": "...and doesn't leak into other traces (the module-level setting is untouched)",
         "file": "no_io", "count": "*", "n": 2},
        {"status": "works", "desc": "LOG_FUNCTIONS: the written file keeps only matching nodes + their ancestors",
         "file": "log_fn", "absent": "__main__.useful"},
        {"status": "works", "desc": "LOG_FUNCTIONS: the kept path survives",
         "file": "log_fn", "path": ["log_fn", "__main__.outer", "__main__.leaf"]},
        {"status": "works", "desc": "CAPTURE_IO=False: timing-only nodes (no args/result)",
         "file": "no_io", "node": "__main__.useful", "lacks": ["args", "result"]},
        {"status": "limitation", "desc": "MAX_SIZE_CAPTURE without SPILL_DIR: oversized values are replaced by a `<TypeName>` marker",
         "file": "max_size", "node": "__main__.big", "result": "<list>"},
        {"status": "works", "desc": "MAX_SIZE_CAPTURE counts numbers too: a huge list of ints is oversized like any other value",
         "file": "max_size", "node": "__main__.big_numbers", "result": "<list>"},
        {"status": "works", "desc": "MAX_SIZE_CAPTURE + SPILL_DIR: oversized values are moved out of the tree; node holds {__spilled__: path, size}",
         "file": "spill", "node": "__main__.big", "raw": True, "result": {"__spilled__": "*.json", "...": True}},
        {"status": "works", "desc": "...and the spill file holds the FULL value inline (never a ref into the trace's pool)",
         "stdout": "*spilled file content: [\"0\", \"1\", \"2\"*"},
        {"status": "works", "desc": "small values are still inline under MAX_SIZE_CAPTURE",
         "file": "spill", "node": "__main__.useful", "result": 1},
        {"status": "works", "desc": "MIN_PCT (stdout only): nodes under that fraction of total time are folded into a `… N hidden` line; the JSON keeps everything",
         "stdout": "*min_pct*└─ … 2 hidden*"},
        {"status": "works", "desc": "MIN_PCT applies to the root's direct children too",
         "stdout": "*─ min_pct*\n├─ __main__.mixed*\n└─ … 1 hidden*"},
        {"status": "works", "desc": "MIN_PCT does not prune the written JSON",
         "file": "min_pct", "count": "__main__.useful", "n": 2},
        {"status": "works", "desc": "MAX_DEPTH: deeper nesting becomes a repr string",
         "file": "depth", "node": "__main__.nested", "result": [["[['x']]"]]},
    ],
}


def leaf():
    return 0


def noisy_poll():
    return leaf()


def useful():
    return 1


def outer():
    return leaf()


def big():
    return [str(i) for i in range(5000)]


def big_numbers():
    return list(range(5000))


def nested():
    return [[[["x"]]]]


class Conn:
    def __init__(self):
        self.host = "db"
        self.port = 1
        self.secret_token = "abc"


def login(user, password, conn, api_key=None):
    return {"user": user, "token": "t0k"}


@perf.trace(SKIP_FUNCTIONS=["*.noisy*"], OUTPUT_FILE="skip_fn")
def skip_fn():
    noisy_poll()
    return useful()


@perf.trace(OUTPUT_FILE="skip_var")
def skip_var():
    return login("ann", "hunter2", Conn(), api_key="k")


@perf.trace(SKIP_VARIABLES=["password"], OUTPUT_FILE="skip_var_override")
def skip_var_override():
    return login("ann", "hunter2", Conn(), api_key="k")


@perf.trace(LOG_FUNCTIONS=["__main__.leaf"], OUTPUT_FILE="log_fn")
def log_fn():
    useful()
    return outer()


@perf.trace(CAPTURE_IO=False, OUTPUT_FILE="no_io")
def no_io():
    return useful()


@perf.trace(MAX_SIZE_CAPTURE=1000, OUTPUT_FILE="max_size")
def max_size():
    return len(big()) + len(big_numbers())


@perf.trace(MAX_SIZE_CAPTURE=1000, SPILL_DIR=str(OUT / "c16_config" / "spill"), OUTPUT_FILE="spill")
def spill():
    useful()
    return len(big())


@perf.trace(MIN_PCT=0.5, OUTPUT_FILE="min_pct")
def min_pct():
    useful()
    return mixed()


def mixed():
    useful()
    return len(big())


@perf.trace(MAX_DEPTH=2, OUTPUT_FILE="depth")
def depth():
    return nested()


if __name__ == "__main__":
    setup(__file__)
    skip_fn()
    perf.SKIP_VARIABLES = ["password", "*token*", "api_key"]
    skip_var()
    perf.SKIP_VARIABLES = []
    skip_var_override()
    log_fn()
    no_io()
    max_size()
    spill()
    for spilled in (OUT / "c16_config" / "spill").glob("*.json"):
        print("spilled file content:", spilled.read_text()[:100])
    min_pct()
    depth()
