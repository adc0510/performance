"""Config settings combined: do they still do exactly what each promises?"""

import json

import perf
from _common import OUT, setup

SPILL = OUT / "c16_config_interactions" / "spill"

EXPECT = {
    "new": True,
    "summary": "Combined settings: SKIP_FUNCTIONS+LOG_FUNCTIONS, SKIP_VARIABLES everywhere (nested results, objects, spill files, exceptions), MAX_SIZE_CAPTURE+SPILL_DIR+MAX_DEPTH",
    "checks": [
        {"status": "works", "desc": "SKIP_FUNCTIONS wins over LOG_FUNCTIONS: a logged function under a skipped one is absent",
         "file": "skip_log", "absent": "__main__.wanted_but_skipped"},
        {"status": "works", "desc": "...a logged function elsewhere is kept with its ancestor path",
         "file": "skip_log", "path": ["skip_log", "__main__.outer", "__main__.wanted"]},
        {"status": "works", "desc": "...unlogged siblings are pruned from the file",
         "file": "skip_log", "absent": "__main__.sibling"},
        {"status": "works", "desc": "SKIP_VARIABLES removes matching keys from nested RESULTS",
         "file": "redact", "node": "__main__.load_config", "result": {"db": {"host": "h", "options": {"timeout": 3}}}},
        {"status": "works", "desc": "SKIP_VARIABLES removes matching object fields from `self`",
         "file": "redact", "node": "Client.call", "args": [{"__type__": "Client", "name": "c"}]},
        {"status": "works", "desc": "SKIP_VARIABLES applies inside SPILL FILES too (redacted value never reaches disk)",
         "stdout": "*secret in spill file: False*"},
        {"status": "works", "desc": "...and the spilled data is otherwise complete", "stdout": "*spill rows: 50*"},
        {"status": "limitation", "desc": "SKIP_VARIABLES drops values by NAME only, so it can't scrub text: a secret inside an exception message is still recorded — use REDACT_PATTERNS for that (below)",
         "file": "redact", "node": "__main__.leaks_in_error", "exception": "*hunter2*"},
        {"status": "works", "fixes": "was a limitation: there was no way to keep a secret that appears inside text (an exception message, a log line passed as an argument) out of the trace",
         "desc": "REDACT_PATTERNS: a regex match inside an exception message is replaced by <redacted>",
         "file": "patterns", "node": "__main__.leaks_in_error", "exception": "ValueError('login failed for password=<redacted>')"},
        {"status": "works", "new": True, "desc": "...nowhere in that trace file (args, results, dict keys, reprs, traceback text, formatted traceback) does the secret appear",
         "stdout": "*secret anywhere in patterns trace: False*"},
        {"status": "works", "new": True, "desc": "...string arguments are scrubbed too, the rest of the text kept",
         "file": "patterns", "node": "__main__.log_line", "args": ["user=ann password=<redacted> ok"]},
        {"status": "works", "new": True, "desc": "...and dict keys", "file": "patterns", "node": "__main__.keyed", "result": {"<redacted>": 1, "plain": 2}},
        {"status": "works", "new": True, "desc": "an invalid pattern stops the trace with a clear error BEFORE the traced function runs (fails closed instead of leaking)",
         "stdout": "*bad pattern: ValueError perf: invalid REDACT_PATTERNS entry*body ran: False*"},
        {"status": "works", "fixes": "a value cut by MAX_DEPTH bypassed MAX_SIZE_CAPTURE, so oversized data stayed in the trace instead of spilling", "desc": "MAX_SIZE_CAPTURE + SPILL_DIR + MAX_DEPTH: a depth-cut repr counts toward the size limit (so it spills), and the spill file honours MAX_DEPTH",
         "stdout": "*deep spill cut at depth: True*"},
        {"status": "works", "desc": "every trace stays valid JSON under combined limits (file parsed)",
         "file": "limits", "count": "__main__.deep_big", "n": 1},
    ],
}


def wanted_but_skipped():
    return 1


def noisy():
    return wanted_but_skipped()


def wanted():
    return 2


def sibling():
    return 3


def outer():
    noisy()
    return wanted()


@perf.trace(SKIP_FUNCTIONS=["*.noisy"], LOG_FUNCTIONS=["__main__.wanted", "__main__.wanted_but_skipped"], OUTPUT_FILE="skip_log")
def skip_log():
    sibling()
    return outer()


def load_config():
    return {"db": {"host": "h", "password": "hunter2", "options": {"timeout": 3, "api_token": "t"}}}


class Client:
    def __init__(self):
        self.name = "c"
        self.secret_key = "k"

    def call(self):
        return "ok"


def big_rows():
    return [{"id": i, "password": "hunter2", "name": f"row{i}"} for i in range(50)]


def leaks_in_error():
    raise ValueError("login failed for password=hunter2")


@perf.trace(MAX_SIZE_CAPTURE=200, SPILL_DIR=str(SPILL), OUTPUT_FILE="redact")
def redact():
    load_config()
    Client().call()
    big_rows()
    try:
        leaks_in_error()
    except ValueError:
        pass


def deep_big():
    v = ["x" * 300]
    for _ in range(6):
        v = [v]
    return v


def log_line(text):
    return len(text)


def keyed():
    return {"hunter2": 1, "plain": 2}


@perf.trace(REDACT_PATTERNS=[r"(?<=password=)\w+", r"hunter2"], OUTPUT_FILE="patterns")
def patterns():
    log_line("user=ann password=hunter2 ok")
    keyed()
    try:
        leaks_in_error()
    except ValueError:
        pass
    return load_config()


BODY_RAN = []


@perf.trace(REDACT_PATTERNS=["(unclosed"], OUTPUT_FILE="bad_pattern")
def bad_pattern():
    BODY_RAN.append(1)


@perf.trace(MAX_SIZE_CAPTURE=100, SPILL_DIR=str(SPILL / "limits"), MAX_DEPTH=3, OUTPUT_FILE="limits")
def limits():
    return deep_big()


if __name__ == "__main__":
    setup(__file__)
    skip_log()
    perf.SKIP_VARIABLES = ["password", "*token*", "secret*"]
    redact()
    limits()
    perf.SKIP_VARIABLES = []
    patterns()
    print("secret anywhere in patterns trace:", "hunter2" in (OUT / "c16_config_interactions" / "patterns.json").read_text())
    try:
        bad_pattern()
    except ValueError as exc:
        print("bad pattern:", type(exc).__name__, str(exc)[:40], "body ran:", bool(BODY_RAN))
    texts = [p.read_text() for p in SPILL.glob("*.json")]
    print("secret in spill file:", any("hunter2" in t for t in texts))
    print("spill rows:", sum(len(json.loads(t)) for t in texts))
    deep = [json.loads(p.read_text()) for p in (SPILL / "limits").glob("*.json")]
    # deep_big's result and the root's (the same value) are each spilled: 2 files
    print("deep spill cut at depth:", len(deep) == 2 and all(isinstance(d[0][0][0], str) for d in deep))
