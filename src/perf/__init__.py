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
``perf.OUTPUT_FILE``, ``perf.ENDPOINT_URL``, ``perf.API_KEY``,
``perf.TIMEOUT``, ``perf.SCAN_VENV``, ``perf.SUPPRESS_ERRORS`` — see each
below.
"""

import asyncio
import atexit
import contextvars
import dataclasses
import enum
import functools
import hashlib
import inspect
import itertools
import linecache
import marshal
import operator
import os
import json as _json
import re
import signal
import sys
import sysconfig
import threading
import traceback
import types
import typing
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from fnmatch import fnmatch
from functools import partial, wraps
from pathlib import Path
from time import perf_counter
from typing import Any

# Optional C speedups (src/perf/_speedups.c, Python 3.12+). PERF_PURE_PYTHON=1
# forces the pure-Python wrappers; perf behaves the same either way.
_speedups: Any = None
if sys.version_info >= (3, 12) and not os.environ.get("PERF_PURE_PYTHON"):
    try:
        # Not `from perf import _speedups`: that would find the None above.
        _speedups = __import__("importlib").import_module(__name__ + "._speedups")
    except ImportError:
        pass
_NodeWrapper: Any = _speedups.NodeWrapper if _speedups is not None else None


def _is_function(obj: Any) -> bool:
    """inspect.isfunction, also true for perf's C node wrapper (which stands
    in for a wrapped Python function)."""
    return inspect.isfunction(obj) or (_NodeWrapper is not None and type(obj) is _NodeWrapper)

__version__ = "0.12.0"
__all__ = [
    "Attachment",
    "TraceTimeout",
    "API_KEY",
    "CAPTURE_IO",
    "DEFAULT_RETURN",
    "ENDPOINT_URL",
    "HOT_SPOTS",
    "LOG_FUNCTIONS",
    "MAX_DEPTH",
    "MAX_SIZE_CAPTURE",
    "MIN_PCT",
    "OUTPUT_DIR",
    "OUTPUT_FILE",
    "PRINT_REPORT",
    "SCAN_VENV",
    "SEND_TIMEOUT",
    "SKIP_FUNCTIONS",
    "SKIP_VARIABLES",
    "SPILL_DIR",
    "SUPPRESS_ERRORS",
    "TAGS",
    "TIMEOUT",
    "attach",
    "attachments",
    "enable_attach",
    "expand_locals",
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
# >0 while executing inside a SKIP_FUNCTIONS call (at any nesting depth), so
# _sync_wrap/_async_wrap can bypass node creation for everything called from
# within it too, not just the skipped call itself.
_skip_depth: ContextVar[int] = ContextVar("_skip_depth", default=0)
# The current top-level trace's value pool (see _ValuePool below) — None
# outside any traced call. Set once per top-level trace() invocation, never
# shared across traces: a fresh pool every time, so one trace's captured
# values can never depend on (or leak into) another's.
_current_pool: ContextVar["_ValuePool | None"] = ContextVar("_current_pool", default=None)


class _TraceState:
    """Lifecycle of one top-level trace, shared by everything recording into it.

    Work can run on other threads (see the thread hooks) and may still be
    running when the trace ends and its tree is written out. ``lock`` guards
    every change to the shared tree and value pool, and once ``open`` is False
    (set under the lock, right before the trace is emitted) nothing touches
    them again: a call still running at that moment stays unfinished
    (``time: null``) in the output — which is exactly what it was.
    """

    __slots__ = ("lock", "open", "graft_tokens", "sources", "_source_ids", "_source_prefix", "captures", "line_sources",
                 "reuse", "reuse_by_out", "last_exception")

    def __init__(self, source_prefix: str = "") -> None:
        # Re-entrant: an asynchronous exception perf can't defer (Ctrl-C)
        # landing between acquire and release must not deadlock the thread
        # the next time it records something.
        self.lock = threading.RLock()
        self.open = True
        self.graft_tokens: list = []
        # TRACE_LOCALS source text, each line stored once per trace:
        # {source id: {"file": path, "lines": {"<line>": text}}}. A node with
        # locals names its file's id as "source"; its entries carry only the
        # line number. `source_prefix` keeps a worker process's ids distinct
        # when its table is merged into the parent's (as with _ValuePool).
        self.sources: dict = {}
        self._source_ids: dict = {}
        self._source_prefix = source_prefix
        # node name -> calls whose IO was captured (MAX_CAPTURES_PER_FUNCTION)
        self.captures: dict = {}
        # (filename, line) -> source id ("" when the line has no text), so a
        # TRACE_LOCALS line in a loop looks its statement up once per trace
        self.line_sources: dict = {}
        # Conversions of plain-valued containers, reused while unchanged (see
        # _reused_conversion): id(container) -> entry, id(converted) -> entry.
        self.reuse: dict = {}
        self.reuse_by_out: dict = {}
        # (exception, origin, interned debug) of the exception recorded last:
        # one unwinding through N traced calls is described once, at the
        # innermost node, and reused by the nodes it passes through next (see
        # _record_exception_fields). Only the latest is kept, so a loop that
        # raises and catches doesn't keep every traceback (and its frames) alive.
        self.last_exception: "tuple | None" = None

    def capture_allowed(self, name: str, limit: int) -> bool:
        """Whether a call of `name` still gets its IO captured, counting it if so."""
        n = self.captures.get(name, 0)
        if n >= limit:
            return False
        self.captures[name] = n + 1
        return True

    def close(self) -> None:
        with self.lock:
            self.open = False
        self.last_exception = None  # its traceback holds frames (and their locals)
        for token in self.graft_tokens:
            _pool_grafts.pop(token, None)

    def source_line(self, filename: str, line: int, text: str) -> "str | None":
        """Record `text` as `filename`'s line `line` (once) and return the
        file's source id -- or None once the trace has closed."""
        with self.lock:
            if not self.open:
                return None
            sid = self._source_ids.get(filename)
            if sid is None:
                sid = f"{self._source_prefix}s{len(self._source_ids)}"
                self._source_ids[filename] = sid
                self.sources[sid] = {"file": filename, "lines": {}}
            self.sources[sid]["lines"].setdefault(str(line), text)
            return sid

    def attach(self, parent: dict, node: dict) -> bool:
        """Link `node` under `parent` — unless the trace already closed."""
        with self.lock:
            if not self.open:
                return False
            parent["children"].append(node)
            return True

    def complete(self, node: dict, fields: dict) -> None:
        """Apply a finished call's fields to its (already attached) node."""
        if "capture_time" in fields:
            fields["capture_time"] = round(fields["capture_time"], _TIME_DECIMALS)
        with self.lock:
            if self.open:
                node.update(fields)


# The current top-level trace's state — None outside any trace.
_current_trace: ContextVar["_TraceState | None"] = ContextVar("_current_trace", default=None)


# Depth of perf's own hook code on this thread (monitoring callbacks, class /
# import hooks). Together with _capturing it tells the TIMEOUT handler that the
# alarm interrupted perf itself -- whose defensive `except Exception` blocks
# would swallow the TimeoutError -- so it retries a moment later instead.
_hook_state = threading.local()


# A TIMEOUT that fired while perf's own code was running (where raising is
# unsafe: perf's defensive handlers would swallow it, a lock could be left
# held, a sys.monitoring callback could be interrupted mid-way). Holds the
# (thread, message) until a safe point on that thread raises it: a node wrapper about to call into the
# program (_raise_pending_timeout), or a retry alarm landing in program code.
_timeout_pending: list = []


class TraceTimeout(TimeoutError):
    """Raised when a traced call exceeds TIMEOUT. A TimeoutError subclass, so
    `except TimeoutError` still catches it -- but SUPPRESS_ERRORS never
    swallows it (that would let a timed-out program keep running)."""


def _suppressible(exc: BaseException) -> bool:
    """Whether SUPPRESS_ERRORS may swallow `exc`: ordinary failures only.
    SystemExit, KeyboardInterrupt, GeneratorExit and asyncio.CancelledError
    (all BaseException, not Exception) are control flow, not errors -- and a
    TraceTimeout must stop the program."""
    return isinstance(exc, Exception) and not isinstance(exc, TraceTimeout)


def _raise_pending_timeout() -> None:
    # Entries are (thread ident, message): SIGALRM is handled on the main
    # thread, and only that thread may receive the TraceTimeout -- a worker
    # thread running traced code at the same moment must not.
    if _timeout_pending and _timeout_pending[0][0] == threading.get_ident():
        message = _timeout_pending[0][1]
        _timeout_pending.clear()
        raise TraceTimeout(message)


def _in_perf_internals() -> bool:
    return bool(_capturing.get() or getattr(_hook_state, "depth", 0))


def _hook_enter() -> None:
    _hook_state.depth = getattr(_hook_state, "depth", 0) + 1


def _hook_exit() -> None:
    _hook_state.depth -= 1


# >0 while perf itself is converting a captured value: user code that runs as
# a side effect of capture (a property read, __getattr__, a pydantic
# model_dump) must not be recorded as if the program had called it.
_capturing: ContextVar[int] = ContextVar("_capturing", default=0)
# >0 while converting a value only to COMPARE it between lines (TRACE_LOCALS
# change detection, see _locals_comparable), never to store it.
_comparing: ContextVar[int] = ContextVar("_comparing", default=0)


def _recording() -> "tuple[dict, _TraceState] | None":
    """(current node, trace state) if calls should be recorded right now."""
    parent = _current_node.get()
    if parent is None or _capturing.get():
        return None
    state = _current_trace.get()
    if state is None or not state.open:
        return None
    return parent, state


def _completion(node: dict) -> dict:
    """The fields a call adds when it ends — built privately, then applied
    in one step under the trace lock (see :meth:`_TraceState.complete`)."""
    fields = {"time": round(perf_counter() - node["start"], _TIME_DECIMALS)}
    if "capture_time" in node:
        fields["capture_time"] = node["capture_time"]
    if "capture_skipped" in node:  # so _record_output skips this call too
        fields["capture_skipped"] = True
    return fields


# Per-trace config overrides (``@trace(MIN_PCT=..., OUTPUT_FILE=...)``),
# context-local so that concurrently running traces -- on other threads, or as
# other asyncio tasks -- each see only their own overrides. Work a trace hands
# to a copied context (asyncio tasks, ``to_thread``) inherits them.
_overrides: ContextVar["dict | None"] = ContextVar("_overrides", default=None)
_G = globals()

# Settings that hold a list of names/patterns. A plain string given for one
# (TRACE_LOCALS="*helper") means that single entry -- iterated as-is it would
# be one pattern per character, and its "*" would match every function.
_LIST_KEYS = frozenset(
    {"LOG_FUNCTIONS", "SKIP_FUNCTIONS", "SKIP_VARIABLES", "TRACE_LOCALS", "REDACT_PATTERNS", "TAGS", "_SCAN_INCLUDE_PREFIXES"}
)


class _Config:
    """Effective config: the current context's override for a setting if any,
    else the module-level value (``perf.MIN_PCT = ...``). Every config read in
    this module goes through the ``_cfg`` instance."""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        overrides = _overrides.get()
        if overrides is not None and name in overrides:
            value = overrides[name]
        else:
            try:
                value = _G[name]
            except KeyError:
                raise AttributeError(name) from None
        if type(value) is str and name in _LIST_KEYS:
            return (value,)
        return value


_cfg = _Config()


# (name, SKIP_FUNCTIONS as a tuple) -> verdict, so fnmatch runs once per name
# and config rather than on every call. Cleared when it grows past
# _SKIP_CACHE_MAX (SKIP_FUNCTIONS changed many times, e.g. per-trace overrides).
_skip_cache: dict = {}
_SKIP_CACHE_MAX = 4096


def _is_skipped(name: str) -> bool:
    """Whether ``name`` matches an entry in SKIP_FUNCTIONS (glob-aware via fnmatch)."""
    patterns = _cfg.SKIP_FUNCTIONS
    if not patterns:
        return False
    key = (name, tuple(patterns))
    verdict = _skip_cache.get(key)
    if verdict is None:
        if len(_skip_cache) >= _SKIP_CACHE_MAX:
            _skip_cache.clear()
        verdict = _skip_cache[key] = any(fnmatch(name, pattern) for pattern in patterns)
    return verdict


_skip_var_cache: dict = {}


def _is_skipped_variable(name: str) -> bool:
    """Whether ``name`` matches an entry in SKIP_VARIABLES (glob-aware via
    fnmatch). Memoized like _is_skipped: it runs for every dict key and slot
    converted while SKIP_VARIABLES is set."""
    patterns = _cfg.SKIP_VARIABLES
    if not patterns:
        return False
    key = (name, tuple(patterns))
    verdict = _skip_var_cache.get(key)
    if verdict is None:
        if len(_skip_var_cache) >= _SKIP_CACHE_MAX:
            _skip_var_cache.clear()
        verdict = _skip_var_cache[key] = any(fnmatch(name, pattern) for pattern in patterns)
    return verdict


def _now_iso() -> str:
    """Current local wall-clock time as an ISO-8601 string with offset and microseconds.

    ``perf_counter()`` (used for ``start``/``time``) is monotonic but has no
    meaningful absolute epoch, so it can't tell a reader *when* a call
    happened — only how long it took. The ROOT node alone carries
    ``start_timestamp``/``end_timestamp`` from this; any other node's
    wall-clock start is ``root.start_timestamp + (node.start - root.start)``
    seconds, so stamping every node would only repeat that (and cost two
    clock reads + formats per call).
    """
    return datetime.now().astimezone().isoformat(timespec="microseconds")


# Durations and timeline positions are rounded to this many decimals (1 ns):
# perf_counter() has no meaningful precision beyond it, and the full 17-digit
# repr of every float was a large share of every written trace.
_TIME_DECIMALS = 9


def make_node(name: str) -> dict:
    """Create a fresh call-tree node and stamp its start time.

    A node is the unit the whole tracer is built on. It starts with ``time``
    unset (``None``) — the wrapper fills it in with the measured wall duration
    when the call returns — and ``start`` set to the current ``perf_counter()``
    so overlap-aware wall math (:func:`_wall_from_intervals`) can later place it
    on the timeline (its end is ``start + time``). Input/output values
    (``args``, ``kwargs`` — only when non-empty —, ``result``, ``exception``)
    and aggregate fields (``cumulative``, ``count``) are attached lazily by
    other helpers and are absent here. Wall-clock timestamps live on the root
    only (see :func:`_now_iso`).

    Args:
        name: Display label for the node, e.g. ``"Class.method"`` or a
            ``module.function`` qualname.

    Returns:
        A dict with keys ``name``, ``time`` (``None``), ``start``, ``children``.
    """
    # `start` is re-stamped (and rounded) by _restart right before the call runs.
    return {"name": name, "time": None, "start": perf_counter(), "children": []}


def _finish_node(node: dict) -> None:
    """Stamp a node's wall ``time`` now that its call has returned."""
    node["time"] = round(perf_counter() - node["start"], _TIME_DECIMALS)


def _restart(node: dict) -> None:
    """Re-stamp a node's ``start`` right before the traced call actually begins.

    :func:`make_node` stamps ``start`` at node creation, but there is real work
    between that and the call itself — input capture in every node wrapper,
    plus installing the profiler hook for a ``trace()`` root. Left alone, it
    would be counted as part of the node's own time. Calling this right before
    invoking the wrapped function moves ``start`` to the true beginning of the
    call, so that overhead is excluded the same way output capture is excluded
    by :func:`_finish_node`.
    """
    node["start"] = round(perf_counter(), _TIME_DECIMALS)


# ── output tuning ──────────────────────────────────────────────────────────
# Tree nodes whose wall time is below this fraction of the root total are
# hidden, collapsed into one "… N hidden" line per parent. Default 0.0 shows
# everything; raise it (e.g. 0.01) when the tree gets too noisy.
MIN_PCT = 0.0
# Number of functions to list in the flat "hot spots" summary. 0 disables it.
HOT_SPOTS = 15
# Print the call tree + hot-spots table to stdout when a traced call ends.
# Set False for services that trace every request and only read the written
# (OUTPUT_DIR/OUTPUT_FILE) or sent (ENDPOINT_URL) traces: the report is then
# not even built.
PRINT_REPORT = True
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
# Blacklist of function names (same format as LOG_FUNCTIONS, e.g. "Class.method"
# or "module.function") to exclude from tracing entirely. Entries are matched
# with fnmatch, so glob patterns work too (e.g. "_*" or "Class._*" to skip
# leading-underscore names). A matched call runs with node creation suppressed
# for it AND for everything reached underneath it (tracked via _skip_depth) —
# nested calls under a skipped function are excluded automatically, without
# needing to be listed themselves. Unlike LOG_FUNCTIONS, this affects
# instrumentation itself (and therefore overhead and every output: stdout,
# file, ENDPOINT_URL), not just what's kept when writing the tree out. Empty
# (the default) disables it. For excluding data BY NAME rather than whole
# calls — e.g. redacting a field/kwarg/positional-arg named "_token" or
# "password" from captured input/output — see SKIP_VARIABLES below instead.
SKIP_FUNCTIONS: list = []
# Blacklist of variable/field names to exclude from captured input/output data
# (requires CAPTURE_IO). Matched with fnmatch against every dict key
# encountered while converting a value — kwargs names, an instance's __dict__
# field names (e.g. "self._token"), and nested dict keys anywhere inside a
# captured arg/kwarg/result/exception — so glob patterns work too (e.g. "_*"
# to drop every leading-underscore field, or "*password*", "*secret*"). A
# matched key's value is dropped entirely (not even converted) rather than
# replaced with a marker, so redacted data never touches perf.json/stdout/
# ENDPOINT_URL. Positional (non-keyword) call arguments are matched by their
# parameter name, resolved from the function's own signature — a positional
# arg whose name matches is captured as a "<skipped:name>" marker (its slot is
# kept so later positional args don't shift). Unlike SKIP_FUNCTIONS, this
# doesn't affect what's instrumented/timed, only what's captured. Empty (the
# default) disables it.
SKIP_VARIABLES: list = []
# Functions whose LOCAL VARIABLES are traced line by line (fnmatch patterns
# on node names, e.g. ["__main__.func", "Cart.*"]). Each matching call's node
# gets a "locals" list -- one entry per executed line that changed something,
# written positionally (0 = empty part; trailing empty parts are left out):
#   [line, set, changed, deleted, globals, deleted_globals]
# e.g. [12, {"i": 3}] or [14, 0, {"rows": [["+", [], [{"id": 3}]]]}]
# -- covering rebinding (a = 3) and in-place mutation (items.append(x),
# self.count += 1). `set` ({name: value}) holds full values. `changed`
# ({name: [op, ..]}) holds, for an object mutated in place, only the edits to
# its previous value: ["+", path, [items]] appends to the list at path,
# ["=", path, value] sets a key/attribute/index, ["-", path] deletes one
# (path: a list of keys and indices, [] = the variable itself). The previous
# value is the name's latest entry in this call -- or, for an argument with
# none yet, its value in the node's "args" (at its index in the node's
# "params", the positional parameter names) or "kwargs". `deleted`: names.
# `globals`: like `set`, for names the function assigns via `global`.
# Replaying a node's entries in order rebuilds every value:
# perf.expand_locals(trace) does it. Arguments aren't repeated (they're in
# "args"). Objects known only by a repr naming their address (a lock: <locked
# _thread.lock object at 0x…>) change only when rebound, not when their state
# words flip. The source
# text of each line is stored once per trace, in the output's top-level
# "sources" map -- {id: {"file": path, "lines": {"<line>": text}}} -- and the
# node names its file's id as "source": an entry's text is
# sources[node["source"]]["lines"][str(line)]. Costly:
# after every line of a matching function that could change an object in
# place (it calls something, stores an attribute/item, uses an in-place
# operator, iterates, or awaits/yields), its mutable locals are converted and
# compared, so scope it to the functions you are debugging. (After any other
# line only rebinding is checked: a change that a property or operator
# dunder makes there is recorded at the next such line instead.) Python 3.12+ (sys.monitoring line events);
# ignored with a warning on older versions. Empty (the default) disables it.
TRACE_LOCALS: list = []
# Cap on "locals" entries per call (a loop can produce one per iteration);
# beyond it, the rest are counted in "locals_dropped".
MAX_LOCALS_EVENTS = 1000

# Regular expressions whose matches are replaced by "<redacted>" in EVERY
# piece of captured text: string args/results/fields (at any depth, dict keys
# included), reprs, exception messages, and every line of a recorded
# traceback. Complements SKIP_VARIABLES, which can only drop values by NAME --
# e.g. a secret inside an exception message ("login failed for
# password=hunter2") needs a pattern like r"(?<=password=)\w+". Patterns run
# on the recorded TEXT, reprs included: a greedy \S+ would also swallow the
# closing quote of a repr like ValueError('...'). Empty (the
# default) disables it. An invalid pattern fails the trace before the traced
# function runs, rather than silently leaking what it was meant to hide.
REDACT_PATTERNS: list = []
# When set, the raw trace tree is POSTed here at the end of every traced call,
# wrapped as {"status", "tags", "trace"} (see _send_trace). None (the default)
# disables it — set it to opt in (e.g. "http://localhost:7999/api/traces").
ENDPOINT_URL: str | None = None
# Tags attached to every posted trace (e.g. ["prod", "search"]).
TAGS: list = ["dev"]
# Pre-shared key for collectors that require API-key auth on their ingestion
# route (sent as the X-API-Key header on every POST to ENDPOINT_URL). Never
# built in: set it here or through the PERF_API_KEY environment variable.
API_KEY: str | None = os.environ.get("PERF_API_KEY") or None
# Seconds a POST to ENDPOINT_URL may take. An error trace is sent before the
# failing call returns (see _send_trace), so this bounds how long a slow or
# unreachable collector can hold up a failing call.
SEND_TIMEOUT: float = 5.0
# Auto-terminate a traced call that runs longer than this many seconds. The
# async path cancels the coroutine (asyncio.wait_for); the sync path interrupts
# via SIGALRM (Unix main thread only — a background thread is left to run and a
# warning is printed). The partial tree collected so far is still printed on
# timeout. None disables the watchdog.
TIMEOUT: float | None = None

# When True, an exception raised by ANY traced call — the top-level function
# decorated with @trace, or any nested auto-instrumented call reached under
# it — is swallowed at the point it happened (after being recorded/sent like
# any other exception) instead of propagating further up the call stack. In
# its place, a default value derived from *that call's own* return annotation
# is returned (e.g. "-> list" yields [], "-> int" yields 0), so a deep failure
# degrades to an empty/zero result there rather than unwinding the whole
# chain. This means a bug anywhere in the traced tree can silently turn into
# an empty default instead of a loud crash — an explicit trade of correctness
# visibility for resilience. Default False preserves the old raise-through
# behaviour everywhere.
SUPPRESS_ERRORS = False
# Overrides the auto-detected default value used by SUPPRESS_ERRORS. Leave as
# _UNSET (the default) to derive the default from each call's own return
# annotation. If set to a callable, it's invoked with no args per suppressed
# call to produce a fresh value (e.g. DEFAULT_RETURN=list); otherwise the
# value is used as-is for every suppressed call, regardless of that call's own
# return type.
_UNSET = object()
DEFAULT_RETURN: Any = _UNSET


# ── auto-patch instances created during a trace session ───────────────────────

_patched_classes: set = set()
# Classes already judged non-project (or our own). The setprofile hook hits
# _patch_class on EVERY call during a trace — including millions of calls inside
# uninstrumented libraries (e.g. BeautifulSoup). Caching the negative verdict
# turns that hot path into one set lookup instead of a per-call filesystem path
# resolution, which otherwise inflates and destabilises CPU-bound self times.
_skip_classes: set = set()
# The (SCAN_VENV, _SCAN_INCLUDE_PREFIXES) the verdicts in _skip_classes were
# reached under: changing either between traces invalidates them (a class
# judged "library" before SCAN_VENV was switched on must be re-judged).
_skip_classes_config: tuple | None = None
# Serializes patching (module functions, classes): traces may start on several
# threads at once, and the profiler hook patches classes from any thread.
# Re-entrant because patching a class can trigger the hook again.
_patch_lock = threading.RLock()

