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
import dataclasses
import inspect
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

__version__ = "0.6.6"
__all__ = [
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
    "SCAN_VENV",
    "SKIP_FUNCTIONS",
    "SKIP_VARIABLES",
    "SPILL_DIR",
    "SUPPRESS_ERRORS",
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
# >0 while executing inside a SKIP_FUNCTIONS call (at any nesting depth), so
# _sync_wrap/_async_wrap can bypass node creation for everything called from
# within it too, not just the skipped call itself.
_skip_depth: ContextVar[int] = ContextVar("_skip_depth", default=0)
# The current top-level trace's value pool (see _ValuePool below) — None
# outside any traced call. Set once per top-level trace() invocation, never
# shared across traces: a fresh pool every time, so one trace's captured
# values can never depend on (or leak into) another's.
_current_pool: ContextVar["_ValuePool | None"] = ContextVar("_current_pool", default=None)


def _is_skipped(name: str) -> bool:
    """Whether ``name`` matches an entry in SKIP_FUNCTIONS (glob-aware via fnmatch)."""
    return any(fnmatch(name, pattern) for pattern in SKIP_FUNCTIONS)


def _is_skipped_variable(name: str) -> bool:
    """Whether ``name`` matches an entry in SKIP_VARIABLES (glob-aware via fnmatch)."""
    return bool(SKIP_VARIABLES) and any(fnmatch(name, pattern) for pattern in SKIP_VARIABLES)


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
    ``sys.setprofile`` hook — before the wrapped
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
# When set, the raw trace tree is POSTed here at the end of every traced call,
# wrapped as {"status", "tags", "trace"} (see _send_trace). None (the default)
# disables it — set it to opt in (e.g. "http://localhost:7999/api/traces").
ENDPOINT_URL: str | None = None
# Tags attached to every posted trace (e.g. ["prod", "search"]).
TAGS: list = ["dev"]
# Pre-shared key for collectors that require API-key auth on their ingestion
# route (sent as the X-API-Key header on every POST to ENDPOINT_URL).
API_KEY: str | None = "F-OZcYtZT0H_5FXT5qwm1-Ftaop5AKiVSFIbr_HGvs4"
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
# Only capture a value whose size (in UTF-8 bytes, enforced WHILE converting —
# see _jsonable's `_budget` param) is below this limit. A converted arg / kwarg
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
# Repeated captures of the exact same object (same `id()` — a service `self`,
# a shared config dict, a big result list passed on to the next stage, a
# reused tokenizer's vocab, …) never pay for a second conversion or a second
# spill: every trace has its own value pool (see _ValuePool) that converts a
# given object once and has every later occurrence, anywhere in the tree,
# just reference that one converted copy. This is automatic whenever
# CAPTURE_IO is on — nothing to configure, and it applies to positional args,
# kwargs, AND return values alike, not just `self`.
SPILL_DIR: str | None = None


# Returned by _jsonable for values with no useful serialization (objects whose
# repr is just the default "<pkg.Class object at 0x…>"). Skipped from the output.
_SKIP = object()

# Matches Python's default object repr, e.g.
#   <qdrant_client.http.api_client.BaseAsyncMiddleware object at 0x7fd8bc2d4d70>
_DEFAULT_REPR = re.compile(r"^<[\w.]+ object at 0x[0-9a-fA-F]+>$")


def _jsonable(value: Any, depth: int = 0, _seen: frozenset = frozenset(), _budget: list | None = None) -> Any:
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

    Every dict/list/tuple/set/rich-object/pydantic-model value is also routed
    through the current trace's :class:`_ValuePool` (see ``_current_pool``),
    keyed by the *original* Python object's ``id()`` (not its converted
    form — a pydantic model's ``model_dump()`` allocates a fresh dict every
    call, so identity has to be checked before conversion, not after). The
    first time a given object is seen anywhere in the trace it's converted
    and stored in the pool once; every later occurrence — the same `self`
    passed down through nested calls, a big result list threaded on to the
    next stage, a shared tokenizer's vocab — resolves instantly to a small
    ``{"__ref__": ref}`` instead of being re-serialized (or re-spilled) from
    scratch. This check happens BEFORE the depth/budget/cycle checks below,
    since resolving an existing ref is cheap and safe regardless of how deep
    or how budget-starved this branch is.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
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
    pool = _current_pool.get()
    if pool is not None:
        ref = pool.ref_by_id.get(id(value))
        if ref is not None:
            return {"__ref__": ref}
    if depth >= MAX_DEPTH:
        return _safe_repr(value)
    if id(value) in _seen:
        return f"<circular {type(value).__name__}>"
    if _budget is not None and _budget[0] <= 0:
        return f"<{type(value).__name__} truncated: size limit reached>"
    try:
        if isinstance(value, (dict, types.MappingProxyType)):
            seen = _seen | {id(value)}
            out = {}
            for k, v in value.items():
                if isinstance(k, str) and _is_skipped_variable(k):
                    continue
                if _budget is not None and _budget[0] <= 0:
                    break
                jv = _jsonable(v, depth + 1, seen, _budget)
                if jv is not _SKIP:
                    out[k if isinstance(k, str) else _safe_repr(k)] = jv
            return _pool_register(pool, value, out)
        if isinstance(value, (list, tuple, set, frozenset)):
            seen = _seen | {id(value)}
            out = []
            for v in value:
                if _budget is not None and _budget[0] <= 0:
                    break
                jv = _jsonable(v, depth + 1, seen, _budget)
                if jv is not _SKIP:
                    out.append(jv)
            return _pool_register(pool, value, out)
        # pydantic models, dataclasses, and other rich objects. exclude_none
        # drops fields that are unset (Optional[...] = None) rather than
        # carrying an explicit null for every one of them -- schemas like a
        # BaseModel with a couple dozen optional fields (e.g. most of them
        # None on any given instance) would otherwise dump every single field
        # name every single time the model flows through a traced call,
        # which is what actually blows up perf.json's size, not depth.
        if hasattr(value, "model_dump"):  # pydantic v2
            converted = _jsonable(value.model_dump(exclude_none=True), depth + 1, _seen | {id(value)}, _budget)
            return _pool_register(pool, value, converted)
        if hasattr(value, "dict") and callable(value.dict):  # pydantic v1
            converted = _jsonable(value.dict(exclude_none=True), depth + 1, _seen | {id(value)}, _budget)
            return _pool_register(pool, value, converted)
        # functions/methods/builtins: no useful __dict__ to unpack (and a
        # bound method's __func__ may carry unrelated noise like __wrapped__
        # from being auto-instrumented) -- describe identity instead of
        # falling through to a raw "<bound method ... at 0x...>" repr.
        if inspect.isroutine(value):
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
        if hasattr(value, "__dict__") and vars(value):
            seen = _seen | {id(value)}
            converted = {"__type__": type(value).__name__, **_jsonable(vars(value), depth + 1, seen, _budget)}
            return _pool_register(pool, value, converted)
    except Exception as e:
        return f"<unconvertible {type(value).__name__}: {e}>"
    r = _safe_repr(value)
    if _budget is not None:
        if len(r) >= _budget[0]:
            _budget[0] = 0
        else:
            _budget[0] -= len(r)
    return _SKIP if _DEFAULT_REPR.match(r) else r


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
        Path(SPILL_DIR).mkdir(parents=True, exist_ok=True)
        path = Path(SPILL_DIR) / f"{uuid.uuid4()}.json"
        with open(path, "w") as f:
            json.dump(full, f, ensure_ascii=False, default=str)
        size = path.stat().st_size
        del full
        return {"__spilled__": str(path), "size": size}
    except Exception as e:  # never let telemetry break the traced call
        print(f"[perf] failed to spill oversized value to {SPILL_DIR}: {e}", file=sys.stderr)
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
    """
    if MAX_SIZE_CAPTURE is None:
        return _jsonable(value)
    budget = [MAX_SIZE_CAPTURE]
    jv = _jsonable(value, _budget=budget)
    if jv is _SKIP:
        return _SKIP
    if budget[0] > 0:
        return jv
    if SPILL_DIR:
        return _spill_value(value)
    return _SKIP


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception as e:  # a __repr__ that raises must not kill the trace
        return f"<unreprable {type(value).__name__}: {e}>"


# ── per-trace value pool: dedup repeated captures by object identity ──────────


class _ValuePool:
    """Converts each distinct object at most once per trace; every later
    occurrence of the same object anywhere in the tree — a `self` threaded
    through nested calls, a big result passed on to the next stage, a shared
    config/tokenizer, … — resolves to a small ``{"__ref__": ref}`` instead of
    being independently re-serialized (and possibly re-spilled).

    Keyed by `id(value)`. A plain `dict`/`list` can't be weak-referenced, so
    instead of the weakref-based liveness tracking a per-instance diff cache
    would need, every pooled object is simply kept alive in `_keepalive` for
    the rest of this trace — cheap here since the trace already outlives
    every value flowing through it, and it's what guarantees an id can never
    be recycled by an unrelated object mid-trace and wrongly resolve to a
    stale entry.

    One instance lives for the duration of a single top-level :func:`trace`
    call (see `_current_pool`) — never shared across traces, so one trace's
    pooled values can't leak into another's.
    """

    def __init__(self, prefix: str = "") -> None:
        # `prefix` namespaces this pool's refs -- a worker process's pool
        # (see _RemoteCall) gets a unique one so its variables can be merged
        # into the parent trace's pool without colliding with "v0", "v1", ….
        self.ref_by_id: dict[int, str] = {}
        self.variables: dict[str, Any] = {}
        self._keepalive: list = []
        self._prefix = prefix
        self._next = 0

    def register(self, value: Any, converted: Any) -> dict:
        ref = f"{self._prefix}v{self._next}"
        self._next += 1
        self.ref_by_id[id(value)] = ref
        self.variables[ref] = converted
        self._keepalive.append(value)
        return {"__ref__": ref}


def _pool_register(pool: "_ValuePool | None", value: Any, converted: Any) -> Any:
    """Stores `converted` (the freshly-converted form of `value`) in `pool`
    under a new ref and returns the ``{"__ref__": ref}`` marker in its place
    — or just returns `converted` unchanged if there's no active pool or
    nothing useful was produced (`_SKIP`).
    """
    if pool is None or converted is _SKIP:
        return converted
    return pool.register(value, converted)


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


def _compute_default_return(default_factory: Any) -> Any:
    """Resolve the value to substitute for a suppressed root-level exception.

    Honors a per-call ``DEFAULT_RETURN`` override (a plain value, used as-is,
    or a callable, invoked fresh each time) over the auto-detected
    `default_factory` derived from the function's return annotation.
    """
    if DEFAULT_RETURN is not _UNSET:
        return DEFAULT_RETURN() if callable(DEFAULT_RETURN) else DEFAULT_RETURN
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

    Identity-based dedup against repeated captures of the same object (e.g.
    `self`, passed down through nested calls) happens automatically inside
    :func:`_jsonable` via the trace's :class:`_ValuePool` — nothing extra to
    do here.
    """
    jv = _capture_value(a)
    return f"<{type(a).__name__}>" if jv is _SKIP else jv


