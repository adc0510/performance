"""Tracing into installed libraries / stdlib with SCAN_VENV."""

import shlex
import textwrap

import perf
from _common import setup

EXPECT = {
    "summary": "Library/stdlib code: invisible by default; SCAN_VENV + _SCAN_INCLUDE_PREFIXES opts a package in",
    "checks": [
        {"status": "works", "desc": "default: library internals are not traced, only the project function calling them",
         "file": "default", "absent": "TextWrapper.*"},
        {"status": "works", "desc": "default: the project caller still shows what went into / came out of the library",
         "file": "default", "node": "__main__.wrap_text", "result": ["aaa bbb", "ccc"]},
        {"status": "works", "fixes": "with _SCAN_INCLUDE_PREFIXES set, perf treated ONLY those packages as project code and stopped tracing your own code (hidden until now because old instrumentation was never removed)",
         "desc": "SCAN_VENV=True + _SCAN_INCLUDE_PREFIXES=(...): those packages are traced IN ADDITION to your own code",
         "file": "scanned", "path": ["scanned", "__main__.split_cmd", "shlex.split", "shlex.get_token", "shlex.read_token"]},
        {"status": "works", "desc": "...including their args/results",
         "file": "scanned", "node": "shlex.split", "args": ["ls -la /tmp"], "result": ["ls", "-la", "/tmp"]},
        {"status": "works", "desc": "enabling SCAN_VENV mid-process also covers classes an earlier trace had judged non-project",
         "file": "scanned", "path": ["scanned", "__main__.wrap_text", "textwrap.wrap", "TextWrapper.wrap"]},
        {"status": "works", "fixes": "was a limitation: library instrumentation persisted for the rest of the process after SCAN_VENV was turned back off",
         "desc": "turning SCAN_VENV back off removes the library instrumentation: the next trace sees only project code again",
         "file": "after", "absent": "textwrap.*"},
        {"status": "works", "new": True, "desc": "...including library classes", "file": "after", "absent": "TextWrapper.*"},
        {"status": "works", "new": True, "desc": "...the library functions are the original objects again (not wrappers)",
         "stdout": "*library restored: True*"},
        {"status": "works", "new": True, "desc": "prefixes match whole package names: 'shlex' covers shlex and shlex.*, not e.g. 'shlexer'",
         "file": "scanned", "absent": "textwrapper*"},
        {"status": "works", "new": True, "desc": "SCAN_VENV can also be enabled for a single trace: @trace(SCAN_VENV=True, _SCAN_INCLUDE_PREFIXES=(...))",
         "file": "one_trace", "path": ["one_trace", "__main__.split_cmd", "shlex.split"]},
    ],
}


def wrap_text(text):
    return textwrap.wrap(text, width=7)


def split_cmd(cmd):
    return shlex.split(cmd)


@perf.trace(OUTPUT_FILE="default")
def default():
    return wrap_text("aaa bbb ccc")


@perf.trace(OUTPUT_FILE="scanned")
def scanned():
    split_cmd("ls -la /tmp")
    return wrap_text("aaa bbb ccc")


@perf.trace(SCAN_VENV=True, _SCAN_INCLUDE_PREFIXES=("shlex",), OUTPUT_FILE="one_trace")
def one_trace():
    return split_cmd("a b")


@perf.trace(OUTPUT_FILE="after")
def after():
    return wrap_text("aaa bbb ccc")


if __name__ == "__main__":
    setup(__file__)
    default()
    perf.SCAN_VENV = True
    perf._SCAN_INCLUDE_PREFIXES = ("textwrap", "shlex")
    scanned()
    perf.SCAN_VENV = False
    perf._SCAN_INCLUDE_PREFIXES = ()
    after()
    print("library restored:", not getattr(textwrap.wrap, "_perf_wrapped", False)
          and not getattr(textwrap.TextWrapper.wrap, "_perf_wrapped", False))
    one_trace()