# Maps id(original_function) -> (original_function, wrapped_function) for every
# free function wrapped by _patch_module_functions. A function patched in its
# defining module stays unwrapped under any name it was re-exported to via
# ``from module import func`` — that import copies the reference into the
# importing module's namespace *before* patching runs, so rebinding the name in
# the defining module never touches the importer's own binding. This map lets
# _patch_reexported_names find and fix up those other bindings afterwards. The
# tuple keeps the original alive so its id can't be recycled by an unrelated
# object while the map is in use.
_wrapped_originals: dict = {}

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
    "_pydev_bundle",
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
# Only capture a value whose size is below this limit. Size = characters of
# content: string/bytes length, the digits of each number, the repr of anything
# converted to a string -- not dict keys or JSON syntax, and characters rather
# than UTF-8 bytes. Enforced WHILE converting (see _jsonable's `_budget` param). A converted arg / kwarg
# / result at or above it is either spilled to SPILL_DIR (if set) or dropped
# and replaced with a compact "<TypeName>" marker, so the node still records
# that something flowed through without bloating perf.json. None (the
# default) disables the limit — capture values of any size.
MAX_SIZE_CAPTURE: int | None = None
# When set (a directory path), a value that exceeds MAX_SIZE_CAPTURE is written
# in full to its own JSON file in this directory instead of being dropped, and
# the node holds only a small {"__spilled__": path, "size": N} reference in
# its place. This preserves full fidelity for large values while keeping peak
# RAM bounded to roughly the size of the single value being spilled at any one
# time — that value's full conversion is written to disk and released
# immediately, not held in the node for the rest of the trace. Ignored when
# MAX_SIZE_CAPTURE is None (nothing is ever considered "too large"). None (the
# default) disables spilling — oversize values are dropped, as before.
#
# Every capture is a fresh snapshot of the value at that moment (so a mutated
# object never shows a stale state), but identical captured CONTENT — a
# service `self` that didn't change between calls, a shared config dict, a
# reused tokenizer's vocab, … — is stored only once per trace in the output's
# top-level "variables" map and referenced from the tree as {"__ref__": ref}
# (see _ValuePool). Automatic whenever CAPTURE_IO is on.
SPILL_DIR: str | None = None

# Generators (and async generators): how many of the values a generator
# yields are recorded on its node, as "yielded" (the total is always in
# "yields"). Each is captured like any other value, at the moment it is
# yielded. 0 records none.
MAX_YIELDS_CAPTURE = 10

# CAPTURE_IO for hot functions: record args/result (and generator yields) for
# only the first N calls of each function (by node name) in a trace. Every
# call still gets its node and timing, and exceptions are always recorded; a
# call past the limit carries "capture_skipped": true instead. Capture is most
# of a traced call's cost -- on real libraries (jinja2, sqlparse, pygments)
# capturing every call made traces 5-40x slower than this default -- so it is
# bounded by default. None captures every call.
MAX_CAPTURES_PER_FUNCTION: int | None = 20


# Exact types _jsonable returns unchanged (when not budget-limited) -- lets the
# container loops skip a recursive call per leaf.
_LEAF_TYPES = frozenset({str, int, float, bool, type(None)})
_NON_STR_LEAF_TYPES = frozenset({int, float, bool, type(None)})  # with REDACT_PATTERNS, strings must be scanned

# Returned by _jsonable for values with no useful serialization (objects whose
# repr is just the default "<pkg.Class object at 0x…>"). Skipped from the output.
_SKIP = object()

# Matches Python's default object repr, e.g.
#   <qdrant_client.http.api_client.BaseAsyncMiddleware object at 0x7fd8bc2d4d70>
_DEFAULT_REPR = re.compile(r"^<[\w.<>]+ object at 0x[0-9a-fA-F]+>$")  # <> : local classes (f.<locals>.C)
_ADDRESS_REPR = re.compile(r"^<.* at (0x[0-9a-fA-F]+)>$", re.S)  # any repr naming its object's address


# ── reusing an unchanged container's conversion across captures ─────────────
#
# A dict (str keys) / list / tuple whose values are all leaves converts to a
# pure function of those exact objects (and the redaction pattern), so while
# a container still holds the very same objects -- checked by identity, not
# equality -- its previous conversion is still exact, and so is the ref the
# pool gave it (see _ValuePool._intern). This is what makes an unchanged
# `self.config` cost one comparison per call instead of a walk + a digest.
# The snapshot holds the keys and values themselves, so none of them can be
# freed and another object take its address while the entry exists.

_REUSE_MIN = 16  # smaller containers convert faster than they are compared
_REUSE_MAX_ENTRIES = 4096  # per trace; the tables are emptied when full


def _py_leaf_items(value: Any, leaf_types: frozenset) -> "tuple | None":
    if type(value) is dict:
        items = tuple(value.items())  # one step: consistent even if another thread writes
        keys = tuple(k for k, _ in items)
        values = tuple(v for _, v in items)
        if all(type(k) is str for k in keys) and leaf_types.issuperset(map(type, values)):
            return keys, values
        return None
    values = tuple(value)
    return (None, values) if leaf_types.issuperset(map(type, values)) else None


def _py_same_items(value: Any, keys: "tuple | None", values: tuple) -> bool:
    if len(value) != len(values):
        return False
    if keys is None:
        return type(value) is not dict and all(map(operator.is_, value, values))
    return (type(value) is dict and all(map(operator.is_, value, keys))
            and all(map(operator.is_, value.values(), values)))


# getattr: an extension built from an older _speedups.c lacks them.
_leaf_items = getattr(_speedups, "leaf_items", None) or _py_leaf_items
_same_items = getattr(_speedups, "same_items", None) or _py_same_items


def _reused_conversion(state: "_TraceState", value: Any, rx: Any, leaf_types: frozenset) -> Any:
    """`value`'s conversion if it holds only leaves (else None): the previous
    one while it still holds the same objects, otherwise built from a fresh
    snapshot and remembered. Each entry: [keys, values, rx, converted,
    (pool, interned result) or None]."""
    entry = state.reuse.get(id(value))
    if entry is not None and entry[2] is rx and _same_items(value, entry[0], entry[1]):
        return entry[3]
    snap = _leaf_items(value, leaf_types)
    if snap is None:
        return None
    keys, values = snap
    if keys is None:
        out: Any = list(values)
    elif rx is None:
        out = dict(zip(keys, values))
    else:
        out = {rx.sub("<redacted>", k): v for k, v in zip(keys, values)}
    if len(state.reuse) >= _REUSE_MAX_ENTRIES:
        state.reuse.clear()
        state.reuse_by_out.clear()
    if entry is not None:
        state.reuse_by_out.pop(id(entry[3]), None)
    entry = [keys, values, rx, out, None]
    state.reuse[id(value)] = entry
    state.reuse_by_out[id(out)] = entry
    return out


def _jsonable(value: Any, depth: int = 0, _seen: frozenset = frozenset(), _budget: list | None = None,
              _memo: dict | None = None) -> Any:
    """Convert a value to a JSON-serializable form.

    dict/list/tuple/set are recursed into so their contents are preserved;
    primitives pass through; rich objects are unpacked. Objects with only a
    default repr (``<pkg.Class object at 0x…>``) carry no information and
    return the _SKIP sentinel so the caller can drop them.

    `_seen` tracks the ``id()`` of every container/object currently open on this
    recursion path (not a global visited-set, so two independent references to
    the same object are both still captured — only an actual cycle, where a
    value contains itself, is cut off). Live objects like a connection wrapping
    its own transport, which back-references the connection, are common enough
    that without this a single argument could recurse `MAX_DEPTH` layers deep
    re-expanding the same handful of objects repeatedly.

    `_budget`, when given, is a single-element ``[remaining_bytes]`` list shared
    (mutated in place, not copied per branch) across the whole recursion. It
    bounds how much this call is allowed to copy *while converting*, instead of
    building the full structure first and only checking its size afterward —
    which is what used to let one huge value blow up RAM even when
    MAX_SIZE_CAPTURE was set, since the full copy (plus a second full copy to
    measure it) happened before the size was ever checked. Strings/bytes are
    truncated at the exact point they'd exceed the budget; containers stop
    recursing into further siblings once it's gone. `_budget[0] <= 0`
    afterward means the value was at least budget-sized; the caller
    (:func:`_capture_value`) decides from there whether to spill or drop it.

    This is a pure snapshot: it never consults or fills the trace's
    :class:`_ValuePool`, so every call converts `value` exactly as it is at
    that moment. Deduplication happens afterwards, on the converted result
    (see :func:`_capture_value` / :meth:`_ValuePool.intern`).

    Within ONE conversion, `_memo` converts each shared container/object once
    and reuses the result wherever else it is referenced -- at other depths
    too, as long as the reused subtree stays clear of MAX_DEPTH there: an
    object graph where the same sub-objects are reachable along many paths
    would otherwise be re-expanded along every path -- exponential in
    MAX_DEPTH. (Not with a size budget: there every occurrence must be charged.)

    A dict holding nothing but functions (a registry: jinja's filters, a
    dispatch table) becomes ``{"__functions__": [its keys]}`` -- which names
    are registered is the data; one ``{__type__: function, …}`` description
    per entry was most of the cost of capturing any object that reaches one.
    Swapping one function for another under the same key is not visible.

    Only objects of PROJECT classes (see _is_project_module; SCAN_VENV opts
    libraries in) are expanded field by field, plus dataclasses and pydantic
    models from anywhere. A library object (an engine, a client, a session…)
    is its repr when it has one, else a "<Type>" marker: its internals are
    not the program's data, and walking them reaches modules, registries and
    whole connection pools. Modules are never expanded.
    """
    if _memo is None:
        _memo = {"circular": 0}
    if depth == 0 and "max_depth" not in _memo:
        # Settings read once per conversion, not at every step (each `_cfg`
        # read is a context-var lookup).
        _memo["max_depth"] = _cfg.MAX_DEPTH
        _memo["rx"] = _redactor()
        _memo["skip"] = bool(_cfg.SKIP_VARIABLES)
        state = _current_trace.get()
        _memo["reuse"] = state if _budget is None and state is not None and not _memo["skip"] else None
    if value is None or isinstance(value, (bool, int, float)):
        if _budget is not None:
            # Numbers count too: a huge list of ints must hit MAX_SIZE_CAPTURE
            # just like a huge list of strings does.
            _budget[0] -= len(repr(value))
        return value
    if isinstance(value, str):
        if _memo["rx"] is not None:
            value = _memo["rx"].sub("<redacted>", value)
        if _budget is not None:
            if len(value) >= _budget[0]:
                truncated = value[: max(_budget[0], 0)]
                _budget[0] = 0
                return truncated
            _budget[0] -= len(value)
        return value
    if isinstance(value, (bytes, bytearray)):
        if _budget is not None:
            if len(value) >= _budget[0]:
                n = max(_budget[0], 0)
                _budget[0] = 0
                return _safe_repr(bytes(value[:n]))
            _budget[0] -= len(value)
        return _safe_repr(value)
    max_depth = _memo["max_depth"]
    if depth > _memo.get("deepest", 0):
        _memo["deepest"] = depth
    if depth >= max_depth:
        return _charge(_safe_repr(value), _budget)
    if id(value) in _seen:
        _memo["circular"] += 1
        return _charge(f"<circular {type(value).__name__}>", _budget)
    if _budget is not None and _budget[0] <= 0:
        return f"<{type(value).__name__} truncated: size limit reached>"
    if _budget is None:
        entry = _memo.get(id(value))
        # Reused at its own depth, or at any depth where neither it nor this
        # occurrence reaches MAX_DEPTH: a subtree `height` levels deep that
        # was never cut converts the same wherever it sits above the limit.
        # (A hub object -- a jinja Environment every AST node points to -- is
        # otherwise re-expanded once per depth it is reached at.)
        if entry is not None and entry[0] is value and (
                entry[2] == depth or (entry[2] + entry[3] < max_depth and depth + entry[3] < max_depth)):
            if depth + entry[3] > _memo.get("deepest", 0):
                _memo["deepest"] = depth + entry[3]
            return entry[1]
        circular_before = _memo["circular"]
        deepest_before = _memo.get("deepest", 0)
        _memo["deepest"] = depth
        converted = _jsonable_uncached(value, depth, _seen, _budget, _memo)
        height = _memo["deepest"] - depth
        _memo["deepest"] = max(deepest_before, _memo["deepest"])
        # A subtree that cut a cycle depends on the path it was reached by:
        # reusing it elsewhere could show "<circular>" where there is none.
        # The value itself is kept in the entry so its id can't be reused by
        # another (temporary) object during this conversion.
        if _memo["circular"] == circular_before:
            _memo[id(value)] = (value, converted, depth, height)
        return converted
    return _jsonable_uncached(value, depth, _seen, _budget, _memo)


def _marker(value: Any) -> str:
    """Stand-in for a value with nothing to show but its type (default repr),
    kept in its container's place: ``"<TypeName>"``, as for a whole argument."""
    return f"<{type(value).__name__}>"


def _jsonable_uncached(value: Any, depth: int, _seen: frozenset, _budget: list | None, _memo: dict) -> Any:
    """Containers and objects (see _jsonable)."""
    try:
        # Compact, unambiguous forms for values whose generic conversion is
        # noise (a class's whole __dict__) or loses meaning (NamedTuple fields).
        if isinstance(value, type):
            return {"__class__": _charge(f"{value.__module__}.{value.__qualname__}", _budget)}
        if isinstance(value, types.ModuleType):
            return {"__module__": _charge(value.__name__, _budget)}
        if isinstance(value, enum.Enum):
            return {
                "__enum__": _charge(f"{type(value).__qualname__}.{value.name}", _budget),
                "value": _jsonable(value.value, depth + 1, _seen | {id(value)}, _budget, _memo),
            }
        if isinstance(value, tuple) and isinstance(getattr(type(value), "_fields", None), tuple):
            seen = _seen | {id(value)}
            fields = {"__type__": type(value).__name__}
            for field_name, field_value in zip(type(value)._fields, value):
                jv = _jsonable(field_value, depth + 1, seen, _budget, _memo)
                fields[field_name] = _marker(field_value) if jv is _SKIP else jv
            return fields
        if isinstance(value, (dict, types.MappingProxyType)):
            seen = _seen | {id(value)}
            out = {}
            skip_keys = _memo["skip"]
            rx = _memo["rx"]
            if len(value) >= _REGISTRY_MIN and all(map(_is_routine, value.values())):  # see _jsonable
                return {"__functions__": [
                    _charge((rx.sub("<redacted>", k) if rx else k) if isinstance(k, str) else _safe_repr(k), _budget)
                    for k in value if not (skip_keys and isinstance(k, str) and _is_skipped_variable(k))]}
            leaf_types = _NON_STR_LEAF_TYPES if rx else _LEAF_TYPES
            reuse = _memo.get("reuse") if type(value) is dict and len(value) >= _REUSE_MIN else None
            if reuse is not None:
                reused = _reused_conversion(reuse, value, rx, leaf_types)
                if reused is not None:
                    return reused
            for k, v in value.items():
                if skip_keys and isinstance(k, str) and _is_skipped_variable(k):
                    continue
                key = (rx.sub("<redacted>", k) if rx else k) if isinstance(k, str) else _safe_repr(k)
                if _budget is None:
                    if type(v) in leaf_types:  # fast path: same result as recursing
                        out[key] = v
                        continue
                elif _budget[0] <= 0:
                    break
                jv = _jsonable(v, depth + 1, seen, _budget, _memo)
                out[key] = _marker(v) if jv is _SKIP else jv
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            seen = _seen | {id(value)}
            out = []
            leaf_types = _NON_STR_LEAF_TYPES if _memo["rx"] is not None else _LEAF_TYPES
            reuse = _memo.get("reuse") if (type(value) is list or type(value) is tuple) and len(value) >= _REUSE_MIN else None
            if reuse is not None:
                reused = _reused_conversion(reuse, value, _memo["rx"], leaf_types)
                if reused is not None:
                    return reused
            for v in value:
                if _budget is None:
                    if type(v) in leaf_types:  # fast path: same result as recursing
                        out.append(v)
                        continue
                elif _budget[0] <= 0:
                    break
                jv = _jsonable(v, depth + 1, seen, _budget, _memo)
                # Keep a marker in the element's place: dropping it would
                # shift every later index and change len() of the capture.
                out.append(_marker(v) if jv is _SKIP else jv)
            return out
        # pydantic models, dataclasses, and other rich objects. exclude_none
        # drops fields that are unset (Optional[...] = None) rather than
        # carrying an explicit null for every one of them -- schemas like a
        # BaseModel with a couple dozen optional fields (e.g. most of them
        # None on any given instance) would otherwise dump every single field
        # name every single time the model flows through a traced call,
        # which is what actually blows up perf.json's size, not depth.
        # Type-level checks: never call a `.dict()` / `.model_dump()` that merely
        # happens to exist on some user object, and never trigger instance
        # __getattr__ / properties just to probe for them.
        vtype = type(value)
        if hasattr(vtype, "model_fields") and callable(getattr(vtype, "model_dump", None)):  # pydantic v2
            return _jsonable(value.model_dump(exclude_none=True), depth + 1, _seen | {id(value)}, _budget, _memo)
        if hasattr(vtype, "__fields__") and callable(getattr(vtype, "dict", None)):  # pydantic v1
            return _jsonable(value.dict(exclude_none=True), depth + 1, _seen | {id(value)}, _budget, _memo)
        # functions/methods/builtins: no useful __dict__ to unpack (and a
        # bound method's __func__ may carry unrelated noise like __wrapped__
        # from being auto-instrumented) -- describe identity instead of
        # falling through to a raw "<bound method ... at 0x...>" repr.
        if _is_routine(value):
            is_method = inspect.ismethod(value)
            func = value.__func__ if is_method else value
            info: dict = {
                "__type__": "method" if is_method else "function",
                "qualname": getattr(func, "__qualname__", getattr(func, "__name__", None)),
                "module": getattr(func, "__module__", None),
            }
            if is_method:
                info["self_type"] = type(value.__self__).__name__
            return info
        # Slotted objects: expand their slots -- but only when the class has no
        # repr of its own (UUID, Path, … define a readable one; their slots
        # are private, version-specific internals).
        if not _expandable(vtype) and not dataclasses.is_dataclass(value):
            raise _NotExpanded  # a library object: its repr, or a <Type> marker (below)
        slot_names = _slot_names(type(value)) if type(value).__repr__ is object.__repr__ else ()
        if slot_names:
            fields = {"__type__": type(value).__name__}
            if hasattr(value, "__dict__"):
                more = _jsonable(vars(value), depth, _seen | {id(value)}, _budget, _memo)
                if isinstance(more, dict):
                    fields.update(more)
            seen = _seen | {id(value)}
            for slot in slot_names:
                if _is_skipped_variable(slot):
                    continue
                try:
                    slot_value = getattr(value, slot)
                except AttributeError:
                    continue  # slot never assigned
                jv = _jsonable(slot_value, depth + 1, seen, _budget, _memo)
                fields[slot] = _marker(slot_value) if jv is _SKIP else jv
            if len(fields) > 1:
                return fields
        if hasattr(value, "__dict__") and vars(value):
            # The object and its __dict__ are ONE level: converting the
            # __dict__ at the object's own depth puts the fields at depth + 1,
            # exactly like a dict's values. (Counting the __dict__ as an extra
            # level made an object just above MAX_DEPTH come out as
            # "<unconvertible …>", since the cut-off __dict__ was a string.)
            fields = _jsonable(vars(value), depth, _seen | {id(value)}, _budget, _memo)
            if not isinstance(fields, dict):
                return fields
            return {"__type__": type(value).__name__, **fields}
    except _NotExpanded:
        pass
    except Exception as e:
        return f"<unconvertible {type(value).__name__}: {e}>"
    r = _safe_repr(value)
    if _DEFAULT_REPR.match(r):
        return _SKIP
    if _comparing.get():
        m = _ADDRESS_REPR.match(r)
        if m:
            _memo["shortened"] = _memo.get("shortened", 0) + 1
            # TRACE_LOCALS change detection: a repr that names its object's
            # address (<locked _thread.lock object at 0x…>) is that object's
            # identity, not its data -- its state words flipping on every
            # `with lock:` must not count as a change of the variable.
            return f"<{type(value).__qualname__} at {m.group(1)}>"
    return _charge(r, _budget)


_REGISTRY_MIN = 4  # a dict of at least this many functions and nothing else is a registry (see _jsonable)
_routine_types: dict = {}  # type -> inspect.isroutine verdict


def _is_routine(value: Any) -> bool:
    """inspect.isroutine, cached per type: its answer depends only on the
    type, and it chains five checks -- run on every captured object and every
    value of a dict that might be a function registry."""
    verdict = _routine_types.get(type(value))
    if verdict is None:
        verdict = _routine_types[type(value)] = inspect.isroutine(value) or _is_function(value)
    return verdict



class _NotExpanded(Exception):
    pass


_expandable_cache: dict = {}  # class -> bool; cleared when the scan config changes


def _expandable(cls: type) -> bool:
    """Whether instances of `cls` are expanded field by field: classes from
    project code only (the same rule as instrumentation)."""
    verdict = _expandable_cache.get(cls)
    if verdict is None:
        module = sys.modules.get(getattr(cls, "__module__", None) or "")
        verdict = module is not None and module.__name__ != __name__ and _is_project_module(module)
        _expandable_cache[cls] = verdict
    return verdict


def _charge(text: str, budget: list | None) -> str:
    """Counts a string produced during conversion (a repr, a marker) against
    the MAX_SIZE_CAPTURE budget, truncating it where the budget runs out --
    every string that ends up in the output is charged, not just the ones the
    value itself contained."""
    if budget is None:
        return text
    if len(text) >= budget[0]:
        truncated = text[: max(budget[0], 0)]
        budget[0] = 0
        return truncated
    budget[0] -= len(text)
    return text


_slot_cache: dict = {}


def _slot_names(cls: type) -> tuple:
    """Data slots declared through the class hierarchy (``__slots__``), in
    declaration order — the fields of an object that has no (or not only a)
    ``__dict__``."""
    cached = _slot_cache.get(cls)
    if cached is not None:
        return cached
    names: list = []
    try:
        for klass in reversed(cls.__mro__):
            slots = vars(klass).get("__slots__", ())
            for name in (slots,) if isinstance(slots, str) else slots:
                if name not in ("__dict__", "__weakref__") and name not in names:
                    names.append(name)
    except Exception:
        names = []
    result = tuple(names)
    _slot_cache[cls] = result
    return result


def _spill_value(value: Any) -> Any:
    """Writes the full conversion of `value` to its own file under
    :data:`SPILL_DIR`, and returns a small reference in its place.

    Only called once :func:`_capture_value`'s budgeted pass already knows
    `value` exceeds :data:`MAX_SIZE_CAPTURE` — so the one unbounded conversion
    this does is paid for exactly one value, written to disk, and released
    (``del full``) immediately, rather than staying resident in the trace tree
    for the rest of the call the way every other captured value does. Falls
    back to dropping the value (_SKIP) if writing fails for any reason —
    telemetry must never break the traced call.
    """
    import json
    import uuid

    try:
        full = _jsonable(value)
        Path(_cfg.SPILL_DIR).mkdir(parents=True, exist_ok=True)
        path = Path(_cfg.SPILL_DIR) / f"{uuid.uuid4()}.json"
        with open(path, "w") as f:
            json.dump(full, f, ensure_ascii=False, default=str)
        size = path.stat().st_size
        del full
        return {"__spilled__": str(path), "size": size}
    except Exception as e:  # never let telemetry break the traced call
        print(f"[perf] failed to spill oversized value to {_cfg.SPILL_DIR}: {e}", file=sys.stderr)
        return _SKIP


def _capture_value(value: Any) -> Any:
    """Convert `value` for capture, spilling or dropping it if too large.

    Returns the JSON-able form from :func:`_jsonable`, unless it is the
    ``_SKIP`` sentinel (no useful serialization). When :data:`MAX_SIZE_CAPTURE`
    is set, conversion happens under a byte budget (see `_jsonable`'s
    `_budget` param), so a huge value is never fully copied into RAM just to
    be measured and discarded — unlike checking its size only after a full,
    unbounded conversion. A value that exhausts the budget is spilled to its
    own file (:func:`_spill_value`) when :data:`SPILL_DIR` is set, or dropped
    — the same "<TypeName>" marker behaviour as when SPILL_DIR is left unset.

    Only a value that is kept inline is interned into the trace's pool, and
    only after its conversion finished — a budget-truncated partial
    conversion never reaches the pool, and a spilled value's file holds its
    full content inline, never a ref into a pool that isn't written with it.
    """
    jv, inline = _capture_value_inline(value)
    return _intern(jv) if inline else jv


def _capture_value_inline(value: Any) -> tuple:
    """:func:`_capture_value` before interning: (converted form, whether it is
    the value's full content -- False for a spill reference or a dropped value)."""
    if _cfg.MAX_SIZE_CAPTURE is None:
        jv = _jsonable(value)
        return jv, jv is not _SKIP
    budget = [_cfg.MAX_SIZE_CAPTURE]
    jv = _jsonable(value, _budget=budget)
    if jv is _SKIP:
        return _SKIP, False
    if budget[0] > 0:
        return jv, True
    if _cfg.SPILL_DIR:
        return _spill_value(value), False
    return _SKIP, False


def _intern(converted: Any) -> Any:
    """Dedups a freshly converted value through the current trace's pool (if any)."""
    pool = _current_pool.get()
    if pool is None or converted is _SKIP:
        return converted
    return pool.intern(converted)


def _safe_repr(value: Any) -> str:
    try:
        text = repr(value)
    except Exception as e:  # a __repr__ that raises must not kill the trace
        text = f"<unreprable {type(value).__name__}: {e}>"
    return _redact(text)


_redactor_cache: list = [None, None]  # [patterns tuple, compiled regex or None]


def _redactor() -> Any:
    """The compiled REDACT_PATTERNS (one alternation), or None if unset."""
    patterns = tuple(_cfg.REDACT_PATTERNS or ())
    if _redactor_cache[0] != patterns:
        compiled = re.compile("|".join(f"(?:{pattern})" for pattern in patterns)) if patterns else None
        _redactor_cache[:] = [patterns, compiled]
    return _redactor_cache[1]


