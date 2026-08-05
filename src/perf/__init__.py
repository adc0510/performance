"""perf — a zero-dependency call-tree profiler / tracer.

Decorate an entry point with :func:`trace` and every project function and method
reached during that call is instrumented, producing a wall/self/cumulative call
tree (printed, and optionally written to a file or POSTed to a collector).

Basic usage::

    import perf

    @perf.trace
    def main() -> None:
        ...

Configuration is done via module-level attributes, e.g. ``perf.OUTPUT_DIR``,
``perf.OUTPUT_FILE``, ``perf.ENDPOINT_URL``, ``perf.TIMEOUT``,
``perf.SCAN_VENV`` — see each below.
"""

import asyncio
import atexit
import inspect
import re
import signal
import sys
import sysconfig
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import partial, wraps
from pathlib import Path
from time import perf_counter
from typing import Any

__version__ = "0.2.1"
__all__ = [
    "CAPTURE_IO",
    "ENDPOINT_URL",
    "HOT_SPOTS",
    "LOG_FUNCTIONS",
    "MAX_DEPTH",
    "MAX_SIZE_CAPTURE",
    "MIN_PCT",
    "OUTPUT_DIR",
    "OUTPUT_FILE",
    "SCAN_VENV",
    "TAGS",
    "TIMEOUT",
    "fmt",
    "print_tree",
    "trace",
]

# ── project vs library path detection ─────────────────────────────────────────

_VENV_PATHS = {
    Path(p).resolve()
    for p in [
        sysconfig.get_path("stdlib"),
        sysconfig.get_path("purelib"),
        sysconfig.get_path("platlib"),
    ]
    if p
}


# ── formatting ────────────────────────────────────────────────────────────────


def fmt(seconds: float) -> str:
    """Format a duration in seconds as a short human-readable string.

    Picks the largest unit that keeps the number readable: seconds (``s``) with
    2 decimals for ``>= 1s``, milliseconds (``ms``) with 2 decimals for
    ``>= 1ms``, otherwise whole microseconds (``µs``). Used throughout the
    printed report and hot-spots table.

    Args:
        seconds: A non-negative duration in seconds.

    Returns:
        The duration rendered with its unit, e.g. ``"1.20 s"``, ``"5.26 ms"``,
        ``"261 µs"``.
    """
    if seconds >= 1:
        return f"{seconds:.2f} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds * 1e6:.0f} µs"


# ── tree ──────────────────────────────────────────────────────────────────────

_current_node: ContextVar[dict | None] = ContextVar("_current_node", default=None)


