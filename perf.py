import inspect
import sys
import sysconfig
import threading
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Any

from app.config.environment import ENV

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


# ── auto-patch instances created during a trace session ───────────────────────

_patched_classes: set = set()

# Modules to always skip even if their __file__ isn't under .venv
_SKIP_MODULE_PREFIXES = (
    "_pydevd", "pydevd", "debugpy", "pytest",
    "_pytest", "ipykernel", "IPython", "jedi",
)


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
            not callable(attr)
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
    if cls in _patched_classes:
        return
    if not _is_project_class(cls):
        return
    if cls.__module__ == __name__:
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
            elif callable(raw):
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
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            return func(*args, **kwargs)
        finally:
            node["time"] = perf_counter() - node["start"]
            _current_node.reset(token)

    return wrapper


def _async_wrap(func: Any, name: str) -> Any:
    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        parent = _current_node.get()
        node = make_node(name)
        if parent is not None:
            parent["children"].append(node)
        token = _current_node.set(node)
        try:
            return await func(*args, **kwargs)
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


# ── @trace entry-point decorator ─────────────────────────────────────────────

def trace(func: Any) -> Any:
    if not ENV.debug:
        return func

    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:  # type: ignore
            _patch_all_project_classes()           # ← NEW
            root = make_node(func.__qualname__)
            _current_node.set(root)
            # _patched_classes.clear()  ← REMOVED
            profiler = _make_profiler()
            threading.setprofile(profiler)
            sys.setprofile(profiler)
            try:
                result = await func(*args, **kwargs)
            finally:
                root["time"] = perf_counter() - root["start"]
                sys.setprofile(None)
                threading.setprofile(None)
                print_tree(root)
            return result
    else:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            _patch_all_project_classes()           # ← NEW
            root = make_node(func.__qualname__)
            _current_node.set(root)
            # _patched_classes.clear()  ← REMOVED
            profiler = _make_profiler()
            threading.setprofile(profiler)
            sys.setprofile(profiler)
            try:
                result = func(*args, **kwargs)
            finally:
                root["time"] = perf_counter() - root["start"]
                sys.setprofile(None)
                threading.setprofile(None)
                print_tree(root)
            return result

    return wrapper

# ── collapse / merge helpers ─────────────────────────────────────────────────


def _merge_children(nodes: list) -> list:
    """Merge same-named nodes, tracking wall (max) and cumulative (sum) separately."""
    merged: dict[str, dict] = {}
    order = []
    for node in nodes:
        name = node["name"]
        if name not in merged:
            merged[name] = {
                "name": name,
                "time": 0.0,
                "cumulative": 0.0,
                "start": float("inf"),  # will become min
                "children": [],
            }
            order.append(name)
        node_wall = node["time"] or 0
        node_cumul = node.get("cumulative") or node_wall
        merged[name]["start"] = min(merged[name]["start"], node.get("start", float("inf")))
        merged[name]["time"] = max(merged[name]["time"], node_wall)
        merged[name]["cumulative"] += node_cumul
        merged[name]["children"].extend(node["children"])

    for name in order:
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

    collapsed = []
    i = 0
    while i < len(children):
        node = children[i]
        count = 1
        group = [node]
        while (
            i + count < len(children)
            and children[i + count]["name"] == node["name"]
        ):
            group.append(children[i + count])
            count += 1
        if count > 1:
            merged_children = _merge_children(
                [c for n in group for c in n["children"]]
            )
            wall = _wall_from_intervals(group)
            cumulative = sum(n["time"] or 0 for n in group)
            collapsed.append({
                "name": f"{node['name']} ×{count}",
                "time": wall,
                "cumulative": cumulative,
                "start": min(n.get("start", 0) for n in group),   # ← add
                "children": merged_children,
            })
        else:
            collapsed.append({**node, "children": _collapse(node["children"])})
        i += count
    return collapsed


def _collapse_repeating_groups(children: list) -> list | None:
    """Detect [A,B,C,A,B,C,...] patterns and collapse into one [xN] group."""
    n = len(children)
    for size in range(1, n // 2 + 1):
        if n % size != 0:
            continue
        pattern = [c["name"] for c in children[:size]]
        reps = n // size
        if all(
            children[i * size + j]["name"] == pattern[j]
            for i in range(reps)
            for j in range(size)
        ):
            merged_group = []
            for j in range(size):
                instances = [children[i * size + j] for i in range(reps)]
                wall = _wall_from_intervals(instances)
                cumulative = sum(inst["time"] or 0 for inst in instances)
                merged_children = _merge_children(
                    [c for inst in instances for c in inst["children"]]
                )
                merged_group.append({
                    "name": instances[0]["name"],
                    "time": wall,
                    "cumulative": cumulative,
                    "start": min(inst.get("start", 0) for inst in instances),   # ← add
                    "children": _collapse(merged_children),
                })
            group_wall = sum(c["time"] or 0 for c in merged_group)
            group_cumulative = sum(
                c.get("cumulative") or c["time"] or 0 for c in merged_group
            )
            return [{
                "name": f"[x{reps}]",
                "time": group_wall,
                "cumulative": group_cumulative,
                "start": min(c.get("start", 0) for c in merged_group),   # ← add
                "children": merged_group,
            }]
    return None


# ── printer ───────────────────────────────────────────────────────────────────


def print_tree(root: dict) -> None:
    lines: list[tuple[str, str, str]] = []
    total = root["time"] or 1

    def walk(node: dict, prefix: str, is_last: bool) -> None:
        branch = "└─ " if is_last else "├─ "
        t = node["time"] or 0
        cumulative = node.get("cumulative")
        pct = f"{t / total * 100:4.0f}%"
        children = node["children"]

        children_sum = sum(c["time"] or 0 for c in children)
        parallel_hint = (
            "  [parallel]"
            if len(children) > 1 and children_sum > t * 1.2
            else ""
        )

        time_str = (
            f"{fmt(t)} / {fmt(cumulative)}"
            if cumulative and abs(cumulative - t) > 1e-4
            else fmt(t)
        )

        lines.append((prefix + branch + node["name"] + parallel_hint, time_str, pct))
        child_prefix = prefix + ("   " if is_last else "│  ")
        for i, child in enumerate(children):
            walk(child, child_prefix, i == len(children) - 1)

    root_cumulative = root.get("cumulative")
    if root_cumulative and abs(root_cumulative - (root["time"] or 0)) > 1e-4:
        root_time_str = f"{fmt(root['time'] or 0)} / {fmt(root_cumulative)}"
    else:
        root_time_str = fmt(root["time"] or 0)

    lines.append(("─ " + root["name"], root_time_str, "100%"))
    children = _collapse(root["children"])
    for i, child in enumerate(children):
        walk(child, "", i == len(children) - 1)

    width = max(len(n) for n, _, _ in lines) + 2
    time_w = max(len(t) for _, t, _ in lines) + 2
    print(f'\n{"call":<{width}}{"time (wall/cumul)":>{time_w}}   % of total')
    print("─" * (width + time_w + 14))
    for name, t, pct in lines:
        print(f"{name:<{width}}{t:>{time_w}}   {pct}")

# @trace
# def a() -> None:
#     print(1)