def _redact(text: Any) -> Any:
    rx = _redactor()
    if rx is None or not isinstance(text, str):
        return text
    return rx.sub("<redacted>", text)


# ── per-trace value pool: dedup repeated captures by content ────────────────


# _ValuePool serializes content exactly as json.dumps(value, ensure_ascii=False,
# separators=(",", ":"), default=str) would -- one reusable encoder (json.dumps
# builds a new one per call for non-default arguments), plus the same leaf
# encodings json uses, so containers can be assembled from their parts.
_store_encoder = _json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), default=str)
_encode_str = _json.encoder.encode_basestring
_INF = float("inf")


def _leaf_json(v: Any) -> "str | None":
    """JSON text of a str/int/float/bool/None as json.dumps writes it, else None."""
    t = type(v)
    if t is str:
        return _encode_str(v)
    if t is int:
        return int.__repr__(v)
    if v is None:
        return "null"
    if t is bool:
        return "true" if v else "false"
    if t is float:
        if v != v:
            return "NaN"
        if v == _INF:
            return "Infinity"
        if v == -_INF:
            return "-Infinity"
        return float.__repr__(v)
    return None


class _ValuePool:
    """Stores each distinct piece of captured *content* once per trace.

    Values are deduplicated by what they converted to, never by the identity
    of the Python object they came from: identity-based dedup can't tell that
    an object was mutated between two captures, so it would show a later call
    a stale snapshot (a `self` whose fields changed, a list appended to). Here
    every capture is a fresh conversion, and only byte-identical content is
    shared: the same unchanged tokenizer vocab reached at 13 call depths is
    still stored once, but two different states of one object are two
    entries.

    :meth:`intern` walks a converted value bottom-up and replaces every
    container (and long string) whose serialized form is at least
    :attr:`MIN_SIZE` bytes with ``{"__ref__": ref}``, where
    ``variables[ref]`` holds that content (itself possibly containing refs).
    Smaller content stays inline, where a ref would cost more than it saves.
    Refs are keyed by a BLAKE2b-256 digest of the serialized content, so two
    different contents can't share an entry.

    One instance lives for the duration of a single top-level :func:`trace`
    call (see `_current_pool`) — never shared across traces, so one trace's
    pooled values can't leak into another's. Thread-safe: work handed to
    other threads with a copied context (``asyncio.to_thread``) interns into
    the same pool.
    """

    MIN_SIZE = 64

    def __init__(self, prefix: str = "", state: "_TraceState | None" = None) -> None:
        # `prefix` namespaces this pool's refs -- a worker process's pool
        # (see _RemoteCall) gets a unique one so its variables can be merged
        # into the parent trace's pool without colliding with "v0", "v1", ….
        self.variables: dict[str, Any] = {}
        self._ref_by_digest: dict[bytes, str] = {}
        # Shares the trace's lock + open flag: once the trace closes (and its
        # variables are being written out), late captures stay inline instead.
        self._state = state if state is not None else _TraceState()
        self._lock = self._state.lock
        self._prefix = prefix
        self._next = 0
        # Shortcuts past serialize + digest + lock for content already pooled
        # (an unchanged `self` re-captured at every call): a long string by
        # itself (immutable, and its hash is cached on the object), a large
        # container of str/int/None values by its contents. Only results that
        # really are refs are remembered, so both stay valid for the trace.
        self._ref_by_str: dict[str, tuple] = {}
        self._ref_by_leaves: dict[tuple, tuple] = {}

    def intern(self, value: Any, _memo: dict | None = None) -> Any:
        # A converted value can share sub-structures (_jsonable reuses the
        # conversion of an object referenced from many places): intern each
        # shared dict/list once per call, or a DAG is walked as a tree.
        return self._intern(value, {} if _memo is None else _memo)[0]

    # A container with more plain (leaf) values than this is serialized by the
    # C encoder in one call; a smaller one is assembled from its children's
    # serialized forms, which beats a C call per container (lists of small
    # records). Either way nothing is serialized once per nesting level.
    COMPOSE_MAX_LEAVES = 6

    # Containers with at least this many entries, all of an exact type in
    # _LEAF_KEY_TYPES, are looked up by their contents before being walked.
    # bool and float are left out so equal-but-different values (1 / True /
    # 1.0, 0.0 / -0.0) never share a key; such containers take the long way.
    LEAF_KEY_MIN = 16
    _LEAF_KEY_TYPES = frozenset({str, int, type(None)})

    def _leaf_key(self, value: Any) -> "tuple | None":
        """Content key of a large container of str/int/None values, else None."""
        if type(value) is dict:
            values = tuple(value.values())
            if self._LEAF_KEY_TYPES.issuperset(map(type, values)) and all(type(k) is str for k in value):
                return (tuple(value), values)
        elif type(value) is list:
            values = tuple(value)
            if self._LEAF_KEY_TYPES.issuperset(map(type, values)):
                return (None, values)
        return None

    def _intern(self, value: Any, memo: dict) -> tuple:
        """(interned value, its serialized form or None if unknown)."""
        if isinstance(value, (dict, list)):
            done = memo.get(id(value))
            if done is not None:
                return done
            # A conversion reused while its container is unchanged (see
            # _reused_conversion) carries the result it was interned to.
            reused = self._state.reuse_by_out.get(id(value))
            if reused is not None and reused[3] is value:
                done = reused[4]
                if done is not None and done[0] is self:
                    memo[id(value)] = done[1]
                    return done[1]
            else:
                reused = None
            key = self._leaf_key(value) if len(value) >= self.LEAF_KEY_MIN else None
            if key is not None:
                done = self._ref_by_leaves.get(key)
                if done is not None:
                    memo[id(value)] = done
                    return done
            original = value
            leaves = 0
            composable = True
            if isinstance(value, dict):
                out: Any = {}
                sers: Any = {}
                for k, v in value.items():
                    if self._is_small_leaf(v):
                        out[k] = v
                        leaves += 1
                    else:
                        out[k], sv = self._intern(v, memo)
                        sers[k] = sv
                        composable = composable and sv is not None
                    composable = composable and type(k) is str
                serialized = None
                if composable and leaves <= self.COMPOSE_MAX_LEAVES:
                    serialized = "{" + ",".join(
                        _encode_str(k) + ":" + (sers[k] if k in sers else _leaf_json(v)) for k, v in out.items()
                    ) + "}"
            else:
                out = []
                sers = []
                for v in value:
                    if self._is_small_leaf(v):
                        out.append(v)
                        sers.append(None)
                        leaves += 1
                    else:
                        rv, sv = self._intern(v, memo)
                        out.append(rv)
                        sers.append(sv)
                        composable = composable and sv is not None
                serialized = None
                if composable and leaves <= self.COMPOSE_MAX_LEAVES:
                    serialized = "[" + ",".join(_leaf_json(v) if sv is None else sv for v, sv in zip(out, sers)) + "]"
            result = self._store(out, serialized)
            memo[id(original)] = result
            if type(result[0]) is dict and "__ref__" in result[0]:
                if key is not None:
                    self._ref_by_leaves[key] = result
                if reused is not None:
                    reused[4] = (self, result)
            return result
        if not isinstance(value, str) or len(value) < self.MIN_SIZE:
            return value, _leaf_json(value)
        if type(value) is not str:
            return self._store(value)
        done = self._ref_by_str.get(value)
        if done is not None:
            return done
        result = self._store(value, _encode_str(value))
        if type(result[0]) is dict:  # pooled, not left inline by a closed trace
            self._ref_by_str[value] = result
        return result

    def _store(self, value: Any, serialized: "str | None" = None) -> tuple:
        if serialized is None:
            serialized = _store_encoder.encode(value)
        if len(serialized) < self.MIN_SIZE:
            return value, serialized
        digest = hashlib.blake2b(serialized.encode("utf-8", "surrogatepass"), digest_size=32).digest()
        with self._lock:
            if not self._state.open:
                return value, serialized
            ref = self._ref_by_digest.get(digest)
            if ref is None:
                ref = f"{self._prefix}v{self._next}"
                self._next += 1
                self._ref_by_digest[digest] = ref
                self.variables[ref] = value
        return {"__ref__": ref}, '{"__ref__":' + _encode_str(ref) + "}"

    @classmethod
    def _is_small_leaf(cls, v: Any) -> bool:
        t = type(v)
        return t is int or t is float or t is bool or v is None or (t is str and len(v) < cls.MIN_SIZE)


# ── SUPPRESS_ERRORS: type-aware default return values ─────────────────────────

_BUILTIN_EMPTY: dict = {
    list: list,
    dict: dict,
    tuple: tuple,
    set: set,
    frozenset: frozenset,
    str: str,
    bytes: bytes,
    int: int,
    float: float,
    bool: lambda: False,
}


def _dataclass_default(annotation: Any) -> Any:
    """Best-effort-construct a dataclass instance, deriving required fields recursively."""
    kwargs = {}
    for field in dataclasses.fields(annotation):
        if field.default is not dataclasses.MISSING:
            kwargs[field.name] = field.default
        elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            kwargs[field.name] = field.default_factory()
        else:
            kwargs[field.name] = _default_factory_for_annotation(field.type)()
    return annotation(**kwargs)


def _pydantic_v2_default(annotation: Any) -> Any:
    """Best-effort-construct a pydantic v2 model, deriving required fields recursively."""
    kwargs = {}
    for name, field_info in annotation.model_fields.items():
        default = field_info.default
        if type(default).__name__ != "PydanticUndefinedType":
            kwargs[name] = default
        elif field_info.default_factory is not None:
            kwargs[name] = field_info.default_factory()
        else:
            kwargs[name] = _default_factory_for_annotation(field_info.annotation)()
    try:
        return annotation(**kwargs)
    except Exception:
        return annotation.model_construct(**kwargs)


def _pydantic_v1_default(annotation: Any) -> Any:
    """Best-effort-construct a pydantic v1 model, deriving required fields recursively."""
    kwargs = {}
    for name, field in annotation.__fields__.items():
        if field.default is not None:
            kwargs[name] = field.default
        elif field.default_factory is not None:
            kwargs[name] = field.default_factory()
        elif not field.required:
            kwargs[name] = None
        else:
            kwargs[name] = _default_factory_for_annotation(field.outer_type_)()
    try:
        return annotation(**kwargs)
    except Exception:
        return annotation.construct(**kwargs)


def _default_factory_for_annotation(annotation: Any) -> Any:
    """Return a zero-arg callable that produces a sensible "empty" value for `annotation`.

    Used by SUPPRESS_ERRORS to synthesize a fallback return value when the
    traced function raises, so the caller's flow isn't broken. Handles bare
    builtins (list/dict/tuple/set/str/int/float/bool/bytes), their generic
    aliases (``list[int]``, ``Dict[str, int]``, ...), ``Optional[X]``/``X |
    None``, dataclasses, and pydantic v1/v2 models (duck-typed — no pydantic
    import, keeps the package zero-dependency) — recursively deriving values
    for required fields from their own annotations. Anything unresolvable
    falls back to ``None``, with a one-line stderr warning, mirroring the
    "never let telemetry break the traced call" rule used elsewhere.
    """
    if annotation is inspect.Signature.empty or annotation is None or annotation is type(None):
        return lambda: None
    if annotation is typing.Any:
        return lambda: None

    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _default_factory_for_annotation(args[0])
        return lambda: None
    if origin is not None:
        # a generic alias like list[int] / Dict[str, int] — dispatch on the runtime origin
        return _default_factory_for_annotation(origin)

    if annotation in _BUILTIN_EMPTY:
        return _BUILTIN_EMPTY[annotation]

    if isinstance(annotation, type):
        if dataclasses.is_dataclass(annotation):
            builder = _dataclass_default
        elif hasattr(annotation, "model_fields"):
            builder = _pydantic_v2_default
        elif hasattr(annotation, "__fields__"):
            builder = _pydantic_v1_default
        else:
            builder = lambda a: a()  # plain no-arg construction

        def _construct() -> Any:
            try:
                return builder(annotation)
            except Exception as e:
                print(
                    f"[perf] SUPPRESS_ERRORS: could not build default for {annotation!r}: {e}",
                    file=sys.stderr,
                )
                return None

        return _construct

    return lambda: None


def _default_return_unrecorded(default_factory: Any) -> Any:
    """:func:`_compute_default_return` as perf's own work: constructing the
    fallback value (which may run project constructors) is not recorded."""
    token = _capturing.set(_capturing.get() + 1)
    try:
        return _compute_default_return(default_factory)
    finally:
        _capturing.reset(token)


def _compute_default_return(default_factory: Any) -> Any:
    """Resolve the value to substitute for a suppressed root-level exception.

    Honors a per-call ``DEFAULT_RETURN`` override (a plain value, used as-is,
    or a callable, invoked fresh each time) over the auto-detected
    `default_factory` derived from the function's return annotation.
    """
    if _cfg.DEFAULT_RETURN is not _UNSET:
        return _cfg.DEFAULT_RETURN() if callable(_cfg.DEFAULT_RETURN) else _cfg.DEFAULT_RETURN
    return default_factory()


def _resolve_return_annotation(func: Any) -> Any:
    """Resolve `func`'s return annotation to an actual type/generic, not a string.

    ``inspect.signature(func).return_annotation`` returns the annotation
    exactly as written — under ``from __future__ import annotations`` (or any
    manually-quoted annotation, e.g. ``-> "list"``), that's a plain string
    like ``"list"``, which :func:`_default_factory_for_annotation` can't
    dispatch on (it would silently fall back to ``None``). ``typing.get_type_hints``
    evaluates such strings in the function's own module namespace, resolving
    forward refs the same way type checkers do. Falls back to the raw
    (possibly still-a-string) signature annotation if that evaluation fails
    (e.g. a forward ref to a name not in scope), and to "no annotation" if
    even that isn't available.
    """
    try:
        return typing.get_type_hints(func).get("return", inspect.Signature.empty)
    except Exception:
        pass
    try:
        return inspect.signature(func).return_annotation
    except (ValueError, TypeError):
        return inspect.Signature.empty


def _resolve_param_names(func: Any) -> tuple:
    """Resolve `func`'s positional parameter names, in call order, for :data:`SKIP_VARIABLES`.

    Used by :func:`_sync_wrap`/:func:`_async_wrap` to precompute (once per
    wrap, not per call) the names :func:`_record_inputs` matches against
    positional call arguments — e.g. so a method's ``self`` or a parameter
    named ``_token`` can be redacted by name the same way a kwarg is.
    ``VAR_POSITIONAL``/``VAR_KEYWORD``/keyword-only params are excluded since
    they either don't correspond to a single positional slot or never appear
    in `args`. Falls back to an empty tuple if the signature can't be read
    (e.g. some C-defined callables) — SKIP_VARIABLES then simply can't match
    positional args for that callable, same as before this existed.
    """
    try:
        params = inspect.signature(func).parameters.values()
    except (ValueError, TypeError):
        return ()
    return tuple(
        p.name
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )


def _capture_positional(a: Any) -> Any:
    """Capture one positional argument.

    Dedup of repeated content (e.g. an unchanged `self` passed down through
    nested calls) happens automatically in :func:`_capture_value` via the
    trace's :class:`_ValuePool` — nothing extra to do here.
    """
    jv = _capture_value(a)
    return f"<{type(a).__name__}>" if jv is _SKIP else jv


def _record_inputs(node: dict, args: tuple, kwargs: dict, param_names: tuple = ()) -> None:
    """Attach a JSON-safe snapshot of a call's arguments to its node.

    No-op unless :data:`CAPTURE_IO` is on. Positional args keep their index in
    ``node["args"]``; a value with no useful serialization (the ``_SKIP``
    sentinel from :func:`_jsonable`) is replaced by a compact ``"<TypeName>"``
    marker so the slot is preserved. Keyword args go to ``node["kwargs"]`` (omitted when there are none),
    where skipped values are dropped entirely rather than marked.

    :data:`SKIP_VARIABLES` redacts by name: a keyword arg whose name matches
    is dropped from ``node["kwargs"]`` entirely (never even converted). A
    positional arg's name is looked up via `param_names` (the wrapped
    function's own signature, resolved once per wrap — see
    :func:`_sync_wrap`/:func:`_async_wrap`); a match replaces that slot with a
    ``"<skipped:name>"`` marker instead of omitting it, since positional args
    are order-sensitive and omitting one would shift every later arg's
    apparent index.

    Also accumulates onto ``node["capture_time"]`` — the wall time this call
    spent capturing (as opposed to running). This node's own ``time`` (stamped
    by :func:`_finish_node` before capture ever runs) never includes it, but a
    PARENT's ``time`` does: the wrapper around this call doesn't return
    control to its caller until capture finishes too. Recording it here is
    what lets :func:`_self_time` subtract it back out of the parent's self
    time instead of that parent's report silently absorbing time actually
    spent capturing a child's args/result.

    Args:
        node: The call-tree node to annotate in place.
        args: The positional arguments the traced function was called with.
        kwargs: The keyword arguments the traced function was called with.
        param_names: The wrapped function's positional parameter names, in
            order (as many as could be resolved), used only to match
            SKIP_VARIABLES against positional args by name.
    """
    if not _cfg.CAPTURE_IO:
        return
    limit = _cfg.MAX_CAPTURES_PER_FUNCTION
    if limit is not None:
        state = _current_trace.get()
        if state is not None and not state.capture_allowed(node["name"], limit):
            node["capture_skipped"] = True
            return
    # Perf's own capture: user code it triggers (a __repr__, a property) is
    # not recorded as calls (see _capturing). Set once per call, inline (a
    # context manager here costs more than the whole guard).
    token = _capturing.set(1)
    try:
        _record_inputs_guarded(node, args, kwargs, param_names)
    finally:
        _capturing.reset(token)


def _record_inputs_guarded(node: dict, args: tuple, kwargs: dict, param_names: tuple) -> None:
    start = perf_counter()
    captured = []
    for i, a in enumerate(args):
        pname = param_names[i] if i < len(param_names) else None
        if pname is not None and _is_skipped_variable(pname):
            captured.append(f"<skipped:{pname}>")
        else:
            captured.append(_capture_positional(a))
    node["args"] = captured
    captured_kwargs = {
        k: jv
        for k, v in kwargs.items()
        if not _is_skipped_variable(k) and (jv := _capture_value(v)) is not _SKIP
    }
    if captured_kwargs:  # omitted when empty, like most calls' kwargs
        node["kwargs"] = captured_kwargs
    _add_capture_time(node, start)
    # Rounded now too: a call still running when its trace closes never
    # completes, so this is the value it is written out with.
    node["capture_time"] = round(node["capture_time"], _TIME_DECIMALS)


def _add_capture_time(node: dict, start: float) -> None:
    # Accumulated unrounded; rounded once when the call completes (_completion
    # / _TraceState.complete, and the trace root before it is emitted).
    node["capture_time"] = node.get("capture_time", 0.0) + (perf_counter() - start)


