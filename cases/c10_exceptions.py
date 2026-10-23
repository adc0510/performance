"""Exceptions: propagated, caught, chained, suppressed, BaseException."""

import perf
from _common import setup

EXPECT = {
    "summary": "Raised/propagated, caught internally, chained (raise from), custom, SUPPRESS_ERRORS, crashing root",
    "checks": [
        {"status": "works", "desc": "the raising call records `exception` (repr), `exception_origin` (deepest frame) and `exception_debug` (all frames + formatted traceback)",
         "node": "__main__.parse_age", "args": ["x"], "exception": "ValueError(*", "has": ["exception_origin", "exception_debug"], "lacks": ["result"]},
        {"status": "works", "desc": "every ancestor the exception passes through records it too",
         "node": "__main__.load_user", "exception": "ValueError(*"},
        {"status": "works", "desc": "the function that catches it has no exception and a normal result",
         "node": "__main__.safe_load", "result": None, "lacks": ["exception"]},
        {"status": "works", "desc": "`raise X from Y`: exception_debug.caused_by holds the original",
         "node": "__main__.wrap_error", "exception": "ConfigError(*"},
        {"status": "works", "desc": "custom exception reprs are kept as-is",
         "node": "__main__.wrap_error", "exception": "ConfigError('bad config', 'db.port')"},
        {"status": "works", "desc": "SUPPRESS_ERRORS=True: failing call records the exception AND returns a default from its annotation (-> list gives [])",
         "file": "suppressed", "node": "__main__.risky_list", "exception": "KeyError(*", "result": []},
        {"status": "works", "desc": "SUPPRESS_ERRORS: callers continue normally after a swallowed error",
         "file": "suppressed", "node": "suppressed_main", "result": 0, "lacks": ["exception"]},
        {"status": "works", "desc": "a crashing root still writes its trace (and the exception keeps propagating to the caller)",
         "file": "crash", "node": "crash_main", "exception": "ZeroDivisionError(*"},
        {"status": "works", "desc": "BaseException subclasses (SystemExit, KeyboardInterrupt) are recorded too",
         "file": "crash", "node": "__main__.bail", "exception": "SystemExit(3)"},
    ],
}


class ConfigError(Exception):
    pass


def parse_age(raw):
    return int(raw)


def load_user(raw):
    return {"age": parse_age(raw)}


def safe_load(raw):
    try:
        return load_user(raw)
    except ValueError:
        return None


def wrap_error():
    try:
        parse_age("port")
    except ValueError as exc:
        raise ConfigError("bad config", "db.port") from exc


def risky_list() -> list:
    return [{}["missing"]]


def bail():
    raise SystemExit(3)


@perf.trace
def main():
    safe_load("x")
    try:
        wrap_error()
    except ConfigError:
        pass


@perf.trace(SUPPRESS_ERRORS=True, OUTPUT_FILE="suppressed")
def suppressed_main():
    return len(risky_list())


@perf.trace(OUTPUT_FILE="crash")
def crash_main():
    try:
        bail()
    except SystemExit:
        pass
    return 1 / 0


if __name__ == "__main__":
    setup(__file__)
    main()
    suppressed_main()
    try:
        crash_main()
    except ZeroDivisionError:
        pass