def _now_iso() -> str:
    """Current local wall-clock time as an ISO-8601 string with offset and microseconds.

    ``perf_counter()`` (used for ``start``/``end``/``time``) is monotonic but
    has no meaningful absolute epoch, so it can't tell a reader *when* a call
    happened — only how long it took. This gives ``start_timestamp``/
    ``end_timestamp`` an absolute, human-readable wall-clock value instead.
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def make_node(name: str) -> dict:
    """Create a fresh call-tree node and stamp its start time.

    A node is the unit the whole tracer is built on. It starts with ``time``
    unset (``None``) — the wrapper fills it in with the measured wall duration
    when the call returns — and ``start`` set to the current ``perf_counter()``
    so overlap-aware wall math (:func:`_wall_from_intervals`) can later place it
    on the timeline. ``start_timestamp`` records the same instant as an
    absolute, human-readable wall-clock time. Input/output values (``args``,
    ``kwargs``, ``result``, ``exception``) and aggregate fields (``cumulative``,
    ``count``) are attached lazily by other helpers and are absent here.

    Args:
        name: Display label for the node, e.g. ``"Class.method"`` or a
            ``module.function`` qualname.

    Returns:
        A dict with keys ``name``, ``time`` (``None``), ``start``,
        ``start_timestamp``, ``end`` (``None``), ``end_timestamp`` (``None``),
        ``children``. ``end``/``end_timestamp`` are pre-seeded here (rather than
        only added by :func:`_finish_node`) purely to fix their key order in the
        written JSON: dicts preserve insertion order, so seeding them right
        after ``start``/``start_timestamp`` keeps the pairs adjacent instead of
        landing after ``children`` once :func:`_finish_node` later assigns them.
    """
    return {
        "name": name,
        "time": None,
        "start": perf_counter(),
        "start_timestamp": _now_iso(),
        "end": None,
        "end_timestamp": None,
        "children": [],
    }


def _finish_node(node: dict) -> None:
    """Stamp a node's wall ``time``, ``end``, and ``end_timestamp`` now that its call has returned.

    ``end`` is ``start + time`` — the wall-clock (monotonic) moment the call
    finished — stored alongside ``start`` so a written trace shows both
    endpoints of the call instead of just start + duration. ``end_timestamp``
    is the same instant as an absolute, human-readable wall-clock time.
    """
    node["time"] = perf_counter() - node["start"]
    node["end"] = node["start"] + node["time"]
    node["end_timestamp"] = _now_iso()


def _restart(node: dict) -> None:
    """Re-stamp a node's ``start``/``start_timestamp`` right before the traced call actually begins.

    :func:`make_node` stamps ``start`` at node creation, but the ``trace()``
    entry-point wrapper does real setup after that — installing the
    ``sys.setprofile``/``threading.setprofile`` hooks — before the wrapped
    function runs. Left alone, that setup cost would be counted as part of the
    root node's time. Calling this right before invoking the wrapped function
    moves ``start``/``start_timestamp`` to the true beginning of the call, so
    setup overhead is excluded the same way capture overhead is excluded by
    :func:`_finish_node`.
    """
    node["start"] = perf_counter()
    node["start_timestamp"] = _now_iso()


# ── output tuning ──────────────────────────────────────────────────────────
# Tree nodes whose wall time is below this fraction of the root total are
# hidden, collapsed into one "… N hidden" line per parent. Default 0.0 shows
# everything; raise it (e.g. 0.01) when the tree gets too noisy.
MIN_PCT = 0.0
# Number of functions to list in the flat "hot spots" summary. 0 disables it.
HOT_SPOTS = 15
# When set, each traced call's raw tree is written as JSON into this directory,
# one file per call — so repeated/concurrent traces don't clobber each other.
# The filename is OUTPUT_FILE if set (user-chosen, ".json" appended if missing),
# otherwise a fresh uuid4 per call. None (the default) disables directory output.
OUTPUT_DIR: str | None = None
# When set, the raw trace tree is written to this path as JSON (e.g. "perf.json").
# If OUTPUT_DIR is also set, this is treated as just a filename inside it rather
# than a full path. None (the default) disables it — set it to opt in.
OUTPUT_FILE: str | None = None
# Whitelist of function names (as they appear as node["name"], e.g. "Class.method"
# or "module.function") to keep when writing the tree out (OUTPUT_DIR/OUTPUT_FILE).
# A matched node's ancestors are kept too, so the path down to it survives, but
# unmatched siblings/subtrees are pruned. Empty (the default) disables filtering —
# the full tree is written, as before. Does not affect what's printed to stdout or
# sent to ENDPOINT_URL.
LOG_FUNCTIONS: list = []
# When set, the raw trace tree is POSTed here at the end of every traced call,
# wrapped as {"status", "tags", "trace"} (see _send_trace). None (the default)
# disables it — set it to opt in (e.g. "http://localhost:7999/api/traces").
ENDPOINT_URL: str | None = None
# Tags attached to every posted trace (e.g. ["prod", "search"]).
TAGS: list = ["dev"]
# Auto-terminate a traced call that runs longer than this many seconds. The
# async path cancels the coroutine (asyncio.wait_for); the sync path interrupts
# via SIGALRM (Unix main thread only — a background thread is left to run and a
# warning is printed). The partial tree collected so far is still printed on
# timeout. None disables the watchdog.
TIMEOUT: float | None = None


# ── auto-patch instances created during a trace session ───────────────────────

_patched_classes: set = set()
# Classes already judged non-project (or our own). The setprofile hook hits
# _patch_class on EVERY call during a trace — including millions of calls inside
# uninstrumented libraries (e.g. BeautifulSoup). Caching the negative verdict
# turns that hot path into one set lookup instead of a per-call filesystem path
# resolution, which otherwise inflates and destabilises CPU-bound self times.
_skip_classes: set = set()

# When True, modules living under stdlib/site-packages are also instrumented.
# Default False: only project code is traced (recommended — patching stdlib
# logging/traceback internals corrupts the logging machinery and crashes).
SCAN_VENV = False

# When SCAN_VENV is True, optionally restrict scanning to these top-level
# packages (e.g. ("langfuse", "openai")). Empty = scan EVERYTHING under
# stdlib/site-packages, which is noisy (importlib, pydantic, …) and can break
# fragile libraries. Scoping to the package you care about is strongly advised.
_SCAN_INCLUDE_PREFIXES: tuple = ()

# Modules to always skip, even when SCAN_VENV is True. Patching these recurses
# through the profiler/logging path and produces endless "--- Logging error ---".
_SKIP_MODULE_PREFIXES = (
    "_pydevd",
    "pydevd",
    "debugpy",
    "pytest",
    "_pytest",
    "ipykernel",
    "IPython",
    "jedi",
    "logging",
    "traceback",
    "linecache",
    "tokenize",
    "threading",
)


# ── argument / return-value capture ───────────────────────────────────────────
# When True, every traced call records its inputs and its return value onto the
# node, so perf.json carries what flowed through each function. Values are kept
# as JSON-native structures (dicts stay dicts, lists stay lists) with FULL
# contents — no truncation — so nested dict results are preserved verbatim.
# Turn off to shrink the file / avoid touching values.
CAPTURE_IO = True
# Guards against runaway/cyclic objects: max recursion depth when converting a
# value to JSON. Beyond it, the sub-value falls back to a repr string.
MAX_DEPTH = 20
# Only capture a value whose JSON-serialized size (in UTF-8 bytes) is below this
# limit. A converted arg / kwarg / result at or above it is dropped and replaced
# with a compact "<TypeName too large: N>" marker, so the node still records that
# something flowed through without bloating perf.json. None (the default)
# disables the limit — capture values of any size.
MAX_SIZE_CAPTURE: int | None = None


# Returned by _jsonable for values with no useful serialization (objects whose
# repr is just the default "<pkg.Class object at 0x…>"). Skipped from the output.
_SKIP = object()

# Matches Python's default object repr, e.g.
#   <qdrant_client.http.api_client.BaseAsyncMiddleware object at 0x7fd8bc2d4d70>
_DEFAULT_REPR = re.compile(r"^<[\w.]+ object at 0x[0-9a-fA-F]+>$")


def _jsonable(value: Any, depth: int = 0) -> Any:
    """Convert a value to a full, JSON-serializable form (no truncation).

    dict/list/tuple/set are recursed into so their contents are preserved
    verbatim; primitives pass through; rich objects are unpacked. Objects with
    only a default repr (``<pkg.Class object at 0x…>``) carry no information and
    return the _SKIP sentinel so the caller can drop them.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth >= MAX_DEPTH:
        return _safe_repr(value)
    try:
        if isinstance(value, dict):
            return {
                (k if isinstance(k, str) else _safe_repr(k)): jv
                for k, v in value.items()
                if (jv := _jsonable(v, depth + 1)) is not _SKIP
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [jv for v in value if (jv := _jsonable(v, depth + 1)) is not _SKIP]
        # pydantic models, dataclasses, and other rich objects
        if hasattr(value, "model_dump"):  # pydantic v2
            return _jsonable(value.model_dump(), depth + 1)
        if hasattr(value, "dict") and callable(value.dict):  # pydantic v1
            return _jsonable(value.dict(), depth + 1)
        if hasattr(value, "__dict__") and vars(value):
            return {"__type__": type(value).__name__, **_jsonable(vars(value), depth + 1)}
    except Exception as e:
        return f"<unconvertible {type(value).__name__}: {e}>"
    r = _safe_repr(value)
    return _SKIP if _DEFAULT_REPR.match(r) else r


def _size_of(jv: Any) -> int:
    """Approximate the serialized size, in UTF-8 bytes, of a JSON-able value.

    Uses the byte length of its compact JSON (UTF-8) encoding as a cheap,
    structure-aware proxy for how much room the value takes in perf.json. Falls
    back to the byte length of its repr if it somehow can't be JSON-encoded.
    """
    import json

    try:
        return len(json.dumps(jv, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return len(_safe_repr(jv).encode("utf-8"))


def _capture_value(value: Any) -> Any:
    """Convert `value` for capture, dropping it if too large.

    Returns the JSON-able form from :func:`_jsonable`, unless it is the ``_SKIP``
    sentinel (no useful serialization) or — when :data:`MAX_SIZE_CAPTURE` is set —
    its serialized size is at or above the limit. In those cases the ``_SKIP``
    sentinel is returned so the caller can drop or mark the slot; an oversize
    value is signalled distinctly via a paired size so callers may annotate it.
    """
    jv = _jsonable(value)
    if jv is _SKIP:
        return _SKIP
    if MAX_SIZE_CAPTURE is not None and _size_of(jv) >= MAX_SIZE_CAPTURE:
        return _SKIP
    return jv


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception as e:  # a __repr__ that raises must not kill the trace
        return f"<unreprable {type(value).__name__}: {e}>"


def _record_inputs(node: dict, args: tuple, kwargs: dict) -> None:
    """Attach a JSON-safe snapshot of a call's arguments to its node.

    No-op unless :data:`CAPTURE_IO` is on. Positional args keep their index in
    ``node["args"]``; a value with no useful serialization (the ``_SKIP``
    sentinel from :func:`_jsonable`) is replaced by a compact ``"<TypeName>"``
    marker so the slot is preserved. Keyword args go to ``node["kwargs"]``, where
    skipped values are dropped entirely rather than marked.

    Args:
        node: The call-tree node to annotate in place.
        args: The positional arguments the traced function was called with.
        kwargs: The keyword arguments the traced function was called with.
    """
    if not CAPTURE_IO:
        return
    # positional args keep their slot; skipped/oversize objects become a compact marker
    node["args"] = [f"<{type(a).__name__}>" if (jv := _capture_value(a)) is _SKIP else jv for a in args]
    node["kwargs"] = {k: jv for k, v in kwargs.items() if (jv := _capture_value(v)) is not _SKIP}


def _record_output(node: dict, result: Any) -> None:
    """Attach a JSON-safe snapshot of a call's return value to its node.

    No-op unless :data:`CAPTURE_IO` is on. Stores the converted value under
    ``node["result"]``; an unserializable result (``_SKIP``) becomes a
    ``"<TypeName>"`` marker so the fact that *something* was returned is kept.

    Args:
        node: The call-tree node to annotate in place.
        result: The value returned by the traced function.
    """
    if CAPTURE_IO:
        jv = _capture_value(result)
        node["result"] = f"<{type(result).__name__}>" if jv is _SKIP else jv


def _exception_origin(exc: BaseException) -> dict | None:
    """Locate the deepest frame in `exc`'s traceback — where it was actually raised.

    A node's ``exception`` repr says *what* went wrong, but every ancestor node
    up to the root records the same repr as the exception re-raises through
    each wrapper — none of them says *where*. This walks ``exc.__traceback__``
    to its last frame (the innermost one, closest to the ``raise``/failing
    statement) and returns its file/line/function/source-text, so a node deep
    in the tree (or the root) can point straight at the origin instead of just
    naming the function that (eventually) surfaced it. Cheap single-frame
    summary; see :func:`_exception_debug` for the full picture.

    Returns:
        A dict with ``file``, ``line``, ``function``, ``text`` (the source line,
        if available), or None if the exception carries no traceback.
    """
    tb = exc.__traceback__
    if tb is None:
        return None
    frames = traceback.extract_tb(tb)
    if not frames:
        return None
    origin = frames[-1]
    return {
        "file": origin.filename,
        "line": origin.lineno,
        "function": origin.name,
        "text": origin.line,
    }


def _exception_debug(exc: BaseException, _seen: frozenset = frozenset()) -> dict:
    """Full, untruncated debug picture of an exception: every frame plus the chained cause.

    :func:`_exception_origin` gives the one line a reader wants first; this is for when that's
    not enough and the whole story is needed. Unlike the rest of capture (:data:`MAX_DEPTH`,
    :data:`MAX_SIZE_CAPTURE`), nothing here is truncated or size-limited — an error is exactly
    the moment a debugger needs everything, not a trimmed sample.

    Includes:

    * ``frames`` — every frame from the outermost call to the innermost raise, each with
      file/line/function/source-text (the same info :func:`_exception_origin` gives for just
      the last one), so the full path to the failure is visible without cross-referencing logs.
    * ``formatted`` — the exact text ``traceback.print_exception`` would print, for pasting
      straight into an issue or terminal.
    * ``caused_by`` — if this exception was raised via ``raise ... from cause`` (``__cause__``)
      or surfaced while handling another (``__context__``, unless explicitly suppressed via
      ``from None``), the same breakdown recursed into that exception too, so a chain is
      fully visible instead of stopping at the outermost wrapper.

    Args:
        exc: The exception to describe.
        _seen: Internal — ``id()``s of exceptions already unwrapped in this chain, so a
            (pathological) exception cycle can't recurse forever.

    Returns:
        A dict with ``frames``, ``formatted``, and optionally ``caused_by``.
    """
    frames = [
        {"file": f.filename, "line": f.lineno, "function": f.name, "text": f.line}
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    debug = {
        "frames": frames,
        "formatted": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    }
    cause = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and id(cause) not in _seen:
        debug["caused_by"] = {"exception": _safe_repr(cause), **_exception_debug(cause, _seen | {id(exc)})}
    return debug


def _record_exception(node: dict, exc: BaseException) -> None:
    """Record, under ``node["exception"]``, the repr of an exception a call raised.

    No-op unless :data:`CAPTURE_IO` is on. Uses :func:`_safe_repr` so an
    exception whose own ``__repr__`` misbehaves can't break the trace. The
    presence of this key is also what flips a shipped trace's status to
    ``"error"`` in :func:`_send_trace`. Also attaches:

    * ``node["exception_origin"]`` (see :func:`_exception_origin`) — the single deepest
      frame, for an at-a-glance file/line;
    * ``node["exception_debug"]`` (see :func:`_exception_debug`) — the full, untruncated
      frame-by-frame traceback (plus any chained cause), for actually debugging the failure.

    Args:
        node: The call-tree node to annotate in place.
        exc: The exception raised out of the traced call (re-raised by the caller).
    """
    if CAPTURE_IO:
        node["exception"] = _safe_repr(exc)
        origin = _exception_origin(exc)
        if origin is not None:
            node["exception_origin"] = origin
        node["exception_debug"] = _exception_debug(exc)


def _is_project_module(module: Any) -> bool:
    """Decide whether a module's functions should be instrumented.

    This is the core "your code vs. someone else's code" gate. A module is
    considered project code (and thus patchable) unless it is disqualified by:

    * matching a :data:`_SKIP_MODULE_PREFIXES` entry (profiler/logging internals
      that recurse or crash when patched);
    * having no ``__file__`` (built-in / namespace module);
    * being a C extension (``.so`` / ``.pyd`` / ``.dylib`` — can't be patched).

    When :data:`SCAN_VENV` is False (default), any module whose file resolves
    under stdlib/site-packages (:data:`_VENV_PATHS`) is excluded, so only your
    own source is traced. When :data:`SCAN_VENV` is True, library code is
    included too — optionally narrowed to :data:`_SCAN_INCLUDE_PREFIXES`.

    Args:
        module: A module object (typically from ``sys.modules``).

    Returns:
        True if functions/classes defined in this module should be wrapped.
    """
    name = getattr(module, "__name__", "")
    if any(name.startswith(prefix) for prefix in _SKIP_MODULE_PREFIXES):
        return False
    filepath = getattr(module, "__file__", None)
    if filepath is None:
        return False
    # C extensions can't be patched
    if filepath.endswith((".so", ".pyd", ".dylib")):
        return False
    if SCAN_VENV:
        if _SCAN_INCLUDE_PREFIXES:
            return any(name.startswith(prefix) for prefix in _SCAN_INCLUDE_PREFIXES)
        return True
    resolved = Path(filepath).resolve()
    return not any(resolved.is_relative_to(venv) for venv in _VENV_PATHS)


def _is_project_class(cls: type) -> bool:
    """Return whether a class is defined in project code, via its module.

    Resolves the class's ``__module__`` to the live module object and defers to
    :func:`_is_project_module`. A class whose defining module is no longer in
    ``sys.modules`` is treated as non-project (not patchable).

    Args:
        cls: The class to test.

    Returns:
        True if the class's methods should be instrumented.
    """
    module = sys.modules.get(cls.__module__)
    if module is None:
        return False
    return _is_project_module(module)


def _patch_module_functions(module: Any) -> None:
    """Wrap the module-level (free) functions defined in a project module.

    Iterates the module's public names (``_``-prefixed are skipped) and replaces
    each function *defined in this module* with a timing wrapper — async
    functions get :func:`_async_wrap`, everything else :func:`_sync_wrap`. It
    deliberately skips:

    * this tracer's own module (patching it would recurse infinitely);
    * classes, imported functions (``__module__`` differs), and anything already
      wrapped (marked with ``_perf_wrapped``, making the pass idempotent).

    Individual attributes that can't be read or reassigned (e.g. immutable
    C-defined members) are skipped rather than raising.

    Args:
        module: A module already judged to be project code.
    """
    # never patch ourselves — would recurse infinitely
    if module.__name__ == __name__:
        return
    for name in list(vars(module)):
        if name.startswith("_"):
            continue
        try:
            attr = vars(module)[name]
        except Exception:
            continue
        if (
            not inspect.isfunction(attr)
            or isinstance(attr, type)
            or getattr(attr, "__module__", None) != module.__name__
            or getattr(attr, "_perf_wrapped", False)
        ):
            continue
        try:
            if inspect.iscoroutinefunction(attr):
                wrapped = _async_wrap(attr, f"{module.__name__}.{name}")
            else:
                wrapped = _sync_wrap(attr, f"{module.__name__}.{name}")
            wrapped._perf_wrapped = True  # type: ignore
            setattr(module, name, wrapped)
        except (TypeError, AttributeError):
            continue


def _patch_class(cls: type) -> None:
    """Wrap the methods of a project class in place (once), with fast negative caching.

    Called on the hot path from the profiler hook for essentially every object
    seen during a trace, so it short-circuits aggressively:

    * already-patched classes (:data:`_patched_classes`) return immediately;
    * classes previously judged non-project (:data:`_skip_classes`) return
      immediately — the negative verdict is cached so a class living in a library
      is path-resolved only once instead of on every call (see the comment on
      ``_skip_classes``).

    For a project class, each attribute is wrapped according to its kind:
    ``staticmethod``/``classmethod`` are re-wrapped preserving that descriptor,
    plain functions are wrapped directly; async callables use
    :func:`_async_wrap`, others :func:`_sync_wrap`. Dunder names are left alone,
    and immutable C-extension types (which reject ``setattr``) are skipped.

    Args:
        cls: The class to instrument.
    """
    if cls in _patched_classes or cls in _skip_classes:
        return
    if not _is_project_class(cls) or cls.__module__ == __name__:
        _skip_classes.add(cls)  # cache the verdict; never re-resolve this class
        return
    _patched_classes.add(cls)
    for name in list(vars(cls)):
        if name.startswith("__"):
            continue
        try:
            raw = vars(cls)[name]
        except Exception:
            continue
        try:
            if isinstance(raw, staticmethod):
                inner = raw.__func__
                wrapped = (
                    _async_wrap(inner, f"{cls.__name__}.{name}")
                    if inspect.iscoroutinefunction(inner)
                    else _sync_wrap(inner, f"{cls.__name__}.{name}")
                )
                setattr(cls, name, staticmethod(wrapped))
            elif isinstance(raw, classmethod):
                inner = raw.__func__
                wrapped = (
                    _async_wrap(inner, f"{cls.__name__}.{name}")
                    if inspect.iscoroutinefunction(inner)
                    else _sync_wrap(inner, f"{cls.__name__}.{name}")
                )
                setattr(cls, name, classmethod(wrapped))
            elif inspect.isfunction(raw):
                if inspect.iscoroutinefunction(raw):
                    setattr(cls, name, _async_wrap(raw, f"{cls.__name__}.{name}"))
                else:
                    setattr(cls, name, _sync_wrap(raw, f"{cls.__name__}.{name}"))
        except (TypeError, AttributeError):
            # immutable C-extension type — skip it and move on
            continue


def _patch_instance(obj: object) -> None:
    """Patch the class of a live instance seen during tracing.

    Thin adapter used by the profiler hook: given an object observed at a
    ``call``/``__init__`` boundary, ensure its type's methods are instrumented.
    Delegates (with all its caching) to :func:`_patch_class`.

    Args:
        obj: Any instance encountered while a trace is active.
    """
    _patch_class(type(obj))


def _patch_all_project_classes() -> None:
    """Instrument every already-imported project module up front.

    Run once at the start of each traced call so functions and classes that
    exist before the profiler hook starts firing are covered too. Walks a
    snapshot of ``sys.modules``, and for each project module (per
    :func:`_is_project_module`) wraps its free functions
    (:func:`_patch_module_functions`) and every class *defined in that module*
    (:func:`_patch_class`). Instances created *during* the call are picked up
    separately by the ``setprofile`` hook.
    """
    for module in list(sys.modules.values()):
        if module is None or not _is_project_module(module):
            continue
        _patch_module_functions(module)  # ← NEW
        for attr_name in list(vars(module)):
            try:
                attr = vars(module)[attr_name]
            except Exception:
                continue
            if isinstance(attr, type) and attr.__module__ == module.__name__:
                _patch_class(attr)


# ── sync / async node wrappers ────────────────────────────────────────────────


def _sync_wrap(func: Any, name: str) -> Any:
    """Wrap a synchronous callable so each call becomes a timed tree node.

    The returned wrapper: creates a node named ``name``, links it under the
    current node (:data:`_current_node`) so the tree mirrors the real call
    stack, and makes it current for the duration via a ``ContextVar`` token —
    all before invoking `func`, so nothing but that bookkeeping sits between the
    node's start stamp and the real call. As soon as `func` returns or raises,
    :func:`_finish_node` stamps ``time``/``end`` immediately — before input/output
    capture, which can be slow (JSON-ifying large args/results) and must not be
    counted as part of the function's own time. Exceptions are recorded then
    re-raised, and the previous current-node is always restored.

    Args:
        func: The original synchronous function/method to time.
        name: Display name for the node (typically ``"Owner.attr"``).

    Returns:
        A ``functools.wraps``-preserving wrapper with the same signature.
    """

    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        parent = _current_node.get()
        node = make_node(name)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = func(*args, **kwargs)
            _finish_node(node)
            _record_inputs(node, args, kwargs)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _finish_node(node)
            _record_inputs(node, args, kwargs)
            _record_exception(node, exc)
            raise
        finally:
            _current_node.reset(token)

    return wrapper


def _async_wrap(func: Any, name: str) -> Any:
    """Wrap a coroutine function so each awaited call becomes a timed tree node.

    Async analogue of :func:`_sync_wrap`: same node creation, input/output
    capture, parent linking, and current-node bookkeeping, but it ``await``\\ s
    the inner coroutine. Timing is stamped immediately around the ``await`` —
    input/output capture happens after — for the same reason as the sync path.
    Because linkage flows through a ``ContextVar``, sibling coroutines running
    concurrently each attach to their own logical parent, and their nodes'
    start/time intervals let the reporter compute overlap-aware wall time.

    Args:
        func: The original coroutine function to time.
        name: Display name for the node (typically ``"Owner.attr"``).

    Returns:
        A ``functools.wraps``-preserving async wrapper with the same signature.
    """

    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        parent = _current_node.get()
        node = make_node(name)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = await func(*args, **kwargs)
            _finish_node(node)
            _record_inputs(node, args, kwargs)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _finish_node(node)
            _record_inputs(node, args, kwargs)
            _record_exception(node, exc)
            raise
        finally:
            _current_node.reset(token)

    return wrapper


# ── setprofile hook: intercepts __init__ and call to patch instances ──────────


def _make_profiler() -> Any:
    """Build the ``sys.setprofile`` hook that patches instances on the fly.

    Up-front patching (:func:`_patch_all_project_classes`) can't see objects
    whose classes are imported or instantiated *after* the trace starts. This
    hook closes that gap: on every ``call`` event, and on ``return`` from an
    ``__init__``, it grabs the frame's ``self`` and routes it through
    :func:`_patch_instance` (whose caching keeps this cheap despite firing on
    virtually every call).

    Returns:
        A ``profiler(frame, event, arg)`` callable to hand to
        ``sys.setprofile`` / ``threading.setprofile``.
    """

    def profiler(frame: Any, event: Any, arg: Any) -> None:
        if (event == "return" and frame.f_code.co_name == "__init__") or event == "call":
            self = frame.f_locals.get("self")
            if self is not None:
                _patch_instance(self)

    return profiler


# ── timeout watchdog ──────────────────────────────────────────────────────────


@contextmanager
def _sync_timeout(seconds: float | None, name: str) -> Any:
    """Interrupt the wrapped block after `seconds` via SIGALRM.

    Only works on the main thread of the main interpreter (a SIGALRM handler
    can't be installed elsewhere); off the main thread it degrades to a no-op
    with a warning, since a running sync call in a background thread can't be
    forcibly killed anyway.
    """
    if seconds is None or not hasattr(signal, "SIGALRM"):
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        print(f"[perf] TIMEOUT ignored for {name}: not on main thread", file=sys.stderr)
        yield
        return

    def _handler(signum: int, frame: Any) -> None:
        raise TimeoutError(f"{name} exceeded TIMEOUT of {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# ── @trace entry-point decorator ─────────────────────────────────────────────

# Config attributes that may be overridden per-decorator, e.g.
# ``@trace(MIN_PCT=0.01, TIMEOUT=5)``. Each name maps to the module-level global
# of the same name; an override temporarily sets that global for the dynamic
# extent of the traced call (including printing/sending), then restores it.
_CONFIG_KEYS = frozenset(
    {
        "MIN_PCT",
        "HOT_SPOTS",
        "OUTPUT_DIR",
        "OUTPUT_FILE",
        "LOG_FUNCTIONS",
        "ENDPOINT_URL",
        "TAGS",
        "TIMEOUT",
        "SCAN_VENV",
        "CAPTURE_IO",
        "MAX_DEPTH",
        "MAX_SIZE_CAPTURE",
    }
)


def _check_overrides(overrides: dict) -> None:
    unknown = set(overrides) - _CONFIG_KEYS
    if unknown:
        raise TypeError(
            f"trace() got unexpected config override(s): {', '.join(sorted(unknown))}. "
            f"Valid keys: {', '.join(sorted(_CONFIG_KEYS))}"
        )


@contextmanager
def _apply_overrides(overrides: dict) -> Any:
    """Temporarily set module-level config globals from `overrides`, then restore.

    All config is read through module globals, so applying overrides for the
    duration of the traced call makes every consumer (patching, capture, print,
    send) honour them without threading a config object through the codebase.

    Note: because it mutates globals, concurrently running traced calls that pass
    *different* overrides can transiently see each other's values — same caveat
    that already applies to setting ``perf.MIN_PCT`` etc. directly.
    """
    if not overrides:
        yield
        return
    g = globals()
    saved = {k: g[k] for k in overrides}
    g.update(overrides)
    try:
        yield
    finally:
        g.update(saved)


def trace(func: Any = None, **overrides: Any) -> Any:
    """Instrument `func`'s call tree. Usable bare or with config overrides.

    ``@trace`` traces using the current module-level config. ``@trace(MIN_PCT=.01,
    TIMEOUT=5, ...)`` overrides those config globals just for calls to this
    function. Valid keys are the module config attributes (see ``_CONFIG_KEYS``).
    """
    # Called with parentheses (@trace(...) / trace(**cfg)): defer until we get func.
    if func is None:
        _check_overrides(overrides)
        return partial(trace, **overrides)
    _check_overrides(overrides)

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # type: ignore
            with _apply_overrides(overrides):
                _patch_all_project_classes()  # ← NEW
                root = make_node(func.__qualname__)
                _current_node.set(root)
                profiler = _make_profiler()
                threading.setprofile(profiler)
                sys.setprofile(profiler)
                _restart(root)  # setup above shouldn't count as the call's own time
                try:
                    if TIMEOUT is not None:
                        result = await asyncio.wait_for(func(*args, **kwargs), TIMEOUT)
                    else:
                        result = await func(*args, **kwargs)
                    _finish_node(root)
                    _record_inputs(root, args, kwargs)
                    _record_output(root, result)
                except BaseException as exc:
                    _finish_node(root)
                    _record_inputs(root, args, kwargs)
                    _record_exception(root, exc)
                    raise
                finally:
                    sys.setprofile(None)
                    threading.setprofile(None)
                    print_tree(root)
                return result
    else:

        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            with _apply_overrides(overrides):
                _patch_all_project_classes()  # ← NEW
                root = make_node(func.__qualname__)
                _current_node.set(root)
                profiler = _make_profiler()
                threading.setprofile(profiler)
                sys.setprofile(profiler)
                try:
                    with _sync_timeout(TIMEOUT, func.__qualname__):
                        _restart(root)  # setup above shouldn't count as the call's own time
                        result = func(*args, **kwargs)
                        _finish_node(root)
                    _record_inputs(root, args, kwargs)
                    _record_output(root, result)
                except BaseException as exc:
                    _finish_node(root)
                    _record_inputs(root, args, kwargs)
                    _record_exception(root, exc)
                    raise
                finally:
                    sys.setprofile(None)
                    threading.setprofile(None)
                    print_tree(root)
                return result

    return wrapper


# ── collapse / merge helpers ─────────────────────────────────────────────────


def _merge_children(nodes: list) -> list:
    """Merge same-named nodes: wall via interval union, cumulative via sum.

    Wall is the union of the merged instances' time intervals — the same rule
    _collapse uses. Using max here instead (the old behaviour) reported only the
    single longest instance, which could be *smaller* than the union of the
    node's own children, producing an impossible "child wall > parent wall".
    """
    merged: dict[str, dict] = {}
    order = []
    for node in nodes:
        name = node["name"]
        if name not in merged:
            merged[name] = {
                "name": name,
                "time": 0.0,
                "cumulative": 0.0,
                "count": 0,
                "start": float("inf"),  # will become min
                "children": [],
                "_intervals": [],
            }
            order.append(name)
        node_wall = node["time"] or 0
        node_cumul = node.get("cumulative") or node_wall
        merged[name]["start"] = min(merged[name]["start"], node.get("start", float("inf")))
        merged[name]["cumulative"] += node_cumul
        merged[name]["count"] += node.get("count", 1)
        merged[name]["children"].extend(node["children"])
        merged[name]["_intervals"].append({"start": node.get("start", 0), "time": node_wall})

    for name in order:
        merged[name]["time"] = _wall_from_intervals(merged[name].pop("_intervals"))
        merged[name]["children"] = _collapse(merged[name]["children"])
        # drop cumulative if it equals wall (no noise)
        if abs(merged[name]["time"] - merged[name]["cumulative"]) < 1e-6:
            del merged[name]["cumulative"]

    return [merged[name] for name in order]


def _wall_from_intervals(nodes: list) -> float:
    """Compute true wall time using interval union (handles parallel + sequential)."""
    intervals = []
    for n in nodes:
        s = n.get("start", 0)
        t = n.get("time") or 0
        intervals.append((s, s + t))
    if not intervals:
        return 0
    intervals.sort()
    merged_start, merged_end = intervals[0]
    total = 0
    for s, e in intervals[1:]:
        if s <= merged_end:
            merged_end = max(merged_end, e)
        else:
            total += merged_end - merged_start
            merged_start, merged_end = s, e
    total += merged_end - merged_start
    return total


def _collapse(children: list) -> list:
    """Compress a sibling list for display, recursing into each survivor.

    Two reductions make a repetitive tree readable:

    1. Cyclic runs like ``[A,B,C,A,B,C,…]`` are folded into a single ``[xN]``
       group (:func:`_collapse_repeating_groups`), taken when detected.
    2. Otherwise same-named siblings — even non-adjacent ones — are merged into
       one ``"name ×N"`` aggregate whose wall is the overlap-aware interval union
       (:func:`_wall_from_intervals`) and whose cumulative/count are summed. The
       first occurrence fixes display order; single, unique nodes pass through
       with their own children collapsed recursively.

    Args:
        children: The child nodes of one parent, in call order.

    Returns:
        A new, possibly shorter list of display nodes (input is not mutated).
    """
    if not children:
        return []

    group_collapsed = _collapse_repeating_groups(children)
    if group_collapsed is not None:
        return group_collapsed

    # Merge same-named siblings into one aggregate node per name — including
    # non-adjacent duplicates (e.g. SQL.run_query interleaved with other calls).
    # First occurrence fixes the display order.
    groups: dict[str, list] = {}
    order: list[str] = []
    for node in children:
        name = node["name"]
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(node)

    collapsed = []
    for name in order:
        group = groups[name]
        if len(group) == 1:
            node = group[0]
            collapsed.append({**node, "children": _collapse(node["children"])})
            continue
        merged_children = _merge_children([c for n in group for c in n["children"]])
        wall = _wall_from_intervals(group)
        cumulative = sum((n.get("cumulative") or n["time"] or 0) for n in group)
        calls = sum(n.get("count", 1) for n in group)
        collapsed.append(
            {
                "name": f"{name} ×{calls}",
                "time": wall,
                "cumulative": cumulative,
                "count": calls,
                "start": min(n.get("start", 0) for n in group),
                "children": merged_children,
            }
        )
    return collapsed


def _collapse_repeating_groups(children: list) -> list | None:
    """Detect [A,B,C,A,B,C,...] cycles and collapse into one [xN] group.

    Only multi-step cycles get a [xN] wrapper. A single name repeated
    ([A,A,A,...]) is not a cycle, so it falls through to _collapse, which
    renders it as the much clearer "A xN" node instead of "[xN] -> A".
    """
    n = len(children)
    for size in range(1, n // 2 + 1):
        if n % size != 0:
            continue
        pattern = [c["name"] for c in children[:size]]
        if len(set(pattern)) <= 1:
            continue
        reps = n // size
        if all(children[i * size + j]["name"] == pattern[j] for i in range(reps) for j in range(size)):
            merged_group = []
            for j in range(size):
                instances = [children[i * size + j] for i in range(reps)]
                wall = _wall_from_intervals(instances)
                cumulative = sum(inst["time"] or 0 for inst in instances)
                merged_children = _merge_children([c for inst in instances for c in inst["children"]])
                merged_group.append(
                    {
                        "name": instances[0]["name"],
                        "time": wall,
                        "cumulative": cumulative,
                        "count": sum(inst.get("count", 1) for inst in instances),
                        "start": min(inst.get("start", 0) for inst in instances),  # ← add
                        "children": _collapse(merged_children),
                    }
                )
            group_wall = sum(c["time"] or 0 for c in merged_group)
            group_cumulative = sum(c.get("cumulative") or c["time"] or 0 for c in merged_group)
            return [
                {
                    "name": f"[x{reps}]",
                    "time": group_wall,
                    "cumulative": group_cumulative,
                    "count": reps,
                    "start": min(c.get("start", 0) for c in merged_group),  # ← add
                    "children": merged_group,
                }
            ]
    return None


# ── self time & flat aggregation ──────────────────────────────────────────────


def _total(node: dict) -> float:
    """Work attributed to a node: cumulative when it merges calls, else wall.

    Wall-vs-wall ratios break down once concurrent calls are merged (a merged
    node's wall is an overlap-aware union, so a child's union can exceed its
    parent's → >100%). Cumulative never exceeds the parent's, so %parent uses
    this instead.
    """
    return node.get("cumulative") or (node["time"] or 0)


def _self_time(node: dict) -> float:
    """Wall time spent inside this node excluding its children (exclusive time)."""
    children = node["children"]
    t = node["time"] or 0
    if not children:
        return t
    return max(t - _wall_from_intervals(children), 0.0)


def _aggregate(root: dict) -> dict[str, dict]:
    """Flat per-function stats over the raw tree.

    For each function name we track three different time totals — they diverge
    under concurrency, and that divergence is the useful signal:
      • self  — summed exclusive (busy) time. Sums of overlapping concurrent
                calls legitimately exceed wall, so this is NOT a % of runtime.
      • wall  — interval union of every call: the real elapsed footprint,
                always ≤ total runtime. self/wall ≫ 1 ⇒ heavy concurrency.
      • cumulative — summed inclusive time across calls.
    """
    stats: dict[str, dict] = {}

    def visit(node: dict) -> None:
        name = node["name"]
        s = stats.get(name)
        if s is None:
            s = {"self": 0.0, "cumulative": 0.0, "calls": 0, "intervals": []}
            stats[name] = s
        s["self"] += _self_time(node)
        s["cumulative"] += node["time"] or 0
        s["calls"] += 1
        s["intervals"].append({"start": node.get("start", 0), "time": node["time"] or 0})
        for child in node["children"]:
            visit(child)

    for child in root["children"]:  # skip root: it's the 100% entry point
        visit(child)
    for s in stats.values():
        s["wall"] = _wall_from_intervals(s.pop("intervals"))
    return stats


def _format_hot_spots(root: dict, total: float) -> str:
    if not HOT_SPOTS:
        return ""
    stats = _aggregate(root)
    if not stats:
        return ""
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["self"], reverse=True)[:HOT_SPOTS]
    rows = []
    for name, s in ranked:
        wall = s["wall"]
        conc = s["cumulative"] / wall if wall else 1.0
        rows.append(
            (
                name,
                fmt(s["self"]),
                fmt(wall),
                f"{min(wall / total, 1.0) * 100:4.0f}%",
                f"{conc:.1f}×",
                str(s["calls"]),
            )
        )
    headers = ("function", "self", "wall", "%tot", "conc", "calls")
    cols = list(zip(headers, *rows, strict=False))
    widths = [max(len(c) for c in col) for col in cols]
    n_w, self_w, wall_w, pct_w, conc_w, calls_w = widths

    def line(name: str, self_t: str, wall: str, pct: str, conc: str, calls: str) -> str:
        return (
            f"{name:<{n_w}}  {self_t:>{self_w}}  {wall:>{wall_w}}  "
            f"{pct:>{pct_w}}  {conc:>{conc_w}}  {calls:>{calls_w}}\n"
        )

    out = f"\nhot spots — top {len(rows)} by self time\n"
    out += (
        "  self = summed busy time · wall = elapsed (overlap-aware) · "
        "%tot = wall/total · conc = cumul/wall ≈ avg parallelism (1.0× = sequential)\n"
    )
    out += "─" * (n_w + self_w + wall_w + pct_w + conc_w + calls_w + 10) + "\n"
    out += line(*headers)
    for r in rows:
        out += line(*r)
    return out


# ── output file writing ──────────────────────────────────────────────────────


def _filter_by_names(children: list, names: set) -> list:
    """Prune a sibling list to nodes matching `names`, keeping ancestors of matches.

    Recurses first so a node survives if either its own name matches or any of
    its descendants do (in which case its subtree is itself filtered down to
    just those matches) — pruning unrelated branches while preserving the path
    to every match.
    """
    kept = []
    for child in children:
        filtered_children = _filter_by_names(child["children"], names)
        if child["name"] in names or filtered_children:
            kept.append({**child, "children": filtered_children})
    return kept


def _write_output(root: dict) -> None:
    """Dump the raw trace tree as JSON, per :data:`OUTPUT_DIR` / :data:`OUTPUT_FILE`.

    * Neither set: no-op.
    * ``OUTPUT_FILE`` only: written to that path as-is (legacy behaviour).
    * ``OUTPUT_DIR`` set: written inside that directory (created if missing),
      named ``OUTPUT_FILE`` (".json" appended if it lacks a suffix) when given,
      otherwise a fresh ``uuid4().json`` per call — so concurrent/repeated
      traces each land in their own file instead of clobbering one another.

    If :data:`LOG_FUNCTIONS` is non-empty, the tree is pruned to only the nodes
    named in it (plus their ancestors) before being written — see
    :func:`_filter_by_names`. The root itself is always kept as the container.
    """
    if not OUTPUT_DIR and not OUTPUT_FILE:
        return

    import json

    tree = {**root, "children": _filter_by_names(root["children"], set(LOG_FUNCTIONS))} if LOG_FUNCTIONS else root

    if OUTPUT_DIR:
        directory = Path(OUTPUT_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        if OUTPUT_FILE:
            filename = OUTPUT_FILE if OUTPUT_FILE.endswith(".json") else f"{OUTPUT_FILE}.json"
        else:
            import uuid

            filename = f"{uuid.uuid4()}.json"
        path = directory / filename
    else:
        path = Path(OUTPUT_FILE)

    with open(path, "w") as f:
        f.write(json.dumps(tree, indent=2, ensure_ascii=False, default=str))


# ── printer ───────────────────────────────────────────────────────────────────


def print_tree(root: dict) -> None:
    """Render a finished trace and emit it to every configured sink.

    This is the reporting entry point, called once when a traced call finishes.
    It builds two things from the raw ``root`` tree and prints them together:

    * a flat **hot spots** table (top :data:`HOT_SPOTS` functions by self time,
      via :func:`_format_hot_spots`);
    * the **call tree**, after :func:`_collapse`, with per-node
      wall/cumulative/self times and ``%tot`` / ``%parent`` columns. Nodes below
      :data:`MIN_PCT` of the root total are hidden into a ``"… N hidden"`` line,
      and obviously-parallel parents are flagged ``[parallel]``. Column widths are
      computed so the table aligns.

    Side effects: writes the report to stdout; if :data:`OUTPUT_FILE` is set,
    dumps the raw ``root`` tree there as JSON; then hands ``root`` to
    :func:`_send_trace` for optional shipping to :data:`ENDPOINT_URL`.

    Args:
        root: The root call-tree node, with ``time`` already filled in.
    """
    lines: list[tuple[str, str, str, str, str]] = []
    total = root["time"] or 1
    threshold = total * MIN_PCT

    def pct_of_parent(node: dict, parent_total: float) -> str:
        if not parent_total:
            return "   —"
        # cap at 100%: residual overflow is a merge artifact, not real work
        ratio = min(_total(node) / parent_total, 1.0)
        return f"{ratio * 100:4.0f}%"

    def walk(node: dict, prefix: str, is_last: bool, parent_total: float) -> None:
        branch = "└─ " if is_last else "├─ "
        t = node["time"] or 0
        cumulative = node.get("cumulative")
        children = node["children"]
        self_t = _self_time(node)

        children_sum = sum(c["time"] or 0 for c in children)
        parallel_hint = "  [parallel]" if len(children) > 1 and children_sum > t * 1.2 else ""

        time_str = f"{fmt(t)} / {fmt(cumulative)}" if cumulative and abs(cumulative - t) > 1e-4 else fmt(t)
        # self time only adds info when there are children to exclude
        self_str = fmt(self_t) if children else "—"
        pct_tot = f"{t / total * 100:4.0f}%"
        pct_par = pct_of_parent(node, parent_total)

        lines.append((prefix + branch + node["name"] + parallel_hint, time_str, self_str, pct_tot, pct_par))

        # apply the noise threshold: small siblings collapse into one summary line
        visible = [c for c in children if (c["time"] or 0) >= threshold]
        hidden = [c for c in children if (c["time"] or 0) < threshold]
        child_prefix = prefix + ("   " if is_last else "│  ")
        node_total = _total(node)
        for i, child in enumerate(visible):
            last = i == len(visible) - 1 and not hidden
            walk(child, child_prefix, last, node_total)
        if hidden:
            ht = sum(c["time"] or 0 for c in hidden)
            h_total = sum(_total(c) for c in hidden)
            lines.append(
                (
                    f"{child_prefix}└─ … {len(hidden)} hidden < {MIN_PCT * 100:g}%",
                    fmt(ht),
                    "—",
                    f"{ht / total * 100:4.0f}%",
                    f"{min(h_total / node_total, 1.0) * 100:4.0f}%" if node_total else "   —",
                )
            )

    root_cumulative = root.get("cumulative")
    if root_cumulative and abs(root_cumulative - (root["time"] or 0)) > 1e-4:
        root_time_str = f"{fmt(root['time'] or 0)} / {fmt(root_cumulative)}"
    else:
        root_time_str = fmt(root["time"] or 0)

    lines.append(("─ " + root["name"], root_time_str, fmt(_self_time(root)), "100%", "  —"))
    children = _collapse(root["children"])
    root_total = _total(root)
    for i, child in enumerate(children):
        walk(child, "", i == len(children) - 1, root_total)

    name_w = max(len(n) for n, *_ in lines) + 2
    time_w = max(len(t) for _, t, *_ in lines) + 2
    self_w = max(max(len(s) for _, _, s, _, _ in lines), len("self")) + 2

    result = _format_hot_spots(root, total)
    result += f"\n{'call':<{name_w}}{'wall/cumul':>{time_w}}{'self':>{self_w}}   %tot  %parent\n"
    result += "─" * (name_w + time_w + self_w + 16) + "\n"
    for name, t, self_t, pct_tot, pct_par in lines:
        result += f"{name:<{name_w}}{t:>{time_w}}{self_t:>{self_w}}   {pct_tot} {pct_par}\n"

    print(result)
    _write_output(root)
    _send_trace(root)


# ── remote trace shipping ─────────────────────────────────────────────────────


_pending_sends: set = set()
_pending_sends_lock = threading.Lock()


@atexit.register
def _flush_pending_sends() -> None:
    """Give in-flight trace POSTs a bounded chance to finish before the process dies.

    ``_send_trace`` ships each trace from a daemon thread so a slow/unreachable
    collector never blocks the traced call. But daemon threads are killed
    outright — mid-write, no cleanup — the instant the interpreter starts
    shutting down. If ``trace()``'s wrapper returns right before the program
    exits (the common case: it's decorating ``main()``), that kill can land
    mid-POST, and the collector receives a truncated (invalid) JSON body
    instead of no body at all. Joining here, before shutdown proceeds, lets
    each pending send either finish or (worst case) time out cleanly.
    """
    with _pending_sends_lock:
        threads = list(_pending_sends)
    for t in threads:
        t.join(timeout=30)


def _send_trace(root: dict) -> None:
    """POST the raw trace tree to ENDPOINT_URL, wrapped as {status, tags, trace}.

    Successful traces are sent from a daemon thread so a slow/unreachable
    collector never blocks (or crashes) the traced call; the thread is tracked
    in :data:`_pending_sends` so :func:`_flush_pending_sends` can wait for it at
    process exit instead of it being killed mid-write.

    Error traces are sent synchronously instead. A process that just errored is
    the one most likely to die right after — a crash handler calling
    ``os._exit``, a supervisor SIGKILL, a worker recycle, a segfault in a C
    extension — and any of those kill daemon threads outright, skipping
    ``atexit`` entirely. Blocking here, before control returns to whatever
    might terminate the process next, is what actually gets the error to the
    collector.

    A failed send is logged to stderr and swallowed either way — telemetry
    must never break the traced call.
    """
    if not ENDPOINT_URL:
        return
    import json
    import urllib.request

    is_error = bool(root.get("exception"))
    payload = {
        "status": "error" if is_error else "ok",
        "tags": TAGS,
        "trace": root,
    }
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def _post() -> None:
        req = urllib.request.Request(
            ENDPOINT_URL,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as e:  # never let telemetry break the traced call
            print(f"[perf] failed to POST trace to {ENDPOINT_URL}: {e}", file=sys.stderr)

    if is_error:
        _post()
        return

    def _post_and_untrack() -> None:
        try:
            _post()
        finally:
            with _pending_sends_lock:
                _pending_sends.discard(threading.current_thread())

    t = threading.Thread(target=_post_and_untrack, name="perf-send-trace", daemon=True)
    with _pending_sends_lock:
        _pending_sends.add(t)
    t.start()


# @trace
# def a() -> None:
#     print(1)