def _record_output(node: dict, result: Any) -> None:
    """Attach a JSON-safe snapshot of a call's return value to its node.

    No-op unless :data:`CAPTURE_IO` is on. Stores the converted value under
    ``node["result"]``; an unserializable result (``_SKIP``) becomes a
    ``"<TypeName>"`` marker so the fact that *something* was returned is kept.

    Also accumulates onto ``node["capture_time"]`` — see :func:`_record_inputs`
    for why.

    Args:
        node: The call-tree node to annotate in place.
        result: The value returned by the traced function.
    """
    if _cfg.CAPTURE_IO and "capture_skipped" not in node:
        token = _capturing.set(1)
        try:
            start = perf_counter()
            jv = _capture_value(result)
        finally:
            _capturing.reset(token)
        node["result"] = f"<{type(result).__name__}>" if jv is _SKIP else jv
        _add_capture_time(node, start)


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
        "text": _redact(origin.line),
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
        {"file": f.filename, "line": f.lineno, "function": f.name, "text": _redact(f.line)}
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    debug = {
        "frames": frames,
        "formatted": _redact("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
    }
    cause = exc.__cause__ if exc.__cause__ is not None else (None if exc.__suppress_context__ else exc.__context__)
    if cause is not None and id(cause) not in _seen:
        debug["caused_by"] = {"exception": _safe_repr(cause), **_exception_debug(cause, _seen | {id(exc)})}
    return debug


def _record_exception(node: dict, exc: BaseException, full: bool = False) -> None:
    """Record, under ``node["exception"]``, the repr of an exception a call raised.

    No-op unless :data:`CAPTURE_IO` is on. Uses :func:`_safe_repr` so an
    exception whose own ``__repr__`` misbehaves can't break the trace. The
    presence of this key is also what flips a shipped trace's status to
    ``"error"`` in :func:`_send_trace`. Also attaches:

    * ``node["exception_origin"]`` (see :func:`_exception_origin`) — the single deepest
      frame, for an at-a-glance file/line;
    * ``node["exception_debug"]`` (see :func:`_exception_debug`) — the full, untruncated
      frame-by-frame traceback (plus any chained cause), for actually debugging the failure.

    Also accumulates onto ``node["capture_time"]`` — see :func:`_record_inputs`
    for why (walking a full traceback/cause chain isn't free either).

    Args:
        node: The call-tree node to annotate in place.
        exc: The exception raised out of the traced call (re-raised by the caller).
    """
    if _cfg.CAPTURE_IO:
        token = _capturing.set(_capturing.get() + 1)  # repr() of a user exception runs user code
        try:
            _record_exception_fields(node, exc, full)
        finally:
            _capturing.reset(token)


def _record_exception_fields(node: dict, exc: BaseException, full: bool = False) -> None:
    """Describing an exception walks and formats its whole traceback, which
    grows by a frame at every level it unwinds through: doing that at each
    traced call on the way up costs O(depth^2) in time and output. So the
    first node to record a given exception object gets the full description,
    and every later one in the same trace reuses it (its origin is the same
    frame, and its frames are where the exception actually came from). With
    `full` (the trace root) it is described again from the top, so the root
    still holds the complete outer-to-inner traceback."""
    start = perf_counter()
    node["exception"] = _safe_repr(exc)
    state = _current_trace.get()
    seen = state.last_exception if state is not None else None
    if seen is not None and seen[0] is exc and not full:
        _, origin, debug = seen
    else:
        origin = _exception_origin(exc)
        # Interned: nodes sharing identical parts (frames, cause chain)
        # store them once.
        debug = _intern(_exception_debug(exc))
        if state is not None:
            state.last_exception = (exc, origin, debug)
    if origin is not None:
        node["exception_origin"] = origin
    node["exception_debug"] = debug
    _add_capture_time(node, start)


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
    if name.startswith(_SKIP_MODULE_PREFIXES):
        return False
    filepath = getattr(module, "__file__", None)
    if filepath is None:
        return False
    # C extensions can't be patched
    if filepath.endswith((".so", ".pyd", ".dylib")):
        return False
    if not _in_library_paths(filepath):
        return True  # your own code: always (SCAN_VENV only ADDS library code)
    if not _cfg.SCAN_VENV:
        return False
    prefixes = _cfg._SCAN_INCLUDE_PREFIXES
    return not prefixes or any(name == p or name.startswith(p + ".") for p in prefixes)


_library_path_cache: dict = {}  # module __file__ -> whether it resolves under stdlib/site-packages


def _in_library_paths(filepath: str) -> bool:
    """Whether `filepath` lies under stdlib/site-packages (:data:`_VENV_PATHS`).
    Resolved once per file: ``Path.resolve`` costs an lstat per path
    component, and every trace start asks this for every loaded module."""
    verdict = _library_path_cache.get(filepath)
    if verdict is None:
        resolved = Path(filepath).resolve()
        verdict = _library_path_cache[filepath] = any(resolved.is_relative_to(venv) for venv in _VENV_PATHS)
    return verdict


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


# Undo log of every patch, keyed by the module the patched code BELONGS to:
# origin module name -> [(kind, target, name, original, replacement)]. Lets a
# module that stops counting as project code (SCAN_VENV switched off, a
# different _SCAN_INCLUDE_PREFIXES) be restored to exactly what it was.
_undo_log: dict = {}


def _set_patched(target: Any, name: str, value: Any, origin: str) -> None:
    """``setattr(target, name, value)``, recording how to undo it."""
    original = vars(target).get(name)
    setattr(target, name, value)
    _undo_log.setdefault(origin, []).append(("attr", target, name, original, value))


def _unpatch_non_project() -> None:
    """Restore everything patched for modules that are no longer project code
    under the current config. A binding the program has since replaced
    (no longer our wrapper) is left alone."""
    for origin, entries in list(_undo_log.items()):
        module = sys.modules.get(origin)
        if module is not None and _is_project_module(module):
            continue
        for kind, target, name, original, value in reversed(entries):
            try:
                if kind == "register":
                    target.register(name, original)
                elif vars(target).get(name) is value:
                    setattr(target, name, original)
            except Exception:
                continue
            if isinstance(target, type):
                _patched_classes.discard(target)
        replaced = {id(e[4]) for e in entries}
        for key, (orig, wrapped) in list(_wrapped_originals.items()):
            if id(wrapped) in replaced:
                del _wrapped_originals[key]
        del _undo_log[origin]


def _module_label(name: str) -> str:
    """Module name as shown in node names: a spawned worker re-imports the
    main script as ``__mp_main__``; it is the same code as ``__main__``."""
    return "__main__" if name == "__mp_main__" else name


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
        if name.startswith("__"):
            continue  # dunders (__getattr__/__dir__ of a module, …) stay as they are
        try:
            attr = vars(module)[name]
        except Exception:
            continue
        if _is_lru_cache(attr) and getattr(attr, "__module__", None) == module.__name__:
            try:
                wrapped = _cache_wrap(attr, f"{_module_label(module.__name__)}.{name}")
                _set_patched(module, name, wrapped, module.__name__)
                _wrapped_originals[id(attr)] = (attr, wrapped)
                _bump_wrap_generation()
            except (TypeError, AttributeError):
                pass
            continue
        if (
            not inspect.isfunction(attr)
            or isinstance(attr, type)
            or getattr(attr, "__module__", None) != module.__name__
            or getattr(attr, "_perf_wrapped", False)
        ):
            continue
        try:
            wrapped = _wrap_any(attr, f"{_module_label(module.__name__)}.{name}")
            wrapped._perf_wrapped = True  # type: ignore
            _set_patched(module, name, wrapped, module.__name__)
            _wrapped_originals[id(attr)] = (attr, wrapped)
            _bump_wrap_generation()
        except (TypeError, AttributeError):
            continue


def _patch_reexported_names(module: Any) -> None:
    """Fix up bindings of functions re-exported via ``from other import func``.

    :func:`_patch_module_functions` only rebinds a wrapped function in the
    module that *defines* it. A module that imported the original by name
    (``from other import func``) holds its own separate reference to the
    pre-wrap function object, unaffected by that rebind. This walks a module's
    namespace and, for any attribute that's an original tracked in
    :data:`_wrapped_originals`, rebinds it to the wrapped version too.

    Must run after every defining module has already been passed through
    :func:`_patch_module_functions`, so the map is fully populated first.

    Args:
        module: A module already judged to be project code.
    """
    if module.__name__ == __name__:
        return
    for name in list(vars(module)):
        if name.startswith("__"):
            continue
        try:
            attr = vars(module)[name]
        except Exception:
            continue
        try:
            if type(attr) is partial:
                # A module-level partial built at import time holds the
                # pre-patch function: rebuild it around the wrapper.
                entry = _wrapped_originals.get(id(attr.func))
                if entry is not None and attr.func is entry[0]:
                    _set_patched(module, name, partial(entry[1], *attr.args, **attr.keywords), entry[0].__module__)
                continue
            if inspect.isfunction(attr) and callable(getattr(attr, "register", None)) and isinstance(
                getattr(attr, "registry", None), types.MappingProxyType
            ):
                _patch_singledispatch(attr)
            entry = _wrapped_originals.get(id(attr))
            if entry is None or attr is not entry[0]:
                continue
            _set_patched(module, name, entry[1], entry[0].__module__)
        except (TypeError, AttributeError):
            continue


def _patch_singledispatch(dispatcher: Any) -> None:
    """Re-register a ``functools.singledispatch`` function's implementations
    with their instrumented versions (the registry captured the originals at
    import time). The default (``object``) implementation is the dispatcher's
    own body, already covered by the dispatcher's node."""
    for cls, impl in list(dispatcher.registry.items()):
        if cls is object:
            continue
        entry = _wrapped_originals.get(id(impl))
        if entry is not None and impl is entry[0]:
            dispatcher.register(cls, entry[1])
            _undo_log.setdefault(impl.__module__, []).append(("register", dispatcher, cls, impl, entry[1]))


_LRU_TYPE = type(functools.lru_cache(maxsize=None)(lambda: None))


def _is_lru_cache(obj: Any) -> bool:
    return type(obj) is _LRU_TYPE


def _cache_wrap(cached: Any, name: str) -> Any:
    """Node wrapper for a ``functools.lru_cache`` / ``functools.cache``
    function. Every call (hit or miss) gets a node; ``cache`` is set to
    ``"hit"`` / ``"miss"`` when the cache counters prove which one this call
    was (exactly one counter moved — other threads using the same cache at
    the same moment make it ambiguous, and then it is left out rather than
    guessed). ``cache_info`` / ``cache_clear`` / ``cache_parameters`` /
    ``__wrapped__`` keep working on the wrapper."""

    def counters() -> Any:
        before = cached.cache_info()

        def finish() -> dict:
            after = cached.cache_info()
            hits, misses = after.hits - before.hits, after.misses - before.misses
            return {"cache": "hit" if hits else "miss"} if hits + misses == 1 else {}

        return finish

    wrapper = _sync_wrap(cached, name, extra=counters)
    for attr in ("cache_info", "cache_clear", "cache_parameters"):
        if hasattr(cached, attr):
            setattr(wrapper, attr, getattr(cached, attr))
    wrapper._perf_wrapped = wrapper._perf_node = True  # type: ignore
    return wrapper


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
        return  # hot path: no lock
    with _patch_lock:
        _patch_class_locked(cls)


def _patch_class_locked(cls: type) -> None:
    if cls in _patched_classes or cls in _skip_classes:
        return  # another thread got here first
    if not _is_project_class(cls) or cls.__module__ == __name__:
        _skip_classes.add(cls)  # cache the verdict; never re-resolve this class
        return
    _patched_classes.add(cls)
    for name in list(vars(cls)):
        if name.startswith("__") and name.endswith("__") and name not in _TRACED_DUNDERS:
            continue
        try:
            raw = vars(cls)[name]
        except Exception:
            continue
        if getattr(getattr(raw, "__func__", raw), "_perf_node", False):
            continue  # already a perf node wrapper (e.g. a patched function assigned onto the class)
        try:
            if type(raw) is property:
                _set_patched(cls, name, property(
                    _wrap_any(raw.fget, f"{cls.__name__}.{name}"),
                    _wrap_any(raw.fset, f"{cls.__name__}.{name}.setter"),
                    _wrap_any(raw.fdel, f"{cls.__name__}.{name}.deleter"),
                    raw.__doc__,
                ), cls.__module__)
            elif type(raw) is functools.cached_property:
                # Only a cache MISS calls the function, so only misses get a node.
                cached = functools.cached_property(_wrap_any(raw.func, f"{cls.__name__}.{name}"))
                cached.__set_name__(cls, name)
                _set_patched(cls, name, cached, cls.__module__)
            elif _is_lru_cache(raw):
                _set_patched(cls, name, _cache_wrap(raw, f"{cls.__name__}.{name}"), cls.__module__)
            elif isinstance(raw, staticmethod):
                wrapped = _wrap_any(raw.__func__, f"{cls.__name__}.{name}")
                _set_patched(cls, name, staticmethod(wrapped), cls.__module__)
            elif isinstance(raw, classmethod):
                wrapped = _wrap_any(raw.__func__, f"{cls.__name__}.{name}")
                _set_patched(cls, name, classmethod(wrapped), cls.__module__)
            elif inspect.isfunction(raw):
                # SUPPRESS_ERRORS never applies to __init__/__call__/__enter__/…:
                # swallowing a constructor's or protocol method's failure would
                # hand back a half-built object / a broken `with`.
                _set_patched(cls, name, _wrap_any(raw, f"{cls.__name__}.{name}", suppress=name not in _TRACED_DUNDERS), cls.__module__)
        except (TypeError, AttributeError):
            # immutable C-extension type — skip it and move on
            continue


# Dunder methods that get nodes: they are called by the PROGRAM, never by
# perf's own value capture (which calls __repr__, __eq__, __hash__, …, so
# tracing those would record perf's own work — or recurse).
_TRACED_DUNDERS = frozenset({"__init__", "__call__", "__enter__", "__exit__", "__aenter__", "__aexit__"})


def _inner_code(func: Any) -> Any:
    """Code object of the function a wrapper ultimately runs (through
    __wrapped__ chains), for TRACE_LOCALS."""
    try:
        return getattr(inspect.unwrap(getattr(func, "__func__", func)), "__code__", None)
    except Exception:
        return None


def _wrap_any(func: Any, name: str, suppress: bool = True) -> Any:
    """Node wrapper for any kind of function; None passes through."""
    if func is None or not inspect.isfunction(func) or getattr(func, "_perf_node", False):
        return func
    if getattr(func, "__code__", None) in _CM_HELPER_CODES and inspect.isfunction(getattr(func, "__wrapped__", None)):
        return _cm_wrap(func, name)
    if inspect.isasyncgenfunction(func):
        return _agen_wrap(func, name, suppress=suppress)
    if inspect.isgeneratorfunction(func):
        return _gen_wrap(func, name, suppress=suppress)
    if inspect.iscoroutinefunction(func):
        return _async_wrap(func, name, suppress=suppress)
    return _sync_wrap(func, name, suppress=suppress)


# ── generators: the node follows the generator's own execution ──────────────
# A generator function's body doesn't run when it is called -- it runs bit by
# bit, each time the consumer asks for the next value. Its node therefore
# starts when the generator STARTS RUNNING (first next()), under whoever does
# that; each resume runs with the generator's node current (so its calls nest
# under it) and restores the consumer's node before every yield (so the
# consumer's own work between values stays under the consumer). The node
# records: `time` (wall, first resume → end), `active_time` (time actually
# spent running the generator's code -- used as its own time in the report),
# `yields` (count), `yielded` (the first MAX_YIELDS_CAPTURE values, captured as
# they were yielded), the return value as `result`, an exception, or
# `closed: true` when the consumer stopped early. The wrappers are themselves
# generator functions, so inspect.isgeneratorfunction() & co. still hold.


class _GenRecorder:
    __slots__ = ("node", "state", "active", "yields", "yielded", "limit")

    def __init__(self, node: dict, state: "_TraceState") -> None:
        self.node, self.state = node, state
        self.active, self.yields, self.yielded = 0.0, 0, []
        self.limit = _cfg.MAX_YIELDS_CAPTURE

    def on_yield(self, value: Any) -> None:
        self.yields += 1
        if len(self.yielded) < self.limit and _cfg.CAPTURE_IO and "capture_skipped" not in self.node:
            token = _capturing.set(1)
            try:
                jv = _capture_value(value)
            finally:
                _capturing.reset(token)
            self.yielded.append(f"<{type(value).__name__}>" if jv is _SKIP else jv)

    def finish(self, **extra: Any) -> dict:
        done = _completion(self.node)
        done["active_time"] = round(self.active, _TIME_DECIMALS)
        done["yields"] = self.yields
        if self.yielded:
            done["yielded"] = self.yielded
        done.update(extra)
        return done


def _start_generator(name: str, args: tuple, kwargs: dict, param_names: tuple, code: Any = None) -> "_GenRecorder | None":
    recording = _recording()
    if recording is None:
        return None
    parent, state = recording
    node = make_node(name)
    _record_inputs(node, args, kwargs, param_names)
    if not state.attach(parent, node):
        return None
    if _locals_users and _locals_wanted(name):
        _locals_enable(code, node)
    _restart(node)
    return _GenRecorder(node, state)


def _gen_wrap(func: Any, name: str, suppress: bool = True) -> Any:
    _cover(func, name)
    inner_code = _inner_code(func)
    param_names = _resolve_param_names(func)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        gen = func(*args, **kwargs)
        rec = _start_generator(name, args, kwargs, param_names, inner_code)
        if rec is None:
            return (yield from gen)
        sent: Any = None
        thrown: Any = None
        while True:
            token = _current_node.set(rec.node)
            t0 = perf_counter()
            try:
                if _timeout_pending:
                    _raise_pending_timeout()
                value = gen.throw(thrown) if thrown is not None else gen.send(sent)
            except StopIteration as stop:
                rec.active += perf_counter() - t0
                done = rec.finish()
                _record_output(done, stop.value)
                rec.state.complete(rec.node, done)
                return stop.value
            except BaseException as exc:
                rec.active += perf_counter() - t0
                done = rec.finish()
                _record_exception(done, exc)
                rec.state.complete(rec.node, done)
                if suppress and _cfg.SUPPRESS_ERRORS and _suppressible(exc):
                    return None  # a failed generator just ends
                raise
            finally:
                _current_node.reset(token)
            rec.active += perf_counter() - t0
            rec.on_yield(value)
            thrown = None
            try:
                sent = yield value
            except GeneratorExit:
                # Consumer stopped early: the generator's own cleanup (finally
                # blocks, a context manager's teardown) runs inside close().
                token = _current_node.set(rec.node)
                t0 = perf_counter()
                try:
                    gen.close()
                finally:
                    rec.active += perf_counter() - t0
                    _current_node.reset(token)
                    rec.state.complete(rec.node, rec.finish(closed=True))
                raise
            except BaseException as exc:
                thrown = exc  # consumer threw into us: forward it

    wrapper._perf_wrapped = wrapper._perf_node = True  # type: ignore
    return wrapper


def _agen_wrap(func: Any, name: str, suppress: bool = True) -> Any:
    _cover(func, name)
    inner_code = _inner_code(func)
    param_names = _resolve_param_names(func)

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        agen = func(*args, **kwargs)
        rec = _start_generator(name, args, kwargs, param_names, inner_code)
        if rec is None:
            async for value in agen:
                yield value
            return
        sent: Any = None
        thrown: Any = None
        while True:
            token = _current_node.set(rec.node)
            t0 = perf_counter()
            try:
                value = await (agen.athrow(thrown) if thrown is not None else agen.asend(sent))
            except StopAsyncIteration:
                rec.active += perf_counter() - t0
                rec.state.complete(rec.node, rec.finish())
                return
            except BaseException as exc:
                rec.active += perf_counter() - t0
                done = rec.finish()
                _record_exception(done, exc)
                rec.state.complete(rec.node, done)
                if suppress and _cfg.SUPPRESS_ERRORS and _suppressible(exc):
                    return
                raise
            finally:
                _current_node.reset(token)
            rec.active += perf_counter() - t0
            rec.on_yield(value)
            thrown = None
            try:
                sent = yield value
            except GeneratorExit:
                token = _current_node.set(rec.node)
                t0 = perf_counter()
                try:
                    await agen.aclose()
                finally:
                    rec.active += perf_counter() - t0
                    _current_node.reset(token)
                    rec.state.complete(rec.node, rec.finish(closed=True))
                raise
            except BaseException as exc:
                thrown = exc

    wrapper._perf_wrapped = wrapper._perf_node = True  # type: ignore
    return wrapper


def _cm_wrap(helper: Any, name: str) -> Any:
    """A ``@contextmanager`` / ``@asynccontextmanager`` function: rebuilt
    around the instrumented generator, so setup (before ``yield``) and
    teardown (after it) run under the context manager's own node. The
    ``with`` body runs while the generator is suspended, so it stays under
    the code that wrote the ``with``."""
    import contextlib

    inner = helper.__wrapped__
    if inspect.isasyncgenfunction(inner):
        wrapped = contextlib.asynccontextmanager(_agen_wrap(inner, name))
    else:
        wrapped = contextlib.contextmanager(_gen_wrap(inner, name))
    wrapped._perf_wrapped = wrapped._perf_node = True  # type: ignore
    return wrapped


def _cm_helper_codes() -> frozenset:
    import contextlib

    def gen():
        yield

    async def agen():
        yield

    return frozenset({contextlib.contextmanager(gen).__code__, contextlib.asynccontextmanager(agen).__code__})


_CM_HELPER_CODES = _cm_helper_codes()


def _register_wrapper_codes() -> None:
    """Code objects of perf's own wrappers: a call made directly from one of
    them is already recorded by it (see _on_event_start)."""

    def f():
        return None

    async def af():
        return None

    for wrapper in (_sync_wrap(f, "f"), _async_wrap(af, "af")):
        wrapper = getattr(wrapper, "_perf_slow", wrapper)  # the Python wrapper behind a C one
        _WRAPPER_CODES.add(wrapper.__code__)


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
    with _patch_lock:
        _patch_all_project_classes_locked()


# Every trace start walks the loaded project modules, but between traces
# their namespaces rarely change. A module whose namespace still holds the
# very same names bound to the very same objects as right after its last scan
# (checked by identity, at C speed) has nothing new to patch, so it is
# skipped. _wrap_generation counts additions to _wrapped_originals: while it
# is unchanged, no module can have gained a re-exported binding to fix up.
_module_scans: dict = {}  # module name -> (module, keys, values) after its last scan
_wrap_generation = 0
_scanned_generation = -1


def _bump_wrap_generation() -> None:
    global _wrap_generation
    _wrap_generation += 1


def _module_unchanged(module: Any) -> bool:
    snap = _module_scans.get(module.__name__)
    if snap is None or snap[0] is not module:
        return False
    try:
        return bool(_same_items(vars(module), snap[1], snap[2]))
    except RuntimeError:  # pure-Python check: the namespace changed while being compared
        return False


def _remember_scan(module: Any) -> None:
    items = tuple(vars(module).items())  # one step: consistent even if another thread writes
    _module_scans[module.__name__] = (module, tuple(k for k, _ in items), tuple(v for _, v in items))


def _patch_all_project_classes_locked() -> None:
    global _skip_classes_config, _scanned_generation
    config = (_cfg.SCAN_VENV, tuple(_cfg._SCAN_INCLUDE_PREFIXES))
    if config != _skip_classes_config:
        _skip_classes.clear()
        _expandable_cache.clear()
        _module_scans.clear()
        if _skip_classes_config is not None:
            _unpatch_non_project()
            _events_config_changed()
        _skip_classes_config = config
    # list() snapshots sys.modules atomically: another thread may import
    # (and so grow sys.modules) while we're walking it.
    project_modules = [m for m in list(sys.modules.values()) if m is not None and _is_project_module(m)]
    changed = [m for m in project_modules if not _module_unchanged(m)]
    for module in changed:
        _patch_module_functions(module)
        for attr_name in list(vars(module)):
            try:
                attr = vars(module)[attr_name]
            except Exception:
                continue
            if isinstance(attr, type) and attr.__module__ == module.__name__:
                _patch_class(attr)
    # Second pass: fix up `from other import func` bindings now that every
    # defining module's own functions have been wrapped (see
    # _patch_reexported_names). Every module needs it when anything new was
    # wrapped anywhere; otherwise only the ones that changed.
    rescan = project_modules if _wrap_generation != _scanned_generation else changed
    for module in rescan:
        _patch_reexported_names(module)
    for module in rescan:
        _remember_scan(module)
    if len(_module_scans) > len(project_modules):  # unloaded modules: don't keep them alive
        live = {m.__name__ for m in project_modules}
        for name in [n for n in _module_scans if n not in live]:
            del _module_scans[name]
    _scanned_generation = _wrap_generation


# ── sync / async node wrappers ────────────────────────────────────────────────


def _sync_wrap(func: Any, name: str, extra: Any = None, suppress: bool = True) -> Any:
    """Wrap a synchronous callable so each call becomes a timed tree node.

    The returned wrapper: creates a node named ``name``, links it under the
    current node (:data:`_current_node`) so the tree mirrors the real call
    stack, and makes it current for the duration via a ``ContextVar`` token —
    all before invoking `func`. Its inputs are snapshotted BEFORE `func` runs
    (so an argument `func` mutates is recorded as received), after which the
    node's start is re-stamped so that capture isn't counted as the call's own
    time. As soon as `func` returns or raises, :func:`_finish_node` stamps
    ``time``/``end`` immediately — before output capture, for the same reason.
    Outside any trace (no current node) the wrapper is a pure pass-through. Exceptions are always recorded
    on the node; what happens next depends on :data:`SUPPRESS_ERRORS` — off
    (the default), they re-raise as before, propagating up the real call stack;
    on, this call swallows its own exception and returns a default value
    derived from *its own* return annotation instead, containing the failure at
    the point it happened rather than unwinding the whole chain. The previous
    current-node is always restored.

    Args:
        func: The original synchronous function/method to time.
        name: Display name for the node (typically ``"Owner.attr"``).

    Returns:
        A ``functools.wraps``-preserving wrapper with the same signature.
    """
    _cover(func, name)
    inner_code = _inner_code(func)
    default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    param_names = _resolve_param_names(func)

    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        if _current_node.get() is None:
            # Not inside any trace (the common case once a trace has patched
            # the program): behave exactly like the original, as cheaply as possible.
            return func(*args, **kwargs)
        if _is_skipped(name) or _skip_depth.get():
            token = _skip_depth.set(_skip_depth.get() + 1)
            try:
                return func(*args, **kwargs)
            finally:
                _skip_depth.reset(token)
        recording = _recording()
        if recording is None:
            # Not inside an open trace: behave exactly like the original.
            return func(*args, **kwargs)
        parent, state = recording
        node = make_node(name)
        # Inputs are snapshotted BEFORE the call, so an argument the call
        # mutates is recorded as it was received, not as it was left. The
        # node is still private here, so no lock is needed yet.
        _record_inputs(node, args, kwargs, param_names)
        if not state.attach(parent, node):
            return func(*args, **kwargs)  # the trace closed meanwhile
        if _locals_users and _locals_wanted(name):
            _locals_enable(inner_code, node)
        token = _current_node.set(node)
        # `extra` (see _cache_wrap): called before the call, returns a
        # callable giving extra fields for this node once the call is over.
        finish_extra = extra() if extra is not None else None
        done = None
        _restart(node)  # input capture isn't part of the call's own time
        try:
            if _timeout_pending:
                _raise_pending_timeout()  # a TIMEOUT that fired inside perf's code
            result = func(*args, **kwargs)
            done = _completion(node)
            _record_output(done, result)
            return result
        except BaseException as exc:
            done = _completion(node)
            _record_exception(done, exc)
            if not (suppress and _cfg.SUPPRESS_ERRORS and _suppressible(exc)):
                raise
            result = _default_return_unrecorded(default_factory)
            _record_output(done, result)
            return result
        finally:
            if done is not None:
                if finish_extra is not None:
                    done.update(finish_extra())
                state.complete(node, done)
            _current_node.reset(token)

    wrapper._perf_wrapped = wrapper._perf_node = True  # type: ignore
    if _speedups is None:
        return wrapper
    # Same behaviour, bookkeeping in C (see _speedups.c); `wrapper` stays
    # behind it for the rare paths (SKIP_FUNCTIONS, a pending TIMEOUT).
    fast = _speedups.NodeWrapper(func, name, wrapper, extra, suppress, default_factory, param_names, inner_code)
    functools.update_wrapper(fast, func)
    fast._perf_wrapped = fast._perf_node = True  # type: ignore
    return fast


# Returned by _sync_failure when the exception must propagate unchanged.
_RERAISE = object()


def _sync_failure(done: dict, exc: BaseException, suppress: bool, default_factory: Any) -> Any:
    """The ``except`` branch of :func:`_sync_wrap`'s wrapper, for the C
    wrapper: record `exc` on `done`, then return :data:`_RERAISE` or (under
    SUPPRESS_ERRORS) the call's default return value."""
    _record_exception(done, exc)
    if not (suppress and _cfg.SUPPRESS_ERRORS and _suppressible(exc)):
        return _RERAISE
    result = _default_return_unrecorded(default_factory)
    _record_output(done, result)
    return result


def _async_wrap(func: Any, name: str, suppress: bool = True) -> Any:
    """Wrap a coroutine function so each awaited call becomes a timed tree node.

    Async analogue of :func:`_sync_wrap`: same node creation, input/output
    capture, parent linking, and current-node bookkeeping, but it ``await``\\ s
    the inner coroutine. Inputs are snapshotted before the ``await`` and the
    output after it, with timing stamped immediately around the ``await`` —
    for the same reasons as the sync path.
    Because linkage flows through a ``ContextVar``, sibling coroutines running
    concurrently each attach to their own logical parent, and their nodes'
    start/time intervals let the reporter compute overlap-aware wall time.
    Exceptions are always recorded on the node; per :data:`SUPPRESS_ERRORS`,
    they either re-raise (default) or are swallowed with a type-derived default
    returned in their place — see :func:`_sync_wrap` for the full rationale.

    Args:
        func: The original coroutine function to time.
        name: Display name for the node (typically ``"Owner.attr"``).

    Returns:
        A ``functools.wraps``-preserving async wrapper with the same signature.
    """
    _cover(func, name)
    inner_code = _inner_code(func)
    default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    param_names = _resolve_param_names(func)

    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        if _current_node.get() is None:
            # Not inside any trace (the common case once a trace has patched
            # the program): behave exactly like the original, as cheaply as possible.
            return await func(*args, **kwargs)
        if _is_skipped(name) or _skip_depth.get():
            token = _skip_depth.set(_skip_depth.get() + 1)
            try:
                return await func(*args, **kwargs)
            finally:
                _skip_depth.reset(token)
        recording = _recording()
        if recording is None:
            # Not inside an open trace: behave exactly like the original.
            return await func(*args, **kwargs)
        parent, state = recording
        node = make_node(name)
        # Inputs are snapshotted BEFORE the call, so an argument the call
        # mutates is recorded as it was received, not as it was left. The
        # node is still private here, so no lock is needed yet.
        _record_inputs(node, args, kwargs, param_names)
        if not state.attach(parent, node):
            return await func(*args, **kwargs)  # the trace closed meanwhile
        if _locals_users and _locals_wanted(name):
            _locals_enable(inner_code, node)
        token = _current_node.set(node)
        _restart(node)  # input capture isn't part of the call's own time
        try:
            if _timeout_pending:
                _raise_pending_timeout()
            result = await func(*args, **kwargs)
            done = _completion(node)
            _record_output(done, result)
            state.complete(node, done)
            return result
        except BaseException as exc:
            done = _completion(node)
            _record_exception(done, exc)
            if not (suppress and _cfg.SUPPRESS_ERRORS and _suppressible(exc)):
                state.complete(node, done)
                raise
            result = _default_return_unrecorded(default_factory)
            _record_output(done, result)
            state.complete(node, done)
            return result
        finally:
            _current_node.reset(token)

    wrapper._perf_wrapped = wrapper._perf_node = True  # type: ignore
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
        A ``profiler(frame, event, arg)`` callable to hand to ``sys.setprofile``.

    Note:
        Deliberately not installed via ``threading.setprofile`` — that applies
        to every thread *started* while it's set, including long-lived threads
        owned by libraries (e.g. a websocket client's read-loop thread) that
        outlive this call entirely. Since a thread's profile function can only
        be cleared from within that thread, such a thread would carry the
        profiling overhead for the rest of its life, well after this trace has
        finished. Scoping to ``sys.setprofile`` on the calling thread only means
        instances first touched on other threads during the call won't be
        patched, but that's a strictly better trade than an unbounded leak.
    """

    def profiler(frame: Any, event: Any, arg: Any) -> None:
        if (event == "return" and frame.f_code.co_name == "__init__") or event == "call":
            self = frame.f_locals.get("self")
            if self is not None:
                _patch_instance(self)

    return profiler


_HAS_MONITORING = hasattr(sys, "monitoring")


def _try_install_monitoring() -> Any:
    """Attempts the same instance-discovery hook via ``sys.monitoring`` (PEP
    669, Python 3.12+) instead of ``sys.setprofile``. Returns a zero-arg
    uninstall callable on success, or ``None`` if the tool slot can't be
    claimed (already held by another profiler/coverage tool, or by another
    concurrently-running perf trace on a different thread — see
    :func:`_install_profiler` for why that's the graceful-degradation case,
    not an error).

    Any failure after claiming the tool id (register_callback/set_events
    raising) frees it before returning ``None``, so a transient failure can't
    permanently lock every future trace in this process out of the fast path.
    """
    m = sys.monitoring
    tool_id = m.PROFILER_ID
    try:
        m.use_tool_id(tool_id, "perf")
    except ValueError:
        return None

    # The tool is process-wide: these fire for every Python call on EVERY
    # thread while a trace runs. DISABLE (per code object) wherever the answer
    # can never change -- a code without any `self` binding never has one at
    # PY_START, and only __init__'s return matters -- and bail out early on
    # threads that aren't recording.
    disable = m.DISABLE

    def on_start(code: Any, instruction_offset: Any) -> Any:
        if "self" not in code.co_varnames and "self" not in code.co_freevars:
            return disable
        if _current_node.get() is None:
            return None
        # PY_START hands (code, offset), not a frame -- unlike sys.setprofile,
        # which hands the frame directly. `self` (when there is one) is
        # already bound in locals by the time the function body starts.
        self = sys._getframe(1).f_locals.get("self")
        if self is not None:
            _patch_instance(self)
        return None

    def on_return(code: Any, instruction_offset: Any, retval: Any) -> Any:
        if code.co_name != "__init__":
            return disable
        if _current_node.get() is None:
            return None
        self = sys._getframe(1).f_locals.get("self")
        if self is not None:
            _patch_instance(self)
        return None

    try:
        m.register_callback(tool_id, m.events.PY_START, on_start)
        m.register_callback(tool_id, m.events.PY_RETURN, on_return)
        m.set_events(tool_id, m.events.PY_START | m.events.PY_RETURN)
    except Exception:
        m.free_tool_id(tool_id)
        return None

    def uninstall() -> None:
        try:
            m.set_events(tool_id, 0)
            m.register_callback(tool_id, m.events.PY_START, None)
            m.register_callback(tool_id, m.events.PY_RETURN, None)
            # Undo our DISABLEs: they outlive free_tool_id, and the next
            # holder of PROFILER_ID (cProfile on 3.12+) would miss those calls.
            m.restart_events()
        finally:
            m.free_tool_id(tool_id)

    return uninstall


def _install_profiler() -> Any:
    """Installs whichever instance-discovery hook is available, returning a
    zero-arg callable that uninstalls it — call exactly once, in a
    ``finally`` block (mirrors the old unconditional ``sys.setprofile(None)``).

    Prefers ``sys.monitoring`` over ``sys.setprofile``: measured ~0 overhead
    versus ``sys.setprofile``, whose per-call/return cost dominates a traced
    call's total wall time even before any value capture happens (see
    ``lab/research_monitoring*.py``). Falls back to ``sys.setprofile`` when
    ``sys.monitoring`` isn't available (Python < 3.12) or its tool slot is
    already claimed.

    Two behavioural differences from ``sys.setprofile``, both accepted
    trade-offs rather than oversights:

    * ``sys.monitoring``'s tool registration is process-global, not
      per-thread. Only one perf trace at a time can hold it — a second,
      concurrently-running trace on another thread will fail to claim it and
      fall back to ``sys.setprofile`` for itself, exactly today's behaviour,
      rather than error or silently corrupt the first trace's hook.
    * While active, it also fires for calls on OTHER threads doing unrelated
      work, not just the traced call's own thread (unlike ``sys.setprofile``,
      which is thread-scoped by design — see :func:`_make_profiler`'s note).
      Harmless in measured practice (near-zero cost, and :func:`_patch_instance`
      is idempotent/cached), but worth knowing if @trace runs inside one
      thread of a busy multi-threaded process.
    """
    if _HAS_MONITORING:
        uninstall = _try_install_monitoring()
        if uninstall is not None:
            return uninstall
    sys.setprofile(_make_profiler())
    return lambda: sys.setprofile(None)


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

    from time import monotonic

    # SIGALRM / ITIMER_REAL are process-wide and may already be in use by the
    # caller (or an enclosing traced call). Their pending alarm is kept: if it
    # is due before our deadline it is delivered to their handler on time and
    # our deadline re-armed; afterwards their handler and remaining time (and
    # repeat interval) are restored.
    deadline = monotonic() + seconds
    prev_delay, prev_interval = signal.getitimer(signal.ITIMER_REAL)
    prev_due = monotonic() + prev_delay if prev_delay > 0 else None

    def _arm() -> None:
        due = deadline if prev_due is None else min(deadline, prev_due)
        signal.setitimer(signal.ITIMER_REAL, max(due - monotonic(), 1e-6))

    deferrals = 0

    message = f"{name} exceeded TIMEOUT of {seconds:g}s"

    def _handler(signum: int, frame: Any) -> None:
        nonlocal prev_due, deferrals
        now = monotonic()
        if prev_due is not None and prev_due <= now + 1e-3 and now < deadline - 1e-3:
            prev_due = now + prev_interval if prev_interval > 0 else None
            _arm()
            _deliver(previous, signum, frame)
            return
        in_perf = frame is not None and frame.f_code.co_filename == __file__
        if in_perf or (_in_perf_internals() and deferrals < 300):
            # Never raise inside perf's own code: its defensive handlers would
            # swallow the TimeoutError, an exception between a lock's acquire
            # and its `with` block would leave it held (deadlock), and one
            # escaping a sys.monitoring callback can destabilise CPython.
            # Mark it pending -- the next node wrapper raises it right before
            # calling into the program -- and retry 10 ms later in case no
            # traced call comes first. (Program code run BY perf, e.g. a
            # __repr__, is raised into after ~3 s, so it can't hang forever.)
            deferrals += 1
            if not _timeout_pending:
                _timeout_pending.append((threading.get_ident(), message))
            signal.setitimer(signal.ITIMER_REAL, 0.01)
            return
        _timeout_pending.clear()
        raise TraceTimeout(message)

    previous = signal.signal(signal.SIGALRM, _handler)
    _arm()
    try:
        yield
    finally:
        _timeout_pending.clear()
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if prev_due is not None:
            signal.setitimer(signal.ITIMER_REAL, max(prev_due - monotonic(), 1e-6), prev_interval)


def _deliver(handler: Any, signum: int, frame: Any) -> None:
    """Runs a saved signal disposition for a signal perf intercepted."""
    if callable(handler):
        handler(signum, frame)
    elif handler == signal.SIG_DFL:
        # The default action (terminate, for SIGALRM): reproduce it faithfully.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)
    # SIG_IGN / None: nothing to do


# ── @trace entry-point decorator ─────────────────────────────────────────────

# Config attributes that may be overridden per-decorator, e.g.
# ``@trace(MIN_PCT=0.01, TIMEOUT=5)``. Each name maps to the module-level global
# of the same name; an override temporarily sets that global for the dynamic
# extent of the traced call (including printing/sending), then restores it.
_CONFIG_KEYS = frozenset(
    {
        "MIN_PCT",
        "HOT_SPOTS",
        "PRINT_REPORT",
        "OUTPUT_DIR",
        "OUTPUT_FILE",
        "LOG_FUNCTIONS",
        "ENDPOINT_URL",
        "TAGS",
        "API_KEY",
        "SEND_TIMEOUT",
        "TIMEOUT",
        "SCAN_VENV",
        "CAPTURE_IO",
        "MAX_DEPTH",
        "MAX_SIZE_CAPTURE",
        "SPILL_DIR",
        "SKIP_FUNCTIONS",
        "SUPPRESS_ERRORS",
        "DEFAULT_RETURN",
        "SKIP_VARIABLES",
        "_SCAN_INCLUDE_PREFIXES",
        "MAX_YIELDS_CAPTURE",
        "MAX_CAPTURES_PER_FUNCTION",
        "REDACT_PATTERNS",
        "TRACE_LOCALS",
        "MAX_LOCALS_EVENTS",
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
    """Apply `overrides` for the dynamic extent of a traced call -- in the
    current context only (see ``_overrides``), layered over any overrides of
    an enclosing trace. Module globals are never touched, so concurrent traces
    with different overrides can't see each other's values."""
    if not overrides:
        yield
        return
    token = _overrides.set({**(_overrides.get() or {}), **overrides})
    try:
        yield
    finally:
        _overrides.reset(token)


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

    # A target an earlier trace already instrumented (e.g. `perf.trace(mod.fn)`
    # after `mod` was patched) would otherwise show up as the root AND as the
    # root's only child.
    func = _unwrap_node(func)
    _cover(func, func.__qualname__)
    _root_code = _inner_code(func)
    _default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    _param_names = _resolve_param_names(func)

    @contextmanager
    def _session(args: tuple, kwargs: dict) -> Any:
        """Set up one traced call: root node + value pool made current (and
        reset afterwards, so nothing called after this trace — or by an
        enclosing trace this one is nested in — attaches to a finished
        root), inputs snapshotted before the call, profiler installed."""
        with _apply_overrides(overrides):
            try:
                _redactor()
            except re.error as e:
                raise ValueError(f"perf: invalid REDACT_PATTERNS entry: {e}") from None
            _patch_all_project_classes()
            _install_executor_hook()
            root = make_node(func.__qualname__)
            root["start_timestamp"] = root["end_timestamp"] = None  # set around the call (see _begin)
            state = _TraceState()
            node_token = _current_node.set(root)
            trace_token = _current_trace.set(state)
            # Fresh value pool for this trace only -- see _current_pool and _ValuePool.
            pool_token = _current_pool.set(_ValuePool(state=state) if _cfg.CAPTURE_IO else None)
            _events_acquire()
            locals_held = _locals_acquire()
            if locals_held and _locals_wanted(root["name"]):
                _locals_enable(_root_code, root)
            try:
                _record_inputs(root, args, kwargs, _param_names)
                uninstall_profiler = _install_profiler()
                try:
                    yield root
                finally:
                    root["end_timestamp"] = _now_iso()
                    if "capture_time" in root:
                        root["capture_time"] = round(root["capture_time"], _TIME_DECIMALS)
                    uninstall_profiler()
                    # Freeze the tree + pool before emitting them: work still
                    # running on other threads can't change them any more.
                    state.close()
                    try:
                        print_tree(root)
                    except Exception as e:  # reporting must never change the traced call's outcome
                        print(f"[perf] failed to report trace {root['name']}: {e!r}", file=sys.stderr)
            finally:
                if locals_held:
                    _locals_release()
                _events_release()
                _current_pool.reset(pool_token)
                _current_trace.reset(trace_token)
                _current_node.reset(node_token)

    def _begin(root: dict) -> None:
        _restart(root)  # setup above shouldn't count as the call's own time
        root["start_timestamp"] = _now_iso()

    def _failed(root: dict, exc: BaseException) -> Any:
        if root["time"] is None:
            _finish_node(root)
        _record_exception(root, exc, full=True)
        if not (_cfg.SUPPRESS_ERRORS and _suppressible(exc)):
            raise  # called from an `except` block: re-raises `exc` unchanged
        result = _default_return_unrecorded(_default_factory)
        _record_output(root, result)
        return result

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # type: ignore
            with _session(args, kwargs) as root:
                _begin(root)
                try:
                    if _cfg.TIMEOUT is not None and hasattr(asyncio, "timeout"):
                        # asyncio.timeout (3.11+) tells OUR deadline apart from a
                        # TimeoutError the program raised itself: only ours
                        # becomes a TraceTimeout (never suppressed, like sync).
                        deadline = asyncio.timeout(_cfg.TIMEOUT)
                        try:
                            async with deadline:
                                result = await func(*args, **kwargs)
                        except TimeoutError as exc:
                            if not deadline.expired():
                                raise
                            raise TraceTimeout(f"{func.__qualname__} exceeded TIMEOUT of {_cfg.TIMEOUT:g}s") from exc
                    elif _cfg.TIMEOUT is not None:
                        # Python 3.10: no asyncio.timeout. wait_for's own expiry is
                        # an asyncio.TimeoutError raised from the CancelledError it
                        # cancelled the call with (on 3.10 not even a builtin
                        # TimeoutError); a TimeoutError the program raised itself
                        # has no such cause and propagates unchanged.
                        try:
                            result = await asyncio.wait_for(func(*args, **kwargs), _cfg.TIMEOUT)
                        except asyncio.TimeoutError as exc:
                            if not isinstance(exc.__cause__, asyncio.CancelledError):
                                raise
                            raise TraceTimeout(f"{func.__qualname__} exceeded TIMEOUT of {_cfg.TIMEOUT:g}s") from exc
                    else:
                        result = await func(*args, **kwargs)
                    _finish_node(root)
                    _record_output(root, result)
                except BaseException as exc:
                    result = _failed(root, exc)
                return result
    else:

        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            with _session(args, kwargs) as root:
                try:
                    with _sync_timeout(_cfg.TIMEOUT, func.__qualname__):
                        _begin(root)
                        result = func(*args, **kwargs)
                        _finish_node(root)
                    _record_output(root, result)
                except BaseException as exc:
                    result = _failed(root, exc)
                return result

    # Never auto-instrument the entry point itself (it makes its own root).
    wrapper._perf_wrapped = True  # type: ignore
    _WRAPPER_CODES.add(wrapper.__code__)
    return wrapper


def _unwrap_node(fn: Any) -> Any:
    """`fn` without perf's own node wrapper, if it has one (bound methods are
    re-bound to the unwrapped function)."""
    if inspect.ismethod(fn) and getattr(fn.__func__, "_perf_node", False):
        return types.MethodType(fn.__func__.__wrapped__, fn.__self__)
    if _is_function(fn) and getattr(fn, "_perf_node", False):
        return fn.__wrapped__
    return fn


def _instrumented(fn: Any) -> Any:
    """`fn` as it looks now that this process is patched: an original free
    function becomes its wrapper, a bound method of a since-patched class
    becomes the bound wrapper, and a ``functools.partial`` (e.g. the
    per-chunk partial ``Executor.map`` submits) is rebuilt with its function
    and arguments resolved the same way. Anything else is returned as-is.

    Needed where a reference was taken BEFORE patching ran — e.g. unpickled
    in a fresh worker process — and would otherwise bypass instrumentation.
    """
    if isinstance(fn, partial):
        return partial(
            _instrumented(fn.func),
            *[_instrumented(a) for a in fn.args],
            **{k: _instrumented(v) for k, v in fn.keywords.items()},
        )
    if inspect.isfunction(fn):
        entry = _wrapped_originals.get(id(fn))
        if entry is not None and entry[0] is fn:
            return entry[1]
        return fn
    if inspect.ismethod(fn) and not getattr(fn.__func__, "_perf_node", False):
        try:
            current = getattr(fn.__self__, fn.__func__.__name__)
        except Exception:
            return fn
        if inspect.ismethod(current) and getattr(current.__func__, "__wrapped__", None) is fn.__func__:
            return current
    return fn


# ── ProcessPoolExecutor: trace work submitted to other processes ──────────────
# A function handed to ProcessPoolExecutor.submit runs in a worker process,
# where none of this process's patching or _current_node exists (under
# "spawn"/"forkserver" the worker re-imports project modules unpatched; even
# under "fork" its nodes would land in the worker's own memory). So while a
# trace is active, submit() ships the call wrapped in a _RemoteCall, which
# traces it inside the worker and sends its subtree back alongside the result;
# the parent then grafts that subtree under the node that called submit().
# perf_counter() is system-wide monotonic on Linux/macOS/Windows, so the
# worker's start/end stamps share the parent's timeline.

# Config the worker needs to instrument/capture the same way the parent does.
# Output/sending config is deliberately absent: the worker never prints or
# sends anything itself — its subtree is emitted as part of the parent's trace.
_REMOTE_CONFIG_KEYS = (
    "SCAN_VENV",
    "_SCAN_INCLUDE_PREFIXES",
    "CAPTURE_IO",
    "MAX_DEPTH",
    "MAX_SIZE_CAPTURE",
    "SPILL_DIR",
    "SKIP_FUNCTIONS",
    "SKIP_VARIABLES",
    "SUPPRESS_ERRORS",
    "DEFAULT_RETURN",
    "MAX_YIELDS_CAPTURE",
    "MAX_CAPTURES_PER_FUNCTION",
    "REDACT_PATTERNS",
    "TRACE_LOCALS",
    "MAX_LOCALS_EVENTS",
)


def _remote_config() -> dict:
    """Snapshot of the config the worker should trace with.

    ``DEFAULT_RETURN`` is left out when unset (``_UNSET`` is a per-process
    sentinel — a pickled copy would no longer be identical to the worker's)
    or unpicklable (e.g. a lambda), since one unpicklable value would make
    the whole submission fail rather than just lose that setting.
    """
    import pickle

    config = {}
    for key in _REMOTE_CONFIG_KEYS:
        value = getattr(_cfg, key)
        if key == "DEFAULT_RETURN":
            if value is _UNSET:
                continue
            try:
                pickle.dumps(value)
            except Exception:
                continue
        config[key] = value
    return config


def _remote_name(fn: Any) -> str:
    """The node name the patched wrapper of `fn` would have used locally:
    ``module.func`` for a free function, ``Class.method`` for a method."""
    func = fn.__func__ if inspect.ismethod(fn) else fn
    qualname = getattr(func, "__qualname__", None) or type(fn).__name__
    if _is_function(func) and "." not in qualname:
        return f"{_module_label(func.__module__)}.{qualname}"
    return qualname


class _RemoteOutcome:
    """What a :class:`_RemoteCall` sends back: the call's real `result` plus
    the worker-side subtree (`children`) and the pool `variables` it refers
    to. On failure the same object rides back on the exception instead, as
    its ``_perf_remote`` attribute."""

    def __init__(self, result: Any, children: list, variables: dict, token: Any = None, sources: dict | None = None) -> None:
        self.result = result
        self.children = children
        self.variables = variables
        self.token = token
        self.sources = sources or {}  # multiprocessing.Pool: where to graft (see _pool_grafts)


class _RemoteCall:
    """Picklable stand-in for `fn` that traces it inside a worker process.

    Pickled by reference (``perf._RemoteCall``) like any module-level class,
    so the worker just needs ``perf`` importable — which it is, since the
    parent imported it.
    """

    def __init__(self, fn: Any, name: str, config: dict, token: Any = None) -> None:
        self.fn = fn
        self.name = name
        self.config = config
        self.token = token

    def __call__(self, *args: Any, **kwargs: Any) -> "_RemoteOutcome":
        # A fresh, empty Context: the worker's own state (if it is itself
        # inside some unrelated trace) can't leak in, and nothing set here
        # outlives the call.
        import contextvars

        return contextvars.Context().run(self._run, args, kwargs)

    def _run(self, args: tuple, kwargs: dict) -> "_RemoteOutcome":
        import uuid

        _overrides.set(dict(self.config))  # this call's own fresh Context (see __call__)
        _install_executor_hook()  # nested pools inside the worker get traced too
        _patch_all_project_classes()
        holder = make_node(self.name)
        prefix = f"{uuid.uuid4().hex[:8]}_"
        state = _TraceState(source_prefix=prefix)
        pool = _ValuePool(prefix=prefix, state=state) if _cfg.CAPTURE_IO else None
        _current_node.set(holder)
        _current_trace.set(state)
        _current_pool.set(pool)
        # Unpickled by name BEFORE the patching above, `fn` (or, for the
        # functools.partial Executor.map submits per chunk, the function
        # inside it) is the original on this worker's first task but
        # already the wrapper on later ones. Resolve it to the patched
        # version so every task gets exactly one node; a callable that
        # still isn't instrumented (not project code / not reachable by
        # name) is wrapped here. Other callables (the partial itself) get
        # no node of their own; what they call attaches to the parent.
        fn = _instrumented(self.fn)
        target = fn.__func__ if inspect.ismethod(fn) else fn
        if inspect.isfunction(target) and not getattr(target, "_perf_node", False):
            fn = _sync_wrap(fn, self.name)
        uninstall_profiler = _install_profiler()
        _events_acquire()
        locals_held = _locals_acquire()
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            state.close()  # freeze before pickling: threads the call started may still run
            try:
                exc._perf_remote = _RemoteOutcome(None, holder["children"], pool.variables if pool else {}, self.token, state.sources)
            except Exception:
                pass  # an exception type that rejects attributes just loses its subtree
            raise
        finally:
            if locals_held:
                _locals_release()
            _events_release()
            uninstall_profiler()
        state.close()
        return _RemoteOutcome(result, holder["children"], pool.variables if pool else {}, self.token, state.sources)


def _graft_remote(future: Any, parent: dict, pool: "_ValuePool | None", state: "_TraceState") -> None:
    """Unwraps a finished remote future in place: swaps the
    :class:`_RemoteOutcome` in its result for the real result (or strips it
    off its exception), and attaches the worker subtree under `parent`.
    Runs at most once per future (see :func:`_traced_submit`)."""
    if future.cancelled():
        return
    exc = future._exception
    if exc is not None:
        outcome = exc.__dict__.pop("_perf_remote", None)
    else:
        outcome = future._result
        if not isinstance(outcome, _RemoteOutcome):
            return
        future._result = outcome.result
    if not isinstance(outcome, _RemoteOutcome):
        return
    with state.lock:
        if not state.open:
            return  # finished after the trace ended: not part of it
        if pool is not None:
            pool.variables.update(outcome.variables)
        state.sources.update(outcome.sources)
        parent["children"].extend(outcome.children)


_original_process_submit: Any = None


def _traced_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Replacement for ``ProcessPoolExecutor.submit``.

    Outside a trace (or under a SKIP_FUNCTIONS call) it's a plain
    pass-through. Inside one, `fn` is sent as a :class:`_RemoteCall` and the
    returned future is unwrapped before anyone can observe the
    :class:`_RemoteOutcome`: by a done-callback as soon as it completes, and
    — since waiters in ``result()``/``exception()`` can wake before
    done-callbacks run — by instance-level ``result``/``exception``
    overrides too. Both paths go through one lock-guarded, run-once
    :func:`_graft_remote`. Overriding the *reading* side (rather than the
    executor's ``set_result``) is what makes this race-free: nobody can read
    the future before submit() returns it.
    """
    recording = _recording()
    if recording is None or _skip_depth.get():
        return _original_process_submit(self, fn, *args, **kwargs)
    parent, state = recording

    future = _original_process_submit(self, _RemoteCall(fn, _remote_name(fn), _remote_config()), *args, **kwargs)
    pool = _current_pool.get()
    lock = threading.Lock()
    grafted = False

    def absorb(f: Any) -> None:
        nonlocal grafted
        with lock:
            if not grafted:
                grafted = True
                _graft_remote(f, parent, pool, state)

    original_result, original_exception = future.result, future.exception

    def result(timeout: float | None = None) -> Any:
        original_exception(timeout)  # waits; raises CancelledError/TimeoutError like result()
        absorb(future)
        return original_result()

    def exception(timeout: float | None = None) -> Any:
        original_exception(timeout)
        absorb(future)
        return original_exception()

    future.result = result
    future.exception = exception
    future.add_done_callback(absorb)
    return future


# ── multiprocessing.Pool (and ThreadPool) ─────────────────────────────────────
# Same idea as ProcessPoolExecutor: while a trace is open, the function handed
# to apply/map/starmap/imap (and their _async forms) is sent as a _RemoteCall
# that traces it in the worker and ships the subtree back. A Pool delivers
# results through its own result objects (on its result-handler thread), so
# the graft target is registered under a token BEFORE the task is queued and
# travels with the call -- a worker that finishes before submit() returns can't
# race past it. Every outcome is unwrapped before the program can see it.

_pool_grafts: dict = {}  # token -> (parent node, value pool, trace state)
_pool_tokens = itertools.count(1)
_original_pool_methods: dict = {}
_original_pool_sets: dict = {}


def _pool_hook(original: Any) -> Any:
    def hooked(self: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        recording = _recording()
        if recording is None or _skip_depth.get():
            return original(self, func, *args, **kwargs)
        parent, state = recording
        token = next(_pool_tokens)  # an int: survives the pickle round trip to the worker
        _pool_grafts[token] = (parent, _current_pool.get(), state)
        state.graft_tokens.append(token)
        return original(self, _RemoteCall(func, _remote_name(func), _remote_config(), token), *args, **kwargs)

    return hooked


def _graft_outcome(outcome: "_RemoteOutcome") -> Any:
    entry = _pool_grafts.get(outcome.token)
    if entry is not None:
        parent, pool, state = entry
        with state.lock:
            if state.open:
                if pool is not None:
                    pool.variables.update(outcome.variables)
                state.sources.update(outcome.sources)
                parent["children"].extend(outcome.children)
    return outcome.result


def _unwrap_pool_result(obj: Any) -> Any:
    """``(success, value)`` as a Pool worker reported it, with every
    _RemoteOutcome grafted and replaced by the real result."""
    try:
        success, value = obj
    except (TypeError, ValueError):
        return obj
    if success:
        if isinstance(value, _RemoteOutcome):
            value = _graft_outcome(value)
        elif isinstance(value, list) and any(isinstance(v, _RemoteOutcome) for v in value):
            value = [_graft_outcome(v) if isinstance(v, _RemoteOutcome) else v for v in value]
    else:
        outcome = getattr(value, "__dict__", {}).pop("_perf_remote", None)
        if isinstance(outcome, _RemoteOutcome):
            _graft_outcome(outcome)
    return (success, value)


def _pool_set_hook(original: Any) -> Any:
    def _set(self: Any, i: Any, obj: Any) -> Any:
        return original(self, i, _unwrap_pool_result(obj))

    return _set


def _install_pool_hooks() -> None:
    from multiprocessing import pool as mp_pool

    for name in ("apply_async", "_map_async", "imap", "imap_unordered"):
        original = getattr(mp_pool.Pool, name)
        _original_pool_methods[name] = original
        setattr(mp_pool.Pool, name, _pool_hook(original))
    for cls in (mp_pool.ApplyResult, mp_pool.MapResult, mp_pool.IMapIterator, mp_pool.IMapUnorderedIterator):
        if "_set" in vars(cls):
            _original_pool_sets[cls] = vars(cls)["_set"]
            cls._set = _pool_set_hook(vars(cls)["_set"])


# ── threads: carry the trace onto other threads ───────────────────────────────
# A new thread starts with an empty context, so before these hooks every call
# made on another thread was silently dropped. While a trace is open:
#
# * ThreadPoolExecutor.submit (and so .map and loop.run_in_executor) runs each
#   JOB in a copy of the submitter's context. The pool's worker THREADS never
#   inherit a trace -- they are long-lived and also run jobs submitted later,
#   from outside the trace or from another trace, which must not be recorded
#   into this one.
# * Thread.start gives the new thread a copy of the starter's context -- but
#   only for a thread whose entry point is project code (the same rule as for
#   instrumentation). Library/stdlib-internal threads (executor workers,
#   connection-pool keepalives, ...) are long-lived workers too.
#
# Work that is still running when the trace ends stops being recorded at that
# moment (see _TraceState). A thread started during a trace keeps a reference
# to that trace's data until the thread itself exits.

_original_thread_submit: Any = None
_original_thread_start: Any = None


def _traced_thread_submit(self: Any, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Replacement for ``ThreadPoolExecutor.submit``: inside an open trace,
    the job runs in a copy of the submitter's context."""
    if _recording() is None:
        return _original_thread_submit(self, fn, *args, **kwargs)
    return _original_thread_submit(self, contextvars.copy_context().run, fn, *args, **kwargs)


def _thread_entry(thread: Any) -> Any:
    """The callable a Thread will run: its target, a Timer's function, or an
    overridden ``run``."""
    for attr in ("_target", "function"):  # Thread(target=...), threading.Timer
        entry = getattr(thread, attr, None)
        if entry is not None:
            return entry
    run = type(thread).run
    return None if run is threading.Thread.run else run


def _is_project_callable(fn: Any) -> bool:
    while isinstance(fn, partial):
        fn = fn.func
    fn = getattr(fn, "__func__", fn)
    module = sys.modules.get(getattr(fn, "__module__", None) or "")
    return module is not None and module.__name__ != __name__ and _is_project_module(module)


def _traced_thread_start(self: Any) -> Any:
    """Replacement for ``Thread.start``: inside an open trace, a thread whose
    entry point is project code runs in a copy of the starter's context."""
    if _recording() is not None and _is_project_callable(_thread_entry(self)):
        ctx = contextvars.copy_context()
        run = self.run
        self.run = lambda: ctx.run(run)
    return _original_thread_start(self)


# ── unwrapped project functions: nodes from sys.monitoring events (3.12+) ────
# Instrumentation by rebinding names can't reach a function nobody looks up
# by name: nested defs, closures, lambdas, functions held in dicts / registries
# / instance attributes since before the trace, or swapped in mid-trace. On
# Python 3.12+, PY_START / PY_RETURN / PY_UNWIND events give such calls nodes
# too (same capture, timing and naming as wrapped calls):
#
# * only plain project functions -- not generator/coroutine bodies (their
#   suspension can't be tracked from these events), class bodies, or dunders;
#   events for everything else are switched off per code object (DISABLE), so
#   library code costs nothing after its first call;
# * a call made directly by one of perf's own wrappers is skipped (the wrapper
#   already records it), as is a same-named call directly under a wrapped call
#   (the original function behind a decorator);
# * events are only enabled while at least one trace is running.
#
# SUPPRESS_ERRORS can't apply to these calls (an event can't change what the
# function does); their failures are recorded and propagate as usual.

_NODE_TOOL_ID = 4
_events_available = hasattr(sys, "monitoring")
_events_users = 0
_events_lock = threading.Lock()
_events_ready = False
_event_names: dict = {}  # code object -> node name, or None (not traced)
_event_tls = threading.local()  # .stack: [(id(frame), node, token, state)]
_WRAPPER_CODES: set = set()
# node name -> code objects that node's call already covers: the layers of a
# decorated function (and the original behind them), found via __wrapped__
# and closure cells when perf wraps it. Calls of those codes made directly
# under such a node are folded into it instead of becoming nested nodes.
_COVERED_CODES: dict = {}


def _cover(func: Any, name: str) -> None:
    codes, todo, seen = set(), [func], set()
    while todo and len(seen) < 16:
        f = todo.pop()
        if id(f) in seen:
            continue
        seen.add(id(f))
        f = getattr(f, "__func__", f)
        code = getattr(f, "__code__", None)
        if code is not None:
            codes.add(code)
        inner = getattr(f, "__wrapped__", None)
        if inner is not None:
            todo.append(inner)
        for cell in getattr(f, "__closure__", None) or ():
            try:
                content = cell.cell_contents
            except ValueError:  # empty cell
                continue
            if inspect.isfunction(content) or inspect.ismethod(content):
                todo.append(content)
    if codes:
        _COVERED_CODES[name] = _COVERED_CODES.get(name, frozenset()) | frozenset(codes)
_CO_SKIP = 0x20 | 0x80 | 0x100 | 0x200  # GENERATOR | COROUTINE | ITERABLE_COROUTINE | ASYNC_GENERATOR
_CO_OPTIMIZED = 0x1


def _event_name(code: Any, frame: Any) -> Any:
    """Node name for a call of `code`, or None if it shouldn't get one."""
    name = code.co_name
    if not code.co_flags & _CO_OPTIMIZED or code.co_flags & _CO_SKIP or (name.startswith("__") and name.endswith("__")):
        return None
    module_name = frame.f_globals.get("__name__")
    module = sys.modules.get(module_name) if module_name else None
    if module is None or module_name == __name__ or not _is_project_module(module):
        return None
    qualname = getattr(code, "co_qualname", name)
    local = qualname.rsplit("<locals>.", 1)[-1]
    if "." in local and not local.startswith("<"):
        return local  # a method: `Class.method`, like wrapped methods
    return f"{_module_label(module_name)}.{qualname}"


def _on_event_start(code: Any, offset: int) -> Any:
    # Fast exits first: these events are process-wide, so they also fire for
    # every project call on threads that aren't recording anything.
    name = _event_names.get(code, _SKIP)
    if name is None:
        return sys.monitoring.DISABLE
    if name is not _SKIP and _current_node.get() is None:
        return None
    _hook_enter()
    try:
        return _on_event_start_body(code, offset)
    finally:
        _hook_exit()


def _on_event_start_body(code: Any, offset: int) -> Any:
    try:
        name = _event_names.get(code, _SKIP)
        frame = sys._getframe(2)  # the monitored function (callback → body → here)
        if name is _SKIP:
            name = _event_names[code] = _event_name(code, frame)
        if name is None:
            return sys.monitoring.DISABLE
        caller = frame.f_back
        if caller is not None and caller.f_code in _WRAPPER_CODES:
            return None  # perf's wrapper records this call
        recording = _recording()
        if recording is None or _skip_depth.get() or _is_skipped(name):
            return None
        parent, state = recording
        stack = getattr(_event_tls, "stack", None)
        if stack is None:
            stack = _event_tls.stack = []
        if not (stack and stack[-1][1] is parent) and (
            parent["name"] == name or code in _COVERED_CODES.get(parent["name"], ())
        ):
            return None  # a decorator layer / the original behind a wrapped call
        node = make_node(name)
        _record_frame_inputs(node, code, frame)
        if not state.attach(parent, node):
            return None
        stack.append((id(frame), node, _current_node.set(node), state))
        if _locals_users and _locals_wanted(name):
            _locals_enable(code, node)
        _restart(node)
    except Exception:
        pass  # instrumentation must never break the program
    return None


def _event_end(code: Any, value: Any, failed: bool) -> Any:
    if _current_node.get() is None:
        # Not recording on this thread, so this frame can't have a node open:
        # _on_event_start made that node current until this very return.
        return sys.monitoring.DISABLE if _event_names.get(code, _SKIP) is None else None
    _hook_enter()
    try:
        return _event_end_body(code, value, failed)
    finally:
        _hook_exit()


def _event_end_body(code: Any, value: Any, failed: bool) -> Any:
    try:
        name = _event_names.get(code, _SKIP)
        if name is None:
            return sys.monitoring.DISABLE
        stack = getattr(_event_tls, "stack", None)
        # sys._getframe(3): the monitored function (callback → _event_end → body → here)
        if not stack or stack[-1][0] != id(sys._getframe(3)):
            return None
        _, node, token, state = stack.pop()
        done = _completion(node)
        if failed:
            _record_exception(done, value)
        else:
            _record_output(done, value)
        state.complete(node, done)
        _current_node.reset(token)
    except Exception:
        pass
    return None


def _on_event_return(code: Any, offset: int, retval: Any) -> Any:
    return _event_end(code, retval, False)


def _on_event_unwind(code: Any, offset: int, exc: BaseException) -> Any:
    _event_end(code, exc, True)
    return None  # DISABLE isn't allowed for PY_UNWIND


def _record_frame_inputs(node: dict, code: Any, frame: Any) -> None:
    """The call's arguments, read from its frame before its body runs:
    positional(-or-keyword) parameters in order (then *args) as `args`,
    keyword-only parameters (then **kwargs) as `kwargs`."""
    names = code.co_varnames
    f_locals = frame.f_locals
    n_pos, n_kw = code.co_argcount, code.co_kwonlyargcount
    positional = names[:n_pos]
    args = [f_locals[p] for p in positional if p in f_locals]
    kwargs = {k: f_locals[k] for k in names[n_pos:n_pos + n_kw] if k in f_locals}
    i = n_pos + n_kw
    if code.co_flags & 0x04:  # *args
        args.extend(f_locals.get(names[i], ()))
        i += 1
    if code.co_flags & 0x08:  # **kwargs
        kwargs.update(f_locals.get(names[i], {}))
    _record_inputs(node, tuple(args), kwargs, positional)


def _events_acquire() -> None:
    """Enable the node events while a trace runs (refcounted across traces)."""
    global _events_users, _events_ready
    if not _events_available:
        return
    with _events_lock:
        if not _events_ready:
            m = sys.monitoring
            try:
                m.use_tool_id(_NODE_TOOL_ID, "perf-nodes")
            except ValueError:
                return  # slot taken by another tool: just no event nodes
            m.register_callback(_NODE_TOOL_ID, m.events.PY_START, _on_event_start)
            m.register_callback(_NODE_TOOL_ID, m.events.PY_RETURN, _on_event_return)
            m.register_callback(_NODE_TOOL_ID, m.events.PY_UNWIND, _on_event_unwind)
            _events_ready = True
        _events_users += 1
        if _events_users == 1:
            m = sys.monitoring
            m.set_events(_NODE_TOOL_ID, m.events.PY_START | m.events.PY_RETURN | m.events.PY_UNWIND)


def _events_release() -> None:
    global _events_users
    if not _events_available or not _events_ready:
        return
    with _events_lock:
        _events_users -= 1
        if _events_users == 0:
            sys.monitoring.set_events(_NODE_TOOL_ID, 0)


def _events_config_changed() -> None:
    """Project-ness may have changed (SCAN_VENV …): re-decide every code object."""
    _event_names.clear()
    if _events_available and _events_ready:
        sys.monitoring.restart_events()


# ── local variables, line by line (TRACE_LOCALS, Python 3.12+) ───────────────
# Line events are switched on only for the code objects of functions matching
# TRACE_LOCALS (sys.monitoring.set_local_events), and only while a trace that
# asks for them is running. A LINE event fires BEFORE a line runs, so the
# locals that changed since the previous event were changed by the previous
# line: each event diffs the frame's locals against the last snapshot and
# attributes the difference to that line. A value is "changed" if it was
# rebound (different object), or -- for mutable objects -- if its captured
# form differs from last time (in-place mutation). The final diff runs at
# PY_RETURN / PY_UNWIND, and the list is attached to the call's node then.

_LOCALS_TOOL_ID = 3
_locals_ready = False
_locals_users = 0  # traces currently running with TRACE_LOCALS set
_locals_codes: set = set()  # code objects with line events switched on
_locals_frames: dict = {}  # id(frame) -> _LocalsState
# id(node) -> code object: the function a just-created node is for. A frame is
# only tracked for the node made for ITS function -- never for whatever node
# happens to be current (e.g. its caller's, when it was called without a node).
_locals_expected: dict = {}
_IMMUTABLE_TYPES = (int, float, complex, str, bytes, bool, type(None), frozenset, range)


class _LocalsState:
    __slots__ = ("frame", "node", "state", "snapshot", "global_names", "global_snapshot", "last_line", "events",
                 "dropped", "limit", "overhead", "source", "based", "arg_based", "params", "params_used",
                 "mutating_lines")

    def __init__(self, frame: Any, node: dict, state: "_TraceState") -> None:
        self.frame, self.node, self.state = frame, node, state
        self.snapshot: dict = {}  # name -> (value, comparable form or None, fingerprint or None)
        # Names whose latest entry in this call holds their full content, so a
        # later in-place mutation can be recorded as "changed" ops against it.
        self.based: set = set()
        # Arguments whose base is still the node's recorded "args"/"kwargs"
        # value (no entry of their own yet), and the positional parameter
        # names a reader needs to find it -- written as the node's "params"
        # once an entry is recorded against one (see _locals_arg_bases).
        self.arg_based: set = set()
        self.params: tuple = ()
        self.params_used = False
        self.global_names = _stored_globals(frame.f_code)  # `global x` names this function assigns
        self.global_snapshot: dict = {}
        self.last_line = None
        self.events: list = []
        self.dropped = 0
        self.limit = _cfg.MAX_LOCALS_EVENTS
        self.overhead = 0.0
        self.source: "str | None" = None  # id of this function's file in the trace's sources table
        self.mutating_lines = _mutating_lines(frame.f_code)  # looked up once per call, not per line


_FINGERPRINT_TYPES = frozenset({list, dict, set, bytearray})


def _locals_fingerprint(value: Any) -> "bytes | None":
    """A C-speed stand-in for comparing a builtin container's content between
    lines: marshal's bytes for a list/dict/set/bytearray holding only builtin
    values (None if it holds anything else, e.g. a project object). Equal
    bytes mean equal content -- and so an equal comparable form; different
    bytes (which can also come from a change in sharing between its parts)
    only mean the comparable form must be looked at, which still decides."""
    if type(value) not in _FINGERPRINT_TYPES:
        return None
    try:
        return marshal.dumps(value)
    except Exception:  # ValueError: unmarshallable content; too deeply nested
        return None


def _locals_comparable(value: Any, memo: "dict | None" = None) -> Any:
    """What a mutable value is compared by between lines (None for immutables,
    which can only change by rebinding). `memo`, shared by every value
    compared at one line, converts an object reachable from several of them
    (self, and a local holding self.items, …) once for all."""
    if isinstance(value, _IMMUTABLE_TYPES):
        return None
    # Full conversion, no size budget: a budget-truncated form would hide a
    # change past the cut-off (MAX_SIZE_CAPTURE only limits what is STORED).
    token = _comparing.set(1)
    try:
        return _jsonable(value, _memo=memo if memo is not None else {"circular": 0})
    finally:
        _comparing.reset(token)


# Instructions that can change an object in place, or let other code run
# that can: calls (a with-statement's __enter__/__exit__ included), stores
# and deletes of attributes/items, in-place operators, iterating (a
# generator's body runs), and suspending (yield/await: other code runs
# meanwhile). After a line with none of them only rebinding is possible, so
# its locals are compared by identity alone. Not covered: user code run
# implicitly by an attribute read (a property) or an operator (__eq__,
# __getitem__) -- a change it makes is recorded at the next line that has
# one of these instead.
_MUTATING_OPS = frozenset({
    "CALL", "CALL_KW", "CALL_FUNCTION_EX", "STORE_ATTR", "STORE_SUBSCR", "STORE_SLICE", "DELETE_ATTR",
    "DELETE_SUBSCR", "FOR_ITER", "SEND", "YIELD_VALUE", "GET_AWAITABLE", "GET_ANEXT", "BEFORE_WITH",
    "BEFORE_ASYNC_WITH", "WITH_EXCEPT_START", "IMPORT_NAME", "IMPORT_FROM",
})
_ALL_LINES = None  # _mutating_lines verdict: every line may mutate (analysis failed)
_mutating_lines_cache: dict = {}


def _mutating_lines(code: Any) -> "frozenset | None":
    """The lines of `code` that contain an instruction from _MUTATING_OPS (or
    an in-place operator); None when that can't be determined."""
    try:
        return _mutating_lines_cache[code]
    except KeyError:
        pass
    lines: "set | None" = set()
    try:
        import dis

        for ins in dis.get_instructions(code):
            name = ins.opname.removeprefix("INSTRUMENTED_")
            if name in _MUTATING_OPS or (name == "BINARY_OP" and ins.argrepr.endswith("=")):
                line = ins.positions.lineno if ins.positions else None
                if line is not None:  # unnumbered: exception plumbing, no user code
                    lines.add(line)
    except Exception:
        lines = _ALL_LINES
    verdict = frozenset(lines) if lines is not None else _ALL_LINES
    _mutating_lines_cache[code] = verdict
    return verdict


def _line_may_mutate(code: Any, line: "int | None") -> bool:
    lines = _mutating_lines(code)
    return lines is _ALL_LINES or line is None or line in lines


def _locals_read(frame: Any) -> dict:
    return {k: v for k, v in frame.f_locals.items() if k.isidentifier()}


def _globals_read(st: "_LocalsState", frame: Any) -> dict:
    g = frame.f_globals
    return {name: g[name] for name in st.global_names if name in g}


_stored_globals_cache: dict = {}


def _stored_globals(code: Any) -> tuple:
    """Names the function assigns or deletes through a `global` statement."""
    names = _stored_globals_cache.get(code)
    if names is None:
        import dis

        names = tuple(sorted({i.argval for i in dis.get_instructions(code) if i.opname in ("STORE_GLOBAL", "DELETE_GLOBAL")}))
        _stored_globals_cache[code] = names
    return names


def _changes(snapshot: dict, current: dict, content: bool = True) -> tuple:
    """(changed {name: value}, deleted [names], mutated {name: (old, new)},
    reusable {name: converted}) between a snapshot and now; updates the
    snapshot. `reusable` holds changed values' comparison forms that are also
    their exact capture (see _capture_locals). `mutated` holds the
    changed names still bound to the SAME object (changed in place), with
    their comparable forms before and after. With `content` False (the line
    that ran can't have changed anything in place, see _MUTATING_OPS) a name
    still bound to the same object is unchanged without comparing it."""
    changed: dict = {}
    mutated: dict = {}
    memo: dict = {"circular": 0}
    for name, value in current.items():
        previous = snapshot.get(name)
        fingerprint = None
        if previous is not None and previous[0] is value:
            if previous[1] is None or not content:
                continue  # same immutable object, or nothing could change it: unchanged
            if previous[2] is not None:
                fingerprint = _locals_fingerprint(value)
                if fingerprint == previous[2]:
                    continue  # identical content, so an identical comparable form: unchanged
            comparable = _locals_comparable(value, memo)
            if comparable == previous[1]:
                if fingerprint is not None:  # don't miss the fast path on the next line
                    snapshot[name] = (value, previous[1], fingerprint)
                continue
            mutated[name] = (previous[1], comparable)
        else:
            comparable = _locals_comparable(value, memo)
        if comparable is not None and fingerprint is None:
            fingerprint = _locals_fingerprint(value)
        snapshot[name] = (value, comparable, fingerprint)
        changed[name] = value
    deleted = [name for name in snapshot if name not in current]
    for name in deleted:
        del snapshot[name]
    # When no repr was shortened for comparing, each comparable form is
    # exactly what capturing the value would store, so it can be stored as is.
    reusable = {} if memo.get("shortened") else {name: snapshot[name][1] for name in changed}
    return changed, deleted, mutated, reusable


def _capture_named(values: dict) -> dict:
    captured = {}
    for name, value in values.items():
        if _is_skipped_variable(name):
            captured[name] = f"<skipped:{name}>"
            continue
        jv = _capture_value(value)
        captured[name] = f"<{type(value).__name__}>" if jv is _SKIP else jv
    return captured


def _capture_locals(st: "_LocalsState", changed: dict, mutated: dict, reusable: "dict | None" = None) -> tuple:
    """The "set" and "changed" parts of a TRACE_LOCALS entry: a value changed
    in place whose previous state this call already recorded in full is
    stored as edits to it (when they are smaller); anything else in full. An
    argument's first edits are against its recorded value in the node's
    "args"/"kwargs" (see _locals_arg_bases)."""
    sets: dict = {}
    deltas: dict = {}
    for name, value in changed.items():
        arg_based = name in st.arg_based
        st.arg_based.discard(name)  # from here on, its base is this entry
        if _is_skipped_variable(name):
            sets[name] = f"<skipped:{name}>"
            st.based.discard(name)
            continue
        if name in mutated and (name in st.based or (arg_based and _arg_base_matches(st, name, mutated[name][0]))):
            ops = _locals_delta(*mutated[name])
            if ops is not None:
                deltas[name] = ops
                st.based.add(name)  # an argument's base until now was its recorded value
                st.params_used = st.params_used or arg_based
                continue
        t = type(value)
        if (t is int or t is float or t is bool or value is None) and _cfg.MAX_SIZE_CAPTURE is None:
            sets[name] = value  # what _jsonable + _intern would give, without the walk
            st.based.add(name)
            continue
        jv = (reusable or {}).get(name)
        if jv is not None and jv is not _SKIP and _cfg.MAX_SIZE_CAPTURE is None:
            inline = True  # already converted for comparing (no size limit to apply)
        else:
            jv, inline = _capture_value_inline(value)
        if inline:
            sets[name] = _intern(jv)
            st.based.add(name)
        else:
            sets[name] = f"<{type(value).__name__}>" if jv is _SKIP else jv
            st.based.discard(name)
    return sets, deltas


def _locals_delta(old: Any, new: Any) -> "list | None":
    """Edit ops turning the converted value `old` into `new` (see TRACE_LOCALS),
    or None when they would not be smaller than `new` itself (or can't be
    expressed, or exceed MAX_SIZE_CAPTURE)."""
    if old is _SKIP or new is _SKIP:
        return None
    ops: list = []
    if not _delta_ops(old, new, [], ops) or not ops:
        return None
    size = len(_json.dumps(ops, ensure_ascii=False, separators=(",", ":"), default=str))
    if not _json_size_exceeds(new, size):
        return None
    if _cfg.MAX_SIZE_CAPTURE is not None and size >= _cfg.MAX_SIZE_CAPTURE:
        return None
    for op in ops:
        if len(op) == 3:
            op[2] = _intern(op[2])
    return ops


def _json_size_exceeds(value: Any, limit: int) -> bool:
    """Whether `value`'s compact JSON is longer than `limit` characters --
    walked only until that is known (serializing a large value in full to
    compare it with a few bytes of edits would cost more than the edits save).
    Approximate where it doesn't matter: string escapes aren't counted."""
    budget = [limit]

    def walk(v: Any) -> bool:  # True once the budget is exhausted
        t = type(v)
        if t is str:
            budget[0] -= len(v) + 2
        elif t is dict:
            budget[0] -= 1 + len(v)  # braces, commas, colons
            for k, x in v.items():
                budget[0] -= len(k) + 3 if type(k) is str else 3
                if budget[0] < 0 or walk(x):
                    return True
        elif t is list:
            budget[0] -= 1 + len(v)
            for x in v:
                if budget[0] < 0 or walk(x):
                    return True
        else:
            budget[0] -= len(_json.dumps(v, default=str))
        return budget[0] < 0

    return walk(value)


def _delta_ops(old: Any, new: Any, path: list, ops: list) -> bool:
    """Append to `ops` the edits turning `old` into `new` at `path`; False if
    they can't be expressed as edits (the caller then sets the whole value)."""
    if type(old) is list and type(new) is list:
        n_old, n_new = len(old), len(new)
        common = min(n_old, n_new)
        if n_old != n_new and not all(map(_same_converted, old[:common], new[:common])):
            return False  # not a pure append/truncate: e.g. insert(0, x), remove()
        if n_new > n_old:
            ops.append(["+", path, new[n_old:]])
        elif n_new < n_old:
            ops.extend(["-", [*path, i]] for i in range(n_old - 1, n_new - 1, -1))
        else:
            for i, (a, b) in enumerate(zip(old, new)):
                if not _same_converted(a, b) and not _delta_ops(a, b, [*path, i], ops):
                    ops.append(["=", [*path, i], b])
        return True
    if type(old) is dict and type(new) is dict:
        for k, v in new.items():
            if k not in old:
                ops.append(["=", [*path, k], v])
            elif not _same_converted(old[k], v) and not _delta_ops(old[k], v, [*path, k], ops):
                ops.append(["=", [*path, k], v])
        ops.extend(["-", [*path, k]] for k in old if k not in new)
        return True
    return False


def _same_converted(a: Any, b: Any) -> bool:
    """Equality of converted values that also tells 1, 1.0 and True apart (==
    doesn't, and an edit skipped on that basis would replay the wrong value).
    The C-level == rejects a different value at once; only equal ones are
    walked, to check that their leaves have the same types too."""
    if type(a) is not type(b) or a != b:
        return False
    t = type(a)
    if t is dict:
        for k, v in a.items():
            w = b[k]
            tv = type(v)
            if tv is not type(w) or ((tv is dict or tv is list) and not _same_types(v, w)):
                return False
        return True
    if t is list:
        for v, w in zip(a, b):
            tv = type(v)
            if tv is not type(w) or ((tv is dict or tv is list) and not _same_types(v, w)):
                return False
    return True


def _same_types(a: Any, b: Any) -> bool:
    """For two values already known to be ==: whether their leaves' types match too."""
    if type(a) is dict:
        for k, v in a.items():
            w = b[k]
            tv = type(v)
            if tv is not type(w) or ((tv is dict or tv is list) and not _same_types(v, w)):
                return False
        return True
    for v, w in zip(a, b):
        tv = type(v)
        if tv is not type(w) or ((tv is dict or tv is list) and not _same_types(v, w)):
            return False
    return True


def _locals_diff(st: "_LocalsState", frame: Any) -> None:
    lines, line = st.mutating_lines, st.last_line
    content = lines is _ALL_LINES or line is None or line in lines  # _line_may_mutate
    changed, deleted, mutated, reusable = _changes(st.snapshot, _locals_read(frame), content)
    g_changed, g_deleted, _, _ = (_changes(st.global_snapshot, _globals_read(st, frame), content)
                                  if st.global_names else ({}, [], {}, {}))
    st.based.difference_update(deleted)
    st.arg_based.difference_update(deleted)
    if not (changed or deleted or g_changed or g_deleted):
        return
    if len(st.events) >= st.limit:
        st.dropped += 1
        return
    filename = frame.f_code.co_filename
    if st.last_line:
        # Once per line per trace: statement text, its redaction, the lock.
        key = (filename, st.last_line)
        sid = st.state.line_sources.get(key)
        if sid is None:
            text = _statement_text(filename, st.last_line)
            sid = st.state.source_line(filename, st.last_line, _redact(text)) if text else ""
            if sid is not None:
                st.state.line_sources[key] = sid
        if sid:
            st.source = sid
    sets, deltas = _capture_locals(st, changed, mutated, reusable) if changed else ({}, {})
    # Positional: [line, set, changed, deleted, globals, deleted_globals],
    # 0 for an empty part, trailing empty parts left out (see TRACE_LOCALS).
    event = [st.last_line, sets or 0, deltas or 0, deleted or 0,
             _capture_named(g_changed) if g_changed else 0, g_deleted or 0]
    while event[-1] == 0:
        event.pop()
    st.events.append(event)


_statement_spans: dict = {}  # filename -> {first line: last line of that statement's own text}


def _statement_text(filename: str, line: int) -> str:
    """Source of the statement starting at `line` -- all of its lines for a
    multi-line statement, the header only for a compound one (if/for/with…)."""
    spans = _statement_spans.get(filename)
    if spans is None:
        spans = {}
        try:
            import ast

            tree = ast.parse("".join(linecache.getlines(filename)))
            for node in ast.walk(tree):
                if isinstance(node, ast.stmt):
                    end = node.end_lineno or node.lineno
                    body = getattr(node, "body", None)
                    if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                        end = max(node.lineno, body[0].lineno - 1)
                    spans[node.lineno] = max(spans.get(node.lineno, node.lineno), end)
        except Exception:
            pass
        _statement_spans[filename] = spans
    end = spans.get(line, line)
    lines = [linecache.getline(filename, n).strip() for n in range(line, min(end, line + 50) + 1)]
    text = " ".join(part for part in lines if part)
    return text if len(text) <= 500 else text[:497] + "..."


def _locals_begin(code: Any, frame: Any) -> "_LocalsState | None":
    """Start tracking a frame of a TRACE_LOCALS function, if it belongs to the
    node that is current right now (the call perf just recorded)."""
    recording = _recording()
    if recording is None:
        return None
    node, state = recording
    if not _locals_wanted(node["name"]):
        return None
    if _locals_expected.get(id(node)) is not code:
        return None  # this frame isn't the call the current node was made for
    del _locals_expected[id(node)]
    st = _LocalsState(frame, node, state)
    token = _capturing.set(1)  # converting values may run user code (__repr__)
    try:
        memo: dict = {"circular": 0}
        for name, value in _locals_read(frame).items():  # the arguments: baseline, not events
            comparable = _locals_comparable(value, memo)
            st.snapshot[name] = (value, comparable, None if comparable is None else _locals_fingerprint(value))
        for name, value in _globals_read(st, frame).items():
            comparable = _locals_comparable(value, memo)
            st.global_snapshot[name] = (value, comparable, None if comparable is None else _locals_fingerprint(value))
        _locals_arg_bases(st, frame)
    finally:
        _capturing.reset(token)
    _locals_frames[id(frame)] = st
    return st


def _locals_arg_bases(st: "_LocalsState", frame: Any) -> None:
    """Note which arguments may have their first in-place change recorded as
    edits against the value already in the node's "args"/"kwargs" instead of
    in full. Checked lazily, at that first change (_arg_base_matches): most
    arguments are never mutated, and checking costs a walk of the value. A
    reader finds that value as args[params.index(name)] when the name is in
    the node's "params" (positional parameter names) and that index is within
    "args", else as kwargs[name]."""
    node = st.node
    if node.get("args") is None and not node.get("kwargs"):
        return  # CAPTURE_IO off, or no arguments
    code = frame.f_code
    st.params = code.co_varnames[: code.co_argcount]
    kwonly = code.co_varnames[code.co_argcount : code.co_argcount + code.co_kwonlyargcount]
    for name in (*st.params, *kwonly):
        entry = st.snapshot.get(name)
        if entry is not None and entry[1] is not None and not _is_skipped_variable(name):
            st.arg_based.add(name)  # bound and mutable (an immutable one can only be rebound)


def _arg_base_matches(st: "_LocalsState", name: str, comparable: Any) -> bool:
    """Whether argument `name`'s recorded value in the node is exactly
    `comparable` -- its state from the call's start until now (not a
    skipped/oversized marker, a spilled value, …)."""
    node = st.node
    args, kwargs = node.get("args") or [], node.get("kwargs") or {}
    i = st.params.index(name) if name in st.params else len(args)
    if i < len(args):
        recorded = args[i]
    elif name in kwargs:
        recorded = kwargs[name]
    else:
        return False
    pool = _current_pool.get()
    return _same_recorded(recorded, comparable, pool.variables if pool is not None else {})


def _same_recorded(recorded: Any, comparable: Any, variables: dict, depth: int = 0) -> bool:
    """:func:`_same_converted` between a RECORDED value (which may hold
    {"__ref__"} pool references) and a comparison form (whose address-bearing
    reprs are shortened, see _locals_comparable): such reprs match by address."""
    if depth > 100:
        return False
    if type(recorded) is dict and "__ref__" in recorded:
        target = variables.get(recorded["__ref__"])
        if len(recorded) > 1:  # {"__type__": T, "__ref__": r}: r is the object's pooled fields
            if type(target) is not dict:
                return False
            target = {**{k: v for k, v in recorded.items() if k != "__ref__"}, **target}
        recorded = target
    if type(recorded) is not type(comparable):
        return False
    if type(recorded) is dict:
        return len(recorded) == len(comparable) and all(
            k in comparable and _same_recorded(v, comparable[k], variables, depth + 1) for k, v in recorded.items())
    if type(recorded) is list:
        return len(recorded) == len(comparable) and all(
            _same_recorded(a, b, variables, depth + 1) for a, b in zip(recorded, comparable))
    if recorded == comparable:
        return True
    if type(recorded) is str:
        a, b = _ADDRESS_REPR.match(recorded), _ADDRESS_REPR.match(comparable)
        return bool(a and b and a.group(1) == b.group(1))
    return False


def _resolve_pooled(value: Any, variables: dict, depth: int = 0) -> Any:
    """`value` with its {"__ref__"} references into the trace's pool expanded."""
    if depth > 100:
        return value
    if type(value) is dict:
        if "__ref__" in value:
            target = _resolve_pooled(variables.get(value["__ref__"], value), variables, depth + 1)
            rest = {k: _resolve_pooled(v, variables, depth + 1) for k, v in value.items() if k != "__ref__"}
            return {**rest, **target} if rest and isinstance(target, dict) else target
        return {k: _resolve_pooled(v, variables, depth + 1) for k, v in value.items()}
    if type(value) is list:
        return [_resolve_pooled(v, variables, depth + 1) for v in value]
    return value


def _on_locals_line(code: Any, line: int) -> Any:
    _hook_enter()
    try:
        result = _on_locals_line_body(code, line)
    finally:
        _hook_exit()
    if _timeout_pending:
        # A safe point for a TIMEOUT that fired inside perf's own code: this
        # callback's work is complete, and an exception raised here surfaces
        # in the monitored function at this line (a tight loop with no traced
        # calls would otherwise never reach a wrapper's safe point).
        _raise_pending_timeout()
    return result


def _on_locals_line_body(code: Any, line: int) -> Any:
    try:
        frame = sys._getframe(2)  # the monitored function (callback → body → here)
        st = _locals_frames.get(id(frame))
        if st is None or st.frame is not frame:
            if _capturing.get():
                return None  # a line of user code run by perf's own capture
            st = _locals_begin(code, frame)
            if st is not None:
                st.last_line = line
            return None
        t0 = perf_counter()
        token = _capturing.set(1)
        try:
            _locals_diff(st, frame)
        finally:
            _capturing.reset(token)
        st.last_line = line
        st.overhead += perf_counter() - t0
    except Exception:
        pass  # instrumentation must never break the program
    return None


def _on_locals_end(code: Any, offset: int, value: Any) -> Any:
    _hook_enter()
    try:
        return _on_locals_end_body(code, offset, value)
    finally:
        _hook_exit()


def _on_locals_end_body(code: Any, offset: int, value: Any) -> Any:
    token = _capturing.set(1)
    try:
        frame = sys._getframe(2)  # the monitored function (callback → body → here)
        st = _locals_frames.pop(id(frame), None)
        if st is None or st.frame is not frame:
            return None
        t0 = perf_counter()
        _locals_diff(st, frame)
        st.overhead += perf_counter() - t0
        fields: dict = {"locals": st.events, "locals_capture_time": round(st.overhead, _TIME_DECIMALS)}
        if st.source is not None:
            fields["source"] = st.source
        if st.dropped:
            fields["locals_dropped"] = st.dropped
        if st.params_used:
            fields["params"] = list(st.params)
        st.state.complete(st.node, fields)
    except Exception:
        pass
    finally:
        _capturing.reset(token)
    return None


def _trace_sources() -> dict:
    """The current trace's TRACE_LOCALS source table (empty if none), each
    file's lines in file order."""
    state = _current_trace.get()
    if state is None:
        return {}
    with state.lock:
        return {
            sid: {"file": src["file"], "lines": dict(sorted(src["lines"].items(), key=lambda item: int(item[0])))}
            for sid, src in state.sources.items()
        }


_UNRESOLVED = object()


def expand_locals(trace: dict) -> dict:
    """Rewrite a written trace (the parsed OUTPUT_FILE JSON) so every
    TRACE_LOCALS entry is a readable dict holding full values:
    {"line", "set", "deleted", "globals", "deleted_globals"} (empty parts
    left out). Each "changed" edit list is replayed onto that name's previous
    value in the same call -- its latest entry, or for an argument its value
    in "args"/"kwargs" -- and stored under "set". Values are resolved against
    the trace's "variables" pool (no {"__ref__"} left in them). Changes
    `trace` in place and returns it.

    The rebuilt values SHARE structure: a pooled value is resolved once for
    the whole trace, and an edit copies only the containers on its path, so
    expanding stays close to the trace's own size however many entries
    repeat a large object. Treat them as read-only (copy one before changing it)."""
    variables = trace.get("variables", {})
    resolved_refs: dict = {}

    def resolve(value: Any, depth: int = 0) -> Any:
        if depth > 100:
            return value
        t = type(value)
        if t is dict:
            if "__ref__" in value:
                ref = value["__ref__"]
                target = resolved_refs.get(ref, _UNRESOLVED)
                if target is _UNRESOLVED:
                    if ref not in variables:
                        return value  # not in this trace's pool: left as the reference it is
                    resolved_refs[ref] = target = resolve(variables[ref], depth + 1)
                if len(value) > 1 and type(target) is dict:  # {"__type__": T, "__ref__": r}: r is its fields
                    return {**{k: resolve(v, depth + 1) for k, v in value.items() if k != "__ref__"}, **target}
                return target
            return {k: resolve(v, depth + 1) for k, v in value.items()}
        if t is list:
            return [resolve(v, depth + 1) for v in value]
        return value

    def apply(value: Any, op: list) -> Any:
        """`value` after one edit op, as a new value sharing everything off the op's path."""
        kind, path = op[0], op[1]
        if not path:
            return value + resolve(op[2]) if kind == "+" else value
        root = parent = type(value)(value)  # shallow copies down the path only
        for key in path[:-1]:
            parent[key] = child = type(parent[key])(parent[key])
            parent = child
        last = path[-1]
        if kind == "+":
            parent[last] = parent[last] + resolve(op[2])
        elif kind == "=":
            parent[last] = resolve(op[2])
        elif kind == "-":
            del parent[last]
        return root

    def walk(node: dict) -> Any:
        yield node
        for child in node.get("children", []):
            yield from walk(child)

    keys = ("line", "set", "changed", "deleted", "globals", "deleted_globals")
    for node in walk(trace["trace"]):
        entries = node.get("locals")
        if not entries:
            continue
        params = node.get("params") or []
        args, kwargs = node.get("args") or [], node.get("kwargs") or {}

        def base(name: str) -> Any:
            if name in current:
                return current[name]
            i = params.index(name) if name in params else len(args)
            return resolve(args[i] if i < len(args) else kwargs.get(name))

        current: dict = {}
        expanded = []
        for raw in entries:
            entry = dict(zip(keys, raw)) if isinstance(raw, list) else dict(raw)  # dicts: older traces
            out: dict = {"line": entry["line"]}
            sets: dict = {}
            for name, value in (entry.get("set") or {}).items():
                sets[name] = current[name] = resolve(value)
            for name, ops in (entry.get("changed") or {}).items():
                value = base(name)
                for op in ops:
                    value = apply(value, op)
                sets[name] = current[name] = value
            if sets:
                out["set"] = sets
            for name in entry.get("deleted") or ():
                current.pop(name, None)
            for key in ("deleted", "globals", "deleted_globals"):
                if entry.get(key):
                    out[key] = resolve(entry[key])
            for extra in entry:  # e.g. "text" a reader filled in
                if extra not in keys:
                    out[extra] = entry[extra]
            expanded.append(out)
        node["locals"] = expanded
    return trace


def _locals_wanted(name: str) -> bool:
    patterns = _cfg.TRACE_LOCALS
    return bool(patterns) and any(fnmatch(name, pattern) for pattern in patterns)


def _locals_enable(code: Any, node: dict) -> None:
    """Switch on line events for `code` (a function whose call perf is about
    to record as `node`, and whose name matches TRACE_LOCALS)."""
    if code is None or not _locals_ready:
        return
    _locals_expected[id(node)] = code
    if code in _locals_codes:
        return
    m = sys.monitoring
    # PY_UNWIND can't be a local event: it is enabled globally while any
    # TRACE_LOCALS trace runs (see _locals_acquire).
    m.set_local_events(_LOCALS_TOOL_ID, code, m.events.LINE | m.events.PY_RETURN)
    _locals_codes.add(code)


def _locals_acquire() -> bool:
    """A trace with TRACE_LOCALS starts. Returns whether it holds a reference
    (to pass back to _locals_release)."""
    global _locals_users, _locals_ready
    if not _cfg.TRACE_LOCALS:
        return False
    if not _events_available:
        print("[perf] TRACE_LOCALS needs Python 3.12+ (sys.monitoring): ignored", file=sys.stderr)
        return False
    with _events_lock:
        if not _locals_ready:
            m = sys.monitoring
            try:
                m.use_tool_id(_LOCALS_TOOL_ID, "perf-locals")
            except ValueError:
                print("[perf] TRACE_LOCALS: sys.monitoring tool slot in use: ignored", file=sys.stderr)
                return False
            m.register_callback(_LOCALS_TOOL_ID, m.events.LINE, _on_locals_line)
            m.register_callback(_LOCALS_TOOL_ID, m.events.PY_RETURN, _on_locals_end)
            m.register_callback(_LOCALS_TOOL_ID, m.events.PY_UNWIND, _on_locals_end)
            _locals_ready = True
        _locals_users += 1
        if _locals_users == 1:
            sys.monitoring.set_events(_LOCALS_TOOL_ID, sys.monitoring.events.PY_UNWIND)
    return True


def _locals_release() -> None:
    global _locals_users
    with _events_lock:
        _locals_users -= 1
        if _locals_users == 0:
            sys.monitoring.set_events(_LOCALS_TOOL_ID, 0)
            for code in _locals_codes:
                sys.monitoring.set_local_events(_LOCALS_TOOL_ID, code, 0)
            _locals_codes.clear()
            _locals_expected.clear()


# ── code created during a trace: patch it as it comes into existence ─────────
# Patching at trace start only sees what already exists. A module imported (or
# reloaded) mid-trace, or a class defined while the trace runs (inside a
# function, or in that new module), used to be invisible -- or patched lazily
# on its first call, which by then was already running unwrapped. These hooks
# instrument such code the moment it is created, while a trace is recording.

_original_build_class: Any = None
_original_exec_module: Any = None


def _traced_build_class(func: Any, name: str, *bases: Any, **kwargs: Any) -> Any:
    """Replacement for ``builtins.__build_class__`` (every ``class`` statement)."""
    cls = _original_build_class(func, name, *bases, **kwargs)
    if isinstance(cls, type) and _recording() is not None:
        _hook_enter()
        try:
            _patch_class(cls)
        except Exception:
            pass  # instrumentation must never break class creation
        finally:
            _hook_exit()
    return cls


def _traced_exec_module(self: Any, module: Any) -> None:
    """Replacement for ``SourceFileLoader.exec_module`` (import / import_module /
    reload of a .py module)."""
    _original_exec_module(self, module)
    if _recording() is not None:
        _hook_enter()
        try:
            with _patch_lock:
                if _is_project_module(module):
                    _patch_module_functions(module)
                    _patch_reexported_names(module)
        except Exception:
            pass  # instrumentation must never break an import
        finally:
            _hook_exit()


def _install_executor_hook() -> None:
    """Install the cross-thread / cross-process hooks (once per process):
    ``ProcessPoolExecutor.submit`` → :func:`_traced_submit`,
    ``ThreadPoolExecutor.submit`` → :func:`_traced_thread_submit`,
    ``Thread.start`` → :func:`_traced_thread_start`. Patched on the classes,
    so they cover executors/threads created before the trace started,
    subclasses, ``Executor.map`` and ``loop.run_in_executor`` (built on
    submit). Deferred until the first trace so merely importing perf doesn't
    pull in multiprocessing."""
    if _hooks_installed:
        return
    # Under the patching lock: several first traces may start at once on
    # different threads, and installing twice would make each hook wrap the
    # other (`original` = the first hook) -- infinite recursion.
    with _patch_lock:
        if not _hooks_installed:
            _install_hooks_locked()


_hooks_installed = False


def _install_hooks_locked() -> None:
    global _original_process_submit, _original_thread_submit, _original_thread_start
    global _original_build_class, _original_exec_module, _hooks_installed
    import builtins
    import importlib.machinery
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    _original_build_class = builtins.__build_class__
    builtins.__build_class__ = _traced_build_class
    _original_exec_module = importlib.machinery.SourceFileLoader.exec_module
    importlib.machinery.SourceFileLoader.exec_module = _traced_exec_module

    _original_thread_submit = ThreadPoolExecutor.submit
    ThreadPoolExecutor.submit = _traced_thread_submit
    _original_thread_start = threading.Thread.start
    threading.Thread.start = _traced_thread_start
    _original_process_submit = ProcessPoolExecutor.submit
    ProcessPoolExecutor.submit = _traced_submit
    _install_pool_hooks()
    _hooks_installed = True


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
                "self": 0.0,
                "children": [],
                "_intervals": [],
            }
            order.append(name)
        merged[name]["self"] += _display_self(node)
        node_wall = node["time"] or 0
        node_cumul = node.get("cumulative") or node_wall
        merged[name]["start"] = min(merged[name]["start"], node.get("start", float("inf")))
        merged[name]["cumulative"] += node_cumul
        merged[name]["count"] += node.get("count", 1)
        merged[name]["children"].extend(node["children"])
        merged[name]["_intervals"].append({"start": node.get("start", 0), "time": node_wall})
        if "active_time" in node:
            merged[name]["active_time"] = merged[name].get("active_time", 0.0) + node["active_time"]

    for name in order:
        merged[name]["time"] = _wall_from_intervals(merged[name].pop("_intervals"))
        merged[name]["children"] = _collapse(merged[name]["children"])
        # drop cumulative if it equals wall (no noise)
        if abs(merged[name]["time"] - merged[name]["cumulative"]) < 1e-6:
            del merged[name]["cumulative"]
        if merged[name]["count"] > 1:
            merged[name]["name"] = f"{name} ×{merged[name]['count']}"

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
            collapsed.append({**node, "self": _display_self(node), "children": _collapse(node["children"])})
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
                "self": sum(_display_self(n) for n in group),
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
                        "start": min(inst.get("start", 0) for inst in instances),
                        "self": sum(_display_self(inst) for inst in instances),
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
                    "start": min(c.get("start", 0) for c in merged_group),
                    "self": 0.0,  # a display-only wrapper: all its time is its members'
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
    """Wall time spent inside this node excluding its children (exclusive time).

    Subtracts each child's FULL occupied window — its own execution
    (``child["time"]``) PLUS its own capture (``child["capture_time"]``), not
    execution alone. A child's wrapper doesn't return control to this node
    until the child's own args/kwargs/result have been captured too, so that
    time elapsed inside THIS node's measured window regardless — without this,
    it would silently land in this node's self time instead of being
    recognized as the child's capture cost, inflating every ancestor's
    apparent "own work" by however much capture its descendants did.
    """
    children = node["children"]
    # A generator's node spans first resume → end, including the consumer's
    # work between yields (which is NOT its child); its own time is active_time.
    t = node.get("active_time", node["time"] or 0) - node.get("locals_capture_time", 0.0)
    if not children:
        return max(t, 0.0)
    occupied = [{"start": c.get("start", 0), "time": (c["time"] or 0) + c.get("capture_time", 0.0)} for c in children]
    return max(t - _wall_from_intervals(occupied), 0.0)


def _display_self(node: dict) -> float:
    """Self time of a display node from :func:`_collapse`: precomputed from the
    raw calls it stands for. Recomputing it from the display node itself would
    be wrong -- a merged child ("f ×N") is one interval spanning only the
    union length of its calls, anchored at the first one, so merged siblings
    seem to overlap and too little child time gets subtracted."""
    if "self" in node:
        return node["self"]
    return _self_time(node)


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
    if not _cfg.HOT_SPOTS:
        return ""
    stats = _aggregate(root)
    if not stats:
        return ""
    ranked = sorted(stats.items(), key=lambda kv: kv[1]["self"], reverse=True)[:_cfg.HOT_SPOTS]
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

    Written as ``{"variables": {...}, "trace": <tree>}``: every captured
    value that recurs anywhere in the tree (see :class:`_ValuePool`) is
    stored once in ``variables`` and referenced from the tree as
    ``{"__ref__": ref}``, instead of being duplicated inline at every
    occurrence. With TRACE_LOCALS, a ``"sources"`` map holds the text of
    every recorded line once (see :data:`TRACE_LOCALS`).
    """
    if not _cfg.OUTPUT_DIR and not _cfg.OUTPUT_FILE:
        return

    import json

    tree = {**root, "children": _filter_by_names(root["children"], set(_cfg.LOG_FUNCTIONS))} if _cfg.LOG_FUNCTIONS else root
    pool = _current_pool.get()
    payload = {"variables": pool.variables if pool is not None else {}, "trace": tree}
    sources = _trace_sources()
    if sources:
        payload["sources"] = sources

    if _cfg.OUTPUT_DIR:
        directory = Path(_cfg.OUTPUT_DIR)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[perf] failed to write trace to {directory}: {e}", file=sys.stderr)
            return
        if _cfg.OUTPUT_FILE:
            filename = _cfg.OUTPUT_FILE if _cfg.OUTPUT_FILE.endswith(".json") else f"{_cfg.OUTPUT_FILE}.json"
        else:
            import uuid

            filename = f"{uuid.uuid4()}.json"
        path = directory / filename
    else:
        path = Path(_cfg.OUTPUT_FILE)

    try:
        with open(path, "w") as f:
            # Compact: indentation was ~97% of a deep trace's bytes. Pretty-print
            # for reading with `python -m json.tool perf.json` (or `jq .`).
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
    except Exception as e:  # never let telemetry change the traced call's result/exception
        print(f"[perf] failed to write trace to {path}: {e}", file=sys.stderr)


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
    if not _cfg.PRINT_REPORT:
        _write_output(root)
        _send_trace(root)
        return
    lines: list[tuple[str, str, str, str, str]] = []
    total = root["time"] or 1
    threshold = total * _cfg.MIN_PCT

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
        self_t = _display_self(node)

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
                    f"{child_prefix}└─ … {len(hidden)} hidden < {_cfg.MIN_PCT * 100:g}%",
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
    visible = [c for c in children if (c["time"] or 0) >= threshold]
    hidden = [c for c in children if (c["time"] or 0) < threshold]
    for i, child in enumerate(visible):
        walk(child, "", i == len(visible) - 1 and not hidden, root_total)
    if hidden:
        ht = sum(c["time"] or 0 for c in hidden)
        h_total = sum(_total(c) for c in hidden)
        lines.append(
            (
                f"└─ … {len(hidden)} hidden < {_cfg.MIN_PCT * 100:g}%",
                fmt(ht),
                "—",
                f"{ht / total * 100:4.0f}%",
                f"{min(h_total / root_total, 1.0) * 100:4.0f}%" if root_total else "   —",
            )
        )

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
_EXIT_FLUSH_SECONDS = 10.0  # total wait at exit for in-flight background sends
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
    # One overall deadline, not one per thread: N stuck sends must not
    # delay exit by N times the timeout.
    from time import monotonic

    deadline = monotonic() + _EXIT_FLUSH_SECONDS
    for t in threads:
        t.join(timeout=max(deadline - monotonic(), 0.0))


def _send_trace(root: dict) -> None:
    """POST the raw trace tree to ENDPOINT_URL, wrapped as
    {status, tags, variables, trace}.

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
    if not _cfg.ENDPOINT_URL:
        return
    import json
    import urllib.request

    is_error = bool(root.get("exception"))
    pool = _current_pool.get()
    payload = {
        "status": "error" if is_error else "ok",
        "tags": _cfg.TAGS,
        "variables": pool.variables if pool is not None else {},
        "trace": root,
    }
    sources = _trace_sources()
    if sources:
        payload["sources"] = sources
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    # Resolved NOW, in the trace's context: the send may run on a background
    # thread after the trace (and its per-trace overrides) has ended.
    url, api_key, send_timeout = _cfg.ENDPOINT_URL, _cfg.API_KEY, _cfg.SEND_TIMEOUT

    def _post() -> None:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        req = urllib.request.Request(
            url,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=send_timeout) as resp:
                resp.read()
        except Exception as e:  # never let telemetry break the traced call
            print(f"[perf] failed to POST trace to {url}: {e}", file=sys.stderr)

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


# ── live attach: trace calls in a process that is already running ────────────
# `perf.attach("pkg.mod:Class.method")` starts tracing the next calls of a
# function without restarting the program or editing its source. Rebinding
# the name (like `mod.fn = perf.trace(mod.fn)`) would miss every caller that
# took a reference before the attach: `from mod import fn`, registries,
# callbacks, decorators' closures. Instead the function OBJECT stays where it
# is and only its ``__code__`` is swapped for a small trampoline that hands
# the call to perf; every reference to the function goes through it. The
# trampoline calls a copy of the function that keeps the original code
# (closure, defaults, globals), so the traced call runs exactly the original
# code. Detaching swaps the original code back; calls already running are
# unaffected either way (a running frame keeps the code it started with).
#
# Getting the attach call itself into a running process is the job of
# `python -m perf attach <pid> <target>` (see perf/__main__.py): Python 3.14's
# sys.remote_exec (PEP 768) needs nothing prepared in the target, and
# `perf.enable_attach()` (a signal handler reading a request file) works on any
# Python and needs no ptrace rights. Both hand the target a request that ends
# up in _handle_attach_request.

_attachments: dict = {}  # id(function) -> Attachment currently swapped into it
_attachments_lock = threading.Lock()
_ATTACH_HOOK = "__perf_attach_hook__"  # placeholder consts in the trampoline,
_ATTACH_COPY = "__perf_attach_copy__"  # replaced by the real objects
_ATTACH_INSIDE = "__perf_attach_inside__"
_ATTACH_FILENAME = "<perf attach>"


class Attachment:
    """A live attach made by :func:`attach`: traces the next calls of one
    function until its budget (``calls`` / ``seconds``) is used up or
    :meth:`detach` is called.

    Attributes:
        target: the function object being traced.
        name: its ``__qualname__`` (the root name of each trace).
        traced: how many traced calls have started.
        active: whether new calls are still being traced.
        reason: why it stopped (``"calls"``, ``"seconds"``, ``"detached"``,
            ``"error"``) once it has.
    """

    def __init__(self, fn: Any, calls: int | None, seconds: float | None, status_path: Any = None, request_id: str | None = None) -> None:
        self.target = fn
        self.name = fn.__qualname__
        self.calls = calls
        self.seconds = seconds
        self.traced = 0
        self.active = False
        self.reason: str | None = None
        self.request_id = request_id
        self._status_path = status_path
        self._running = 0
        self._lock = threading.Lock()
        self._finished = threading.Event()
        self._timer: Any = None
        self._original_code = fn.__code__
        self._original_defaults = (fn.__defaults__, fn.__kwdefaults__)
        self._original_signature = fn.__dict__.get("__signature__", _UNSET)
        self._trampoline_code: Any = None
        self._inside = ContextVar(f"perf_attach_inside_{id(self)}", default=False)

    def __repr__(self) -> str:
        state = "active" if self.active else f"stopped ({self.reason})"
        return f"<perf.Attachment {self.name} traced={self.traced} {state}>"

    def _take(self) -> bool:
        """Claim a traced call: False when stopped, out of budget, or this is
        a nested call (recursion) inside one of this attach's own traces --
        that call is already part of the running trace."""
        if self._inside.get():
            return False
        with self._lock:
            if not self.active:
                return False
            self.traced += 1
            self._running += 1
            last = self.calls is not None and self.traced >= self.calls
        if last:
            self._stop("calls")
        return True

    def _release(self) -> None:
        with self._lock:
            self._running -= 1
            done = not self.active and self._running == 0
        if done:
            self._finish()

    def _stop(self, reason: str) -> None:
        """Stop tracing new calls and put the original code back."""
        with self._lock:
            if not self.active:
                return
            self.active = False
            self.reason = reason
            done = self._running == 0
        if self._timer is not None:
            self._timer.cancel()
        with _attachments_lock:
            if _attachments.get(id(self.target)) is self:
                del _attachments[id(self.target)]
            if self.target.__code__ is self._trampoline_code:
                self.target.__code__ = self._original_code
                self.target.__defaults__, self.target.__kwdefaults__ = self._original_defaults
                if self._original_signature is _UNSET:
                    self.target.__dict__.pop("__signature__", None)
                else:
                    self.target.__signature__ = self._original_signature
        if done:
            self._finish()

    def _finish(self) -> None:
        if not self._finished.is_set():
            self._finished.set()
            self._write_status("done")

    def _write_status(self, state: str, error: str | None = None) -> None:
        if self._status_path is not None:
            _write_attach_status(self._status_path, self.request_id, state, target=self.name, traced=self.traced, reason=self.reason, error=error)

    def detach(self) -> None:
        """Stop tracing new calls now; traced calls still running finish and
        are written as usual."""
        self._stop("detached")

    def wait(self, timeout: float | None = None) -> bool:
        """Block until stopped and every traced call has been written. Returns
        False on timeout."""
        return self._finished.wait(timeout)


def _attach_resolve(target: Any) -> Any:
    """The plain Python function a target names: a function object, or
    ``"pkg.mod:Qual.name"`` / ``"pkg.mod.Qual.name"`` naming one in a module
    this process has ALREADY imported (importing it fresh would create a
    second copy the program never calls)."""
    if isinstance(target, str):
        if ":" in target:
            module_name, _, qualname = target.partition(":")
            module = sys.modules.get(module_name)
            if module is None:
                raise LookupError(f"perf.attach: module {module_name!r} is not imported in this process")
        else:
            parts = target.split(".")
            for i in range(len(parts) - 1, 0, -1):
                module = sys.modules.get(".".join(parts[:i]))
                if module is not None:
                    module_name, qualname = ".".join(parts[:i]), ".".join(parts[i:])
                    break
            else:
                raise LookupError(f"perf.attach: no imported module in {target!r} (use 'module:Qual.name')")
        obj: Any = module
        for part in qualname.split("."):
            try:
                # Static on modules/classes (keeps staticmethod/classmethod
                # objects); plain on anything else (e.g. a property's fget).
                static = isinstance(obj, (type, types.ModuleType))
                obj = inspect.getattr_static(obj, part) if static else getattr(obj, part)
            except AttributeError:
                raise LookupError(f"perf.attach: {module_name}:{qualname} not found ({part!r} missing)") from None
        target = obj
    if isinstance(target, (staticmethod, classmethod)) or inspect.ismethod(target):
        target = target.__func__
    if isinstance(target, property):
        raise TypeError("perf.attach: a property; attach to its getter, e.g. 'mod:Class.prop.fget'")
    # Through C / non-function decorators (lru_cache, partial objects) and
    # perf's own node wrappers, down to the first plain Python function.
    seen = 0
    while not inspect.isfunction(target) or getattr(target, "_perf_node", False):
        inner = getattr(target, "__wrapped__", None) or getattr(target, "func", None)
        if inner is None or seen > 16:
            raise TypeError(f"perf.attach: {target!r} is not a Python function")
        target, seen = inner, seen + 1
    if getattr(target, "_perf_wrapped", False):
        raise ValueError(f"perf.attach: {target.__qualname__} is already traced with perf.trace")
    if inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target):
        raise TypeError(f"perf.attach: {target.__qualname__} is a generator function; attach to the function that iterates it")
    return target


def _attach_copy(fn: Any) -> Any:
    """A twin of `fn` running its original code, closure, defaults and globals."""
    code = fn.__code__
    copy = types.FunctionType(code, fn.__globals__, fn.__name__, fn.__defaults__, fn.__closure__)
    copy.__kwdefaults__ = fn.__kwdefaults__
    copy.__qualname__ = fn.__qualname__
    copy.__module__ = fn.__module__
    copy.__doc__ = fn.__doc__
    copy.__dict__.update(fn.__dict__)
    for attr in ("__annotations__", "__type_params__"):
        try:
            setattr(copy, attr, getattr(fn, attr))
        except Exception:  # 3.14 lazy annotations that fail to evaluate, etc.
            pass
    return copy


def _attach_trampoline(fn: Any, hook: Any, copy: Any, inside: Any) -> Any:
    """Code for `fn` that calls ``hook(*args, **kwargs)`` -- or, for a nested
    call inside one of the attach's own traces (``inside`` set: recursion),
    ``copy`` directly, so each recursion level costs one extra frame, not
    three. Same number of free variables as `fn`'s code (CPython requires it
    to match the closure), async when `fn` is; the objects are baked into its
    constants."""
    code = fn.__code__
    free = code.co_freevars
    is_async = bool(code.co_flags & 0x80)  # CO_COROUTINE
    mention = f"        if False: ({', '.join(free)},)\n" if free else ""
    aw = "await " if is_async else ""
    source = (
        f"def __perf_outer__({', '.join(free)}):\n"
        f"    {'async ' if is_async else ''}def {code.co_name}(*args, **kwargs):\n"
        f"{mention}"
        f"        if {_ATTACH_INSIDE!r}.get():\n"
        f"            return {aw}{_ATTACH_COPY!r}(*args, **kwargs)\n"
        f"        return {aw}{_ATTACH_HOOK!r}(*args, **kwargs)\n"
        f"    return {code.co_name}\n"
    )
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "'str' object is not callable"
        module_code = compile(source, _ATTACH_FILENAME, "exec")
    outer = next(c for c in module_code.co_consts if isinstance(c, types.CodeType))
    inner = next(c for c in outer.co_consts if isinstance(c, types.CodeType))
    objects = {_ATTACH_HOOK: hook, _ATTACH_COPY: copy, _ATTACH_INSIDE: inside}
    consts = tuple(objects.get(c, c) if type(c) is str else c for c in inner.co_consts)
    if any(type(c) is str and c in objects for c in consts) or not all(any(c is o for c in consts) for o in objects.values()):
        raise RuntimeError("perf.attach: trampoline constants not substituted")
    replace: dict = {"co_consts": consts}
    if hasattr(inner, "co_qualname"):
        replace["co_qualname"] = getattr(code, "co_qualname", code.co_name)
    trampoline = inner.replace(**replace)
    if trampoline.co_freevars != free:  # can't happen; checked so a swap never breaks the function
        raise RuntimeError("perf.attach: trampoline free variables don't match")
    return trampoline


def attach(target: Any, *, calls: int | None = 1, seconds: float | None = None, **overrides: Any) -> "Attachment":
    """Trace the next calls of a function in this (already running) process.

    Unlike ``perf.trace(fn)``, nothing has to call a new wrapper: every caller
    of the function -- including ones holding a reference taken long before
    (``from mod import fn``, registries, callbacks) -- gets traced, because
    the function object itself is switched over. Each traced call is written
    as its own trace, as if the function had been decorated with
    ``@perf.trace(**overrides)``.

    Args:
        target: the function, or ``"pkg.mod:Qual.name"`` (``"pkg.mod.func"``
            also works) naming one in an already-imported module. Methods,
            static/class methods and decorated functions (through
            ``__wrapped__``) are accepted; generator functions are not.
        calls: stop after this many traced calls (None: no limit).
        seconds: stop after this long (None: no limit).
        **overrides: per-trace config, like ``@perf.trace(...)``, checked
            up front (a bad value raises here, not in the program's call).
            ``TRACE_LOCALS=True`` is short for the attached function's own
            locals.

    Returns:
        An :class:`Attachment`; ``.detach()`` stops early, ``.wait()`` blocks
        until it has stopped and every trace has been written.
    """
    return _attach(target, calls=calls, seconds=seconds, overrides=overrides)


def _check_attach_config(overrides: dict) -> None:
    """Reject config values of the wrong shape NOW: with a decorator a bad
    value fails at the first call, in the developer's face; in a live attach
    that first call is the running program's, which must not pay for it."""
    _check_overrides(overrides)
    for key, value in overrides.items():
        default = _G.get(key)
        if key in _LIST_KEYS:
            ok = isinstance(value, str) or (isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value))
            want = "a string or a list of strings"
        elif isinstance(default, bool):
            ok, want = isinstance(value, bool), "True or False"
        elif isinstance(default, (int, float)) and key != "DEFAULT_RETURN":
            ok, want = isinstance(value, (int, float)) and not isinstance(value, bool), "a number"
        elif key in ("OUTPUT_DIR", "OUTPUT_FILE", "ENDPOINT_URL", "API_KEY", "SPILL_DIR"):
            ok, want = value is None or isinstance(value, str), "a string or None"
        else:
            ok, want = True, ""
        if not ok:
            raise TypeError(f"perf.attach: {key} must be {want}, got {value!r}")
    with _apply_overrides(overrides):
        try:
            _redactor()
        except re.error as e:
            raise ValueError(f"perf.attach: invalid REDACT_PATTERNS entry: {e}") from None