def _record_inputs(node: dict, args: tuple, kwargs: dict, param_names: tuple = ()) -> None:
    """Attach a JSON-safe snapshot of a call's arguments to its node.

    No-op unless :data:`CAPTURE_IO` is on. Positional args keep their index in
    ``node["args"]``; a value with no useful serialization (the ``_SKIP``
    sentinel from :func:`_jsonable`) is replaced by a compact ``"<TypeName>"``
    marker so the slot is preserved. Keyword args go to ``node["kwargs"]``,
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
    if not CAPTURE_IO:
        return
    start = perf_counter()
    captured = []
    for i, a in enumerate(args):
        pname = param_names[i] if i < len(param_names) else None
        if pname is not None and _is_skipped_variable(pname):
            captured.append(f"<skipped:{pname}>")
        else:
            captured.append(_capture_positional(a))
    node["args"] = captured
    node["kwargs"] = {
        k: jv
        for k, v in kwargs.items()
        if not _is_skipped_variable(k) and (jv := _capture_value(v)) is not _SKIP
    }
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
    if CAPTURE_IO:
        start = perf_counter()
        jv = _capture_value(result)
        node["result"] = f"<{type(result).__name__}>" if jv is _SKIP else jv
        node["capture_time"] = node.get("capture_time", 0.0) + (perf_counter() - start)


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

    Also accumulates onto ``node["capture_time"]`` — see :func:`_record_inputs`
    for why (walking a full traceback/cause chain isn't free either).

    Args:
        node: The call-tree node to annotate in place.
        exc: The exception raised out of the traced call (re-raised by the caller).
    """
    if CAPTURE_IO:
        start = perf_counter()
        node["exception"] = _safe_repr(exc)
        origin = _exception_origin(exc)
        if origin is not None:
            node["exception_origin"] = origin
        node["exception_debug"] = _exception_debug(exc)
        node["capture_time"] = node.get("capture_time", 0.0) + (perf_counter() - start)


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
            _wrapped_originals[id(attr)] = (attr, wrapped)
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
        if name.startswith("_"):
            continue
        try:
            attr = vars(module)[name]
        except Exception:
            continue
        entry = _wrapped_originals.get(id(attr))
        if entry is None or attr is not entry[0]:
            continue
        try:
            setattr(module, name, entry[1])
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
    project_modules = [m for m in sys.modules.values() if m is not None and _is_project_module(m)]
    for module in project_modules:
        _patch_module_functions(module)  # ← NEW
        for attr_name in list(vars(module)):
            try:
                attr = vars(module)[attr_name]
            except Exception:
                continue
            if isinstance(attr, type) and attr.__module__ == module.__name__:
                _patch_class(attr)
    # Second pass: fix up `from other import func` bindings now that every
    # defining module's own functions have been wrapped (see
    # _patch_reexported_names).
    for module in project_modules:
        _patch_reexported_names(module)


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
    counted as part of the function's own time. Exceptions are always recorded
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
    default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    param_names = _resolve_param_names(func)

    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        if _is_skipped(name) or _skip_depth.get():
            token = _skip_depth.set(_skip_depth.get() + 1)
            try:
                return func(*args, **kwargs)
            finally:
                _skip_depth.reset(token)
        parent = _current_node.get()
        node = make_node(name)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = func(*args, **kwargs)
            _finish_node(node)
            _record_inputs(node, args, kwargs, param_names)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _finish_node(node)
            _record_inputs(node, args, kwargs, param_names)
            _record_exception(node, exc)
            if not SUPPRESS_ERRORS:
                raise
            result = _compute_default_return(default_factory)
            _record_output(node, result)
            return result
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
    Exceptions are always recorded on the node; per :data:`SUPPRESS_ERRORS`,
    they either re-raise (default) or are swallowed with a type-derived default
    returned in their place — see :func:`_sync_wrap` for the full rationale.

    Args:
        func: The original coroutine function to time.
        name: Display name for the node (typically ``"Owner.attr"``).

    Returns:
        A ``functools.wraps``-preserving async wrapper with the same signature.
    """
    default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    param_names = _resolve_param_names(func)

    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        if _is_skipped(name) or _skip_depth.get():
            token = _skip_depth.set(_skip_depth.get() + 1)
            try:
                return await func(*args, **kwargs)
            finally:
                _skip_depth.reset(token)
        parent = _current_node.get()
        node = make_node(name)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = await func(*args, **kwargs)
            _finish_node(node)
            _record_inputs(node, args, kwargs, param_names)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _finish_node(node)
            _record_inputs(node, args, kwargs, param_names)
            _record_exception(node, exc)
            if not SUPPRESS_ERRORS:
                raise
            result = _compute_default_return(default_factory)
            _record_output(node, result)
            return result
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

    def on_start(code: Any, instruction_offset: Any) -> None:
        # PY_START hands (code, offset), not a frame -- unlike sys.setprofile,
        # which hands the frame directly. `self` (when there is one) is
        # already bound in locals by the time the function body starts.
        self = sys._getframe(1).f_locals.get("self")
        if self is not None:
            _patch_instance(self)

    def on_return(code: Any, instruction_offset: Any, retval: Any) -> None:
        if code.co_name == "__init__":
            self = sys._getframe(1).f_locals.get("self")
            if self is not None:
                _patch_instance(self)

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
        "API_KEY",
        "TIMEOUT",
        "SCAN_VENV",
        "CAPTURE_IO",
        "MAX_DEPTH",
        "MAX_SIZE_CAPTURE",
        "SPILL_DIR",
        "SKIP_FUNCTIONS",
        "SUPPRESS_ERRORS",
        "DEFAULT_RETURN",
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

    _default_factory = _default_factory_for_annotation(_resolve_return_annotation(func))
    _param_names = _resolve_param_names(func)

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # type: ignore
            with _apply_overrides(overrides):
                _patch_all_project_classes()  # ← NEW
                _install_executor_hook()
                root = make_node(func.__qualname__)
                _current_node.set(root)
                # Fresh value pool for this trace only -- see _current_pool
                # and _ValuePool.
                _current_pool.set(_ValuePool() if CAPTURE_IO else None)
                _uninstall_profiler = _install_profiler()
                _restart(root)  # setup above shouldn't count as the call's own time
                try:
                    if TIMEOUT is not None:
                        result = await asyncio.wait_for(func(*args, **kwargs), TIMEOUT)
                    else:
                        result = await func(*args, **kwargs)
                    _finish_node(root)
                    _record_inputs(root, args, kwargs, _param_names)
                    _record_output(root, result)
                except BaseException as exc:
                    _finish_node(root)
                    _record_inputs(root, args, kwargs, _param_names)
                    _record_exception(root, exc)
                    if not SUPPRESS_ERRORS:
                        raise
                    result = _compute_default_return(_default_factory)
                    _record_output(root, result)
                finally:
                    _uninstall_profiler()
                    print_tree(root)
                return result
    else:

        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            with _apply_overrides(overrides):
                _patch_all_project_classes()  # ← NEW
                _install_executor_hook()
                root = make_node(func.__qualname__)
                _current_node.set(root)
                # Fresh value pool for this trace only -- see _current_pool
                # and _ValuePool.
                _current_pool.set(_ValuePool() if CAPTURE_IO else None)
                _uninstall_profiler = _install_profiler()
                try:
                    with _sync_timeout(TIMEOUT, func.__qualname__):
                        _restart(root)  # setup above shouldn't count as the call's own time
                        result = func(*args, **kwargs)
                        _finish_node(root)
                    _record_inputs(root, args, kwargs, _param_names)
                    _record_output(root, result)
                except BaseException as exc:
                    _finish_node(root)
                    _record_inputs(root, args, kwargs, _param_names)
                    _record_exception(root, exc)
                    if not SUPPRESS_ERRORS:
                        raise
                    result = _compute_default_return(_default_factory)
                    _record_output(root, result)
                finally:
                    _uninstall_profiler()
                    print_tree(root)
                return result

    return wrapper


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
        value = globals()[key]
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
    if inspect.isfunction(func) and "." not in qualname:
        return f"{func.__module__}.{qualname}"
    return qualname


class _RemoteOutcome:
    """What a :class:`_RemoteCall` sends back: the call's real `result` plus
    the worker-side subtree (`children`) and the pool `variables` it refers
    to. On failure the same object rides back on the exception instead, as
    its ``_perf_remote`` attribute."""

    def __init__(self, result: Any, children: list, variables: dict) -> None:
        self.result = result
        self.children = children
        self.variables = variables


class _RemoteCall:
    """Picklable stand-in for `fn` that traces it inside a worker process.

    Pickled by reference (``perf._RemoteCall``) like any module-level class,
    so the worker just needs ``perf`` importable — which it is, since the
    parent imported it.
    """

    def __init__(self, fn: Any, name: str, config: dict) -> None:
        self.fn = fn
        self.name = name
        self.config = config

    def __call__(self, *args: Any, **kwargs: Any) -> "_RemoteOutcome":
        # A fresh, empty Context: the worker's own state (if it is itself
        # inside some unrelated trace) can't leak in, and nothing set here
        # outlives the call.
        import contextvars

        return contextvars.Context().run(self._run, args, kwargs)

    def _run(self, args: tuple, kwargs: dict) -> "_RemoteOutcome":
        import uuid

        g = globals()
        saved = {k: g[k] for k in self.config}
        g.update(self.config)
        try:
            _install_executor_hook()  # nested pools inside the worker get traced too
            _patch_all_project_classes()
            holder = make_node(self.name)
            pool = _ValuePool(prefix=f"{uuid.uuid4().hex[:8]}_") if CAPTURE_IO else None
            _current_node.set(holder)
            _current_pool.set(pool)
            # Unpickled by name, `fn` is the original on this worker's first
            # task (its module wasn't patched yet) but already the wrapper on
            # later ones — wrap it only when it isn't, so it gets one node.
            # Non-function callables (e.g. the functools.partial that
            # Executor.map submits per chunk) get no node of their own; the
            # project calls they make attach straight to the parent.
            fn = self.fn
            if not getattr(fn, "_perf_wrapped", False) and (inspect.isfunction(fn) or inspect.ismethod(fn)):
                fn = _sync_wrap(fn, self.name)
            uninstall_profiler = _install_profiler()
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                try:
                    exc._perf_remote = _RemoteOutcome(None, holder["children"], pool.variables if pool else {})
                except Exception:
                    pass  # an exception type that rejects attributes just loses its subtree
                raise
            finally:
                uninstall_profiler()
            return _RemoteOutcome(result, holder["children"], pool.variables if pool else {})
        finally:
            g.update(saved)


def _graft_remote(future: Any, parent: dict, pool: "_ValuePool | None") -> None:
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
    if pool is not None:
        pool.variables.update(outcome.variables)
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
    parent = _current_node.get()
    if parent is None or _skip_depth.get():
        return _original_process_submit(self, fn, *args, **kwargs)

    future = _original_process_submit(self, _RemoteCall(fn, _remote_name(fn), _remote_config()), *args, **kwargs)
    pool = _current_pool.get()
    lock = threading.Lock()
    grafted = False

    def absorb(f: Any) -> None:
        nonlocal grafted
        with lock:
            if not grafted:
                grafted = True
                _graft_remote(f, parent, pool)

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


def _install_executor_hook() -> None:
    """Route ``ProcessPoolExecutor.submit`` through :func:`_traced_submit`
    (once per process). Patched on the class, so it covers executors created
    before the trace started, subclasses, and ``Executor.map`` (built on
    submit). Deferred until the first trace so merely importing perf doesn't
    pull in multiprocessing."""
    global _original_process_submit
    if _original_process_submit is not None:
        return
    from concurrent.futures import ProcessPoolExecutor

    _original_process_submit = ProcessPoolExecutor.submit
    ProcessPoolExecutor.submit = _traced_submit


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
    t = node["time"] or 0
    if not children:
        return t
    occupied = [{"start": c.get("start", 0), "time": (c["time"] or 0) + c.get("capture_time", 0.0)} for c in children]
    return max(t - _wall_from_intervals(occupied), 0.0)


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

    Written as ``{"variables": {...}, "trace": <tree>}``: every captured
    value that recurs anywhere in the tree (see :class:`_ValuePool`) is
    stored once in ``variables`` and referenced from the tree as
    ``{"__ref__": ref}``, instead of being duplicated inline at every
    occurrence.
    """
    if not OUTPUT_DIR and not OUTPUT_FILE:
        return

    import json

    tree = {**root, "children": _filter_by_names(root["children"], set(LOG_FUNCTIONS))} if LOG_FUNCTIONS else root
    pool = _current_pool.get()
    payload = {"variables": pool.variables if pool is not None else {}, "trace": tree}

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
        f.write(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


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
    if not ENDPOINT_URL:
        return
    import json
    import urllib.request

    is_error = bool(root.get("exception"))
    pool = _current_pool.get()
    payload = {
        "status": "error" if is_error else "ok",
        "tags": TAGS,
        "variables": pool.variables if pool is not None else {},
        "trace": root,
    }
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    def _post() -> None:
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["X-API-Key"] = API_KEY
        req = urllib.request.Request(
            ENDPOINT_URL,
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
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
