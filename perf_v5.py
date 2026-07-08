import asyncio
import inspect
import re
import signal
import sys
import sysconfig
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Any

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
    if seconds >= 1:
        return f"{seconds:.2f} s"
    if seconds >= 1e-3:
        return f"{seconds * 1e3:.2f} ms"
    return f"{seconds * 1e6:.0f} µs"


# ── tree ──────────────────────────────────────────────────────────────────────

_current_node: ContextVar[dict | None] = ContextVar("_current_node", default=None)


def make_node(name: str) -> dict:
    return {"name": name, "time": None, "start": perf_counter(), "children": []}


# ── output tuning ──────────────────────────────────────────────────────────
# Tree nodes whose wall time is below this fraction of the root total are
# hidden, collapsed into one "… N hidden" line per parent. Default 0.0 shows
# everything; raise it (e.g. 0.01) when the tree gets too noisy.
MIN_PCT = 0.0
# Number of functions to list in the flat "hot spots" summary. 0 disables it.
HOT_SPOTS = 15
# When set, the rendered report is also written to this path (e.g. "perf.txt").
OUTPUT_FILE: str | None = "perf.json"
# When set, the raw trace tree is POSTed here at the end of every traced call,
# wrapped as {"status", "tags", "trace"} (see _send_trace). None disables it.
ENDPOINT_URL: str | None = "http://localhost:7999/api/traces"
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


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception as e:  # a __repr__ that raises must not kill the trace
        return f"<unreprable {type(value).__name__}: {e}>"


def _record_inputs(node: dict, args: tuple, kwargs: dict) -> None:
    if not CAPTURE_IO:
        return
    # positional args keep their slot; skipped objects become a compact marker
    node["args"] = [f"<{type(a).__name__}>" if (jv := _jsonable(a)) is _SKIP else jv for a in args]
    node["kwargs"] = {k: jv for k, v in kwargs.items() if (jv := _jsonable(v)) is not _SKIP}


def _record_output(node: dict, result: Any) -> None:
    if CAPTURE_IO:
        jv = _jsonable(result)
        node["result"] = f"<{type(result).__name__}>" if jv is _SKIP else jv


def _record_exception(node: dict, exc: BaseException) -> None:
    if CAPTURE_IO:
        node["exception"] = _safe_repr(exc)


def _is_project_module(module: Any) -> bool:
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
    module = sys.modules.get(cls.__module__)
    if module is None:
        return False
    return _is_project_module(module)


def _patch_module_functions(module: Any) -> None:
    """Patch free functions defined in a project module."""
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
    _patch_class(type(obj))


def _patch_all_project_classes() -> None:
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
    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        parent = _current_node.get()
        node = make_node(name)
        _record_inputs(node, args, kwargs)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = func(*args, **kwargs)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _record_exception(node, exc)
            raise
        finally:
            node["time"] = perf_counter() - node["start"]
            _current_node.reset(token)

    return wrapper


def _async_wrap(func: Any, name: str) -> Any:
    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        parent = _current_node.get()
        node = make_node(name)
        _record_inputs(node, args, kwargs)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            result = await func(*args, **kwargs)
            _record_output(node, result)
            return result
        except BaseException as exc:
            _record_exception(node, exc)
            raise
        finally:
            node["time"] = perf_counter() - node["start"]
            _current_node.reset(token)

    return wrapper


# ── setprofile hook: intercepts __init__ and call to patch instances ──────────


def _make_profiler() -> Any:
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


def trace(func: Any) -> Any:
    # if not ENV.debug:
    #     return func

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # type: ignore
            _patch_all_project_classes()  # ← NEW
            root = make_node(func.__qualname__)
            _record_inputs(root, args, kwargs)
            _current_node.set(root)
            # _patched_classes.clear()  ← REMOVED
            profiler = _make_profiler()
            threading.setprofile(profiler)
            sys.setprofile(profiler)
            try:
                if TIMEOUT is not None:
                    result = await asyncio.wait_for(func(*args, **kwargs), TIMEOUT)
                else:
                    result = await func(*args, **kwargs)
                _record_output(root, result)
            except BaseException as exc:
                _record_exception(root, exc)
                raise
            finally:
                root["time"] = perf_counter() - root["start"]
                sys.setprofile(None)
                threading.setprofile(None)
                print_tree(root)
            return result
    else:

        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            _patch_all_project_classes()  # ← NEW
            root = make_node(func.__qualname__)
            _record_inputs(root, args, kwargs)
            _current_node.set(root)
            # _patched_classes.clear()  ← REMOVED
            profiler = _make_profiler()
            threading.setprofile(profiler)
            sys.setprofile(profiler)
            try:
                with _sync_timeout(TIMEOUT, func.__qualname__):
                    result = func(*args, **kwargs)
                _record_output(root, result)
            except BaseException as exc:
                _record_exception(root, exc)
                raise
            finally:
                root["time"] = perf_counter() - root["start"]
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


# ── printer ───────────────────────────────────────────────────────────────────


def print_tree(root: dict) -> None:
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
    if OUTPUT_FILE:
        import json

        with open(OUTPUT_FILE, "w") as f:
            f.write(json.dumps(root, indent=2, ensure_ascii=False, default=str))
    _send_trace(root)


# ── remote trace shipping ─────────────────────────────────────────────────────


def _send_trace(root: dict) -> None:
    """POST the raw trace tree to ENDPOINT_URL, wrapped as {status, tags, trace}.

    Runs in a daemon thread so a slow/unreachable collector never blocks (or
    crashes) the traced call. A failed send is logged to stderr and swallowed.
    """
    if not ENDPOINT_URL:
        return
    import json
    import urllib.request

    payload = {
        "status": "error" if root.get("exception") else "ok",
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

    threading.Thread(target=_post, name="perf-send-trace", daemon=True).start()


# @trace
# def a() -> None:
#     print(1)