def _attach(target: Any, *, calls: Any, seconds: Any, overrides: dict, status_path: Any = None, request_id: Any = None) -> "Attachment":
    if calls is not None and (isinstance(calls, bool) or not isinstance(calls, int) or calls < 1):
        raise ValueError("perf.attach: calls must be an integer >= 1 (or None for no limit)")
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0):
        raise ValueError("perf.attach: seconds must be a positive number (or None for no limit)")
    fn = _attach_resolve(target)
    if overrides.get("TRACE_LOCALS") is True:  # shorthand: the attached function's own locals
        overrides = {**overrides, "TRACE_LOCALS": [fn.__qualname__]}
    # An attach observes a program that was running fine without perf: it
    # must not start swallowing its exceptions or killing slow calls because
    # of perf's global config. Opt in explicitly to get either.
    overrides = {"SUPPRESS_ERRORS": False, "TIMEOUT": None, **overrides}
    _check_attach_config(overrides)
    copy = _attach_copy(fn)
    wrapper = trace(copy, **overrides)
    att = Attachment(fn, calls, seconds, status_path=status_path, request_id=request_id)

    if fn.__code__.co_flags & 0x80:  # coroutine function

        async def hook(*args: Any, **kwargs: Any) -> Any:
            if not att._take():
                return await copy(*args, **kwargs)
            token = att._inside.set(True)
            try:
                return await wrapper(*args, **kwargs)
            finally:
                att._inside.reset(token)
                att._release()

    else:

        def hook(*args: Any, **kwargs: Any) -> Any:
            if not att._take():
                return copy(*args, **kwargs)
            token = att._inside.set(True)
            try:
                return wrapper(*args, **kwargs)
            finally:
                att._inside.reset(token)
                att._release()

    trampoline = _attach_trampoline(fn, hook, copy, att._inside)
    try:
        signature = fn.__dict__.get("__signature__") or inspect.signature(copy)
    except Exception:
        signature = None
    att._trampoline_code = trampoline
    _event_names[trampoline] = None  # no node of its own (it's perf's, not the program's)
    with _attachments_lock:
        current = _attachments.get(id(fn))
        if current is not None and current.target is fn:
            raise ValueError(f"perf.attach: {fn.__qualname__} is already attached (detach it first)")
        _attachments[id(fn)] = att
        att.active = True
        # The function object's own defaults would apply to the trampoline's
        # (*args, **kwargs) signature; the copy keeps them for the real code.
        if signature is not None:  # what inspect.signature() shows while swapped
            fn.__signature__ = signature
        fn.__defaults__ = fn.__kwdefaults__ = None
        fn.__code__ = trampoline
    if seconds is not None:
        att._timer = threading.Timer(seconds, att._stop, args=("seconds",))
        att._timer.daemon = True
        att._timer.start()
    return att


