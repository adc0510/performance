"""Every way a project function can be imported and referenced."""

import importlib

import app.util
import app.util as u
import perf
from _common import setup
from app import normalize as pkg_normalize  # re-exported through app/__init__.py
from app.util import slugify

EXPECT = {
    "summary": "import x.y / import as / from-import / package re-export / lazy import inside the trace / importlib",
    "checks": [
        {"status": "works", "desc": "`import app.util; app.util.f()` traced as `app.util.f`",
         "path": ["main", "app.util.slugify", "app.util.normalize"]},
        {"status": "works", "desc": "`from app.util import slugify` (name bound before the trace started) is traced",
         "count": "app.util.slugify", "n": 3},
        {"status": "works", "desc": "names are the DEFINING module's, whatever alias the caller used (`as u`, package re-export)",
         "count": "app.util.normalize", "n": 4},
        {"status": "works", "fixes": "was a limitation: code created during the trace (a class defined in a function, a module imported or reloaded mid-trace) was patched late or not at all, so first calls / its functions were missed",
         "desc": "module imported for the first time INSIDE the trace (importlib.import_module): its functions are traced from the first call",
         "node": "app.lazy.late_function", "args": [1], "result": 2},
        {"status": "works", "new": True, "desc": "...and its classes are traced from the first method call", "count": "LateClass.method", "n": 2},
        {"status": "works", "desc": "a module imported mid-trace IS fully traced on the next trace run in the same process",
         "file": "second", "node": "app.lazy.late_function", "args": [1], "result": 2},
    ],
}


@perf.trace
def main():
    app.util.slugify("A B")
    slugify("C D")
    u.slugify("E F")
    pkg_normalize("G")
    lazy = importlib.import_module("app.lazy")
    lazy.late_function(1)
    obj = lazy.LateClass()
    obj.method(1), obj.method(2)


@perf.trace(OUTPUT_FILE="second")
def second():
    from app.lazy import late_function

    return late_function(1)


if __name__ == "__main__":
    setup(__file__)
    main()
    second()
