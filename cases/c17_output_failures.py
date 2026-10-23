"""Output that can't be written must never change what the traced code does."""

import perf
from _common import setup

EXPECT = {
    "new": True,
    "summary": "Unwritable OUTPUT_FILE / OUTPUT_DIR, unreachable ENDPOINT_URL: traced result/exception unchanged, clear stderr warning",
    "checks": [
        {"status": "works", "fixes": "an unwritable output path raised from inside the traced call, replacing its result", "desc": "unwritable OUTPUT_FILE: the traced function still returns its own result",
         "stdout": "*bad file returned: 42*"},
        {"status": "works", "desc": "...and a clear warning names the path that failed",
         "stdout": "*[perf] failed to write trace to /nonexistent-dir/x/trace.json*"},
        {"status": "works", "desc": "OUTPUT_DIR that is actually a file: the traced function still returns its result",
         "stdout": "*bad dir returned: 42*"},
        {"status": "works", "fixes": "an output error replaced the traced function's own exception", "desc": "a traced function that RAISES while output also fails: the caller gets the ORIGINAL exception, not the output error",
         "stdout": "*caller got: ValueError original*"},
        {"status": "works", "desc": "unreachable ENDPOINT_URL: result unchanged (send failure is logged, not raised)",
         "stdout": "*bad endpoint returned: 42*"},
        {"status": "works", "fixes": "the background send thread read ENDPOINT_URL/API_KEY after the trace ended, so a per-trace @trace(ENDPOINT_URL=...) was ignored",
         "desc": "a per-trace ENDPOINT_URL override is the URL actually used, even though the send runs on a background thread after the trace ended",
         "stdout": "*failed to POST trace to http://127.0.0.1:9/api/traces*"},
        {"status": "works", "desc": "the tree is still printed to stdout when the file can't be written",
         "stdout": "*─ bad_file*"},
    ],
}


def compute():
    return 42


def fails():
    raise ValueError("original")


@perf.trace(OUTPUT_DIR=None, OUTPUT_FILE="/nonexistent-dir/x/trace.json")
def bad_file():
    return compute()


@perf.trace(OUTPUT_FILE=None)
def bad_dir():
    return compute()


@perf.trace(OUTPUT_DIR=None, OUTPUT_FILE="/nonexistent-dir/x/trace.json")
def bad_file_and_raises():
    return fails()


@perf.trace(OUTPUT_DIR=None, OUTPUT_FILE=None, ENDPOINT_URL="http://127.0.0.1:9/api/traces")
def bad_endpoint():
    return compute()


if __name__ == "__main__":
    import tempfile

    setup(__file__)
    print("bad file returned:", bad_file())
    not_a_dir = tempfile.NamedTemporaryFile(delete=False).name
    perf.OUTPUT_DIR = not_a_dir
    print("bad dir returned:", bad_dir())
    setup(__file__)
    try:
        bad_file_and_raises()
    except Exception as exc:
        print("caller got:", type(exc).__name__, exc)
    print("bad endpoint returned:", bad_endpoint())