def attachments() -> list:
    """The live :class:`Attachment` objects in this process."""
    with _attachments_lock:
        return list(_attachments.values())


# ── attach requests: how `python -m perf attach` talks to a target ──────────
# The CLI writes the request (target + budget + config) and the target answers
# in a status file next to it, inside a per-user 0700 directory. The CLI
# delivers the request by sys.remote_exec (3.14+) or, when the target called
# enable_attach(), by a signal. Requests are JSON, never code.


def _attach_dir(uid: int | None = None) -> Path:
    import tempfile

    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"perf-attach-{uid}"


def _attach_dir_trusted(directory: Path) -> bool:
    """Only this user (or root) can have written into it."""
    if not hasattr(os, "getuid"):
        return directory.is_dir()
    try:
        st = directory.stat()
    except OSError:
        return False
    return st.st_uid in (os.getuid(), 0) and not st.st_mode & 0o022


def _write_attach_status(path: Any, request_id: Any, state: str, **fields: Any) -> None:
    try:
        path = Path(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(_json.dumps({"id": request_id, "state": state, "pid": os.getpid(), **fields}))
        os.replace(tmp, path)
    except Exception as e:
        print(f"[perf] attach: failed to write status {path}: {e!r}", file=sys.stderr)


def _handle_attach_request(path: Any) -> None:
    """Act on one request file (attach / detach); never raises."""
    path = Path(path)
    status = path.with_name(f"{os.getpid()}.status.json")
    request_id = None
    if not path.exists():
        return  # withdrawn by the CLI (it gave up waiting), or already handled
    try:
        if not _attach_dir_trusted(path.parent):
            raise PermissionError(f"{path.parent} is not private to this user")
        request = _json.loads(path.read_text())
        request_id = request.get("id")
        if isinstance(request_id, str) and request_id.isalnum():
            status = path.with_name(f"{os.getpid()}.{request_id}.status.json")
        try:
            path.unlink()
        except OSError:
            pass
        action = request.get("action", "attach")
        if action == "detach":
            names = request.get("target")
            stopped = []
            for att in attachments():
                if names is None or att.name == names or att.target is _safe_resolve(names):
                    att.detach()
                    stopped.append(att.name)
            _write_attach_status(status, request_id, "detached", stopped=stopped)
            return
        att = _attach(
            request["target"],
            calls=request.get("calls"),
            seconds=request.get("seconds"),
            overrides=request.get("config") or {},
            status_path=status,
            request_id=request_id,
        )
        _write_attach_status(status, request_id, "attached", target=att.name, calls=att.calls, seconds=att.seconds)
        print(f"[perf] attached to {att.name} (pid {os.getpid()})", file=sys.stderr)
    except BaseException as e:  # a bad request must never take the program down
        _write_attach_status(status, request_id, "error", error=f"{type(e).__name__}: {e}")


def _safe_resolve(target: Any) -> Any:
    try:
        return _attach_resolve(target)
    except Exception:
        return None


_attach_signal: Any = None


def _process_start(pid: int) -> Any:
    """Kernel start time of `pid` (Linux; None elsewhere)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def enable_attach(sig: Any = None) -> None:
    """Let ``python -m perf attach <pid> ...`` attach to this process later.

    Call it once at startup. It installs a handler for ``sig`` (default
    ``SIGUSR1``) that reads the attach request the CLI left in a private
    per-user directory; until then it costs nothing. Needed on Python < 3.14,
    or when ptrace is restricted (``sys.remote_exec`` needs ptrace rights);
    on 3.14+ the CLI can attach without it. Must be called from the main
    thread (a Python signal-handler rule). POSIX only.
    """
    global _attach_signal
    if sig is None:
        sig = getattr(signal, "SIGUSR1", None)
        if sig is None:
            raise OSError("perf.enable_attach: no SIGUSR1 on this platform")
    directory = _attach_dir()
    directory.mkdir(mode=0o700, exist_ok=True)
    pid = os.getpid()
    marker = directory / f"{pid}.listening"

    def _on_signal(signum: int, frame: Any) -> None:
        # One file per request (<pid>.<id>.request.json): two CLIs asking at
        # once must not overwrite each other; a signal may stand for both.
        for request in sorted(directory.glob(f"{pid}.*.request.json")):
            _handle_attach_request(request)

    signal.signal(sig, _on_signal)
    # The start time lets the CLI tell this process from a later one that
    # reuses the pid after we die without running atexit (SIGKILL/SIGTERM):
    # sending SIGUSR1 to a process that doesn't handle it would kill it.
    marker.write_text(_json.dumps({"signal": int(sig), "start": _process_start(os.getpid())}))
    _attach_signal = sig

    def _cleanup() -> None:
        try:
            marker.unlink()
        except OSError:
            pass

    atexit.register(_cleanup)


_register_wrapper_codes()

if _speedups is not None:
    _speedups.init(globals())
