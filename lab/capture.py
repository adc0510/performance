"""Budget-bounded, spill-aware value capture + instance dedup with checkpointing.

Producer-side capture for the fixed architecture:

* `capture_value` converts a value under a byte budget so a single oversized
  value is never fully copied into RAM before being size-checked -- anything
  that blows the budget is streamed straight to its own spill file instead
  (`_full` + `SpillStore`), and only a small reference is kept inline.
* `InstanceCache` dedups repeated captures of the same live instance (e.g.
  `self` across many method calls) via a weakref-keyed cache, so id() reuse
  after garbage collection can't merge two different objects' history, and
  periodic full-snapshot checkpoints mean a lost/evicted entry only costs data
  back to the last checkpoint, not the whole diff chain.
"""

import json
import weakref
from pathlib import Path
from typing import Any


class SpillStore:
    """Writes oversized values to their own file, immediately, on the producer.

    The local file is always written first -- it's the crash/loss backstop
    and is fast (local disk, no network round-trip on the hot path). If a
    `sender` is configured, the file is additionally handed off for upload
    from its own background thread (see EndpointSender.send_blob_from_file),
    so shipping a spilled value to the collector never blocks the traced call.
    """

    def __init__(self, directory: Path, sender: Any = None) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.sender = sender
        self._n = 0
        self.bytes_written = 0
        self.write_calls = 0

    def write(self, value: Any, trace_id: str | None = None) -> dict:
        self._n += 1
        path = self.directory / f"spill_{self._n}.json"
        with open(path, "w") as f:
            json.dump(value, f, default=str)
        self.write_calls += 1  # one file, one write() worth of IO for this value
        size = path.stat().st_size
        self.bytes_written += size
        ref = str(path)
        if self.sender is not None:
            self.sender.send_blob_from_file(ref, path, trace_id)
        return {"__spilled__": ref, "size": size}


def _budgeted(value: Any, budget: list, depth: int = 0, seen: frozenset = frozenset(), max_depth: int = 20) -> Any:
    """Bounded conversion: never copies more than ~budget[0] bytes total."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if budget[0] <= 0:
            return "<truncated: budget exceeded>"
        if len(value) > budget[0]:
            out = value[: budget[0]]
            budget[0] = -1
            return out
        budget[0] -= len(value)
        return value
    if isinstance(value, (bytes, bytearray)):
        n = max(budget[0], 0)
        out = repr(bytes(value[:n]))
        if n < len(value):
            budget[0] = -1
        else:
            budget[0] -= n
        return out
    if depth >= max_depth:
        return repr(value)
    if id(value) in seen:
        return f"<circular {type(value).__name__}>"
    if budget[0] <= 0:
        return f"<{type(value).__name__} truncated: budget exceeded>"
    if isinstance(value, dict):
        seen2 = seen | {id(value)}
        out = {}
        for k, v in value.items():
            if budget[0] <= 0:
                break
            out[k if isinstance(k, str) else repr(k)] = _budgeted(v, budget, depth + 1, seen2, max_depth)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        seen2 = seen | {id(value)}
        out = []
        for v in value:
            if budget[0] <= 0:
                break
            out.append(_budgeted(v, budget, depth + 1, seen2, max_depth))
        return out
    if hasattr(value, "__dict__") and vars(value):
        seen2 = seen | {id(value)}
        return {"__type__": type(value).__name__, **_budgeted(vars(value), budget, depth + 1, seen2, max_depth)}
    return repr(value)


def _full(value: Any, depth: int = 0, seen: frozenset = frozenset(), max_depth: int = 20) -> Any:
    """Unbounded conversion -- used only once we already know a value needs spilling."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return repr(value)
    if depth >= max_depth:
        return repr(value)
    if id(value) in seen:
        return f"<circular {type(value).__name__}>"
    if isinstance(value, dict):
        seen2 = seen | {id(value)}
        return {(k if isinstance(k, str) else repr(k)): _full(v, depth + 1, seen2, max_depth) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        seen2 = seen | {id(value)}
        return [_full(v, depth + 1, seen2, max_depth) for v in value]
    if hasattr(value, "__dict__") and vars(value):
        seen2 = seen | {id(value)}
        return {"__type__": type(value).__name__, **_full(vars(value), depth + 1, seen2, max_depth)}
    return repr(value)


def capture_value(value: Any, max_size: int, spill: SpillStore | None, trace_id: str | None = None) -> Any:
    """Convert `value`, keeping it inline if small, spilling it whole if not.

    The budgeted pass bounds how much gets copied while we're still deciding.
    Only once we know a value is genuinely oversized do we pay for a full
    conversion -- and that full copy is written to disk and released
    immediately, never held in the in-memory trace. `trace_id` is threaded
    through to the spill store so an uploaded blob is tagged with the trace it
    actually belongs to (see SpillStore.write).
    """
    budget = [max_size]
    jv = _budgeted(value, budget)
    if budget[0] > 0:
        return jv
    if spill is not None:
        full = _full(value)
        ref = spill.write(full, trace_id)
        del full
        return ref
    return f"<{type(value).__name__} too large: dropped>"


class InstanceCache:
    """Dedups repeated captures of the same live instance.

    Keyed by id(obj) but validated via a weakref with a death callback that
    removes the entry the moment the object is actually collected -- so a
    later, different object reusing the same id can never be mistaken for the
    original and merged into its history.
    """

    def __init__(self, checkpoint_every: int = 10) -> None:
        self.checkpoint_every = checkpoint_every
        self._entries: dict = {}
        self._next_seq = 0
        self.full_snapshots = 0
        self.diffs = 0

    def _on_die(self, oid: int):
        def _cb(_ref: Any) -> None:
            self._entries.pop(oid, None)

        return _cb

    def diff_capture(self, obj: Any, snapshot: dict) -> dict:
        """Returns a marker tagged with a stable, monotonic `instance_seq`.

        `id(obj)` is only meaningful *within this process* for as long as the
        object is alive (handled locally via the weakref callback above) --
        it means nothing to a remote collector receiving records out of
        process, where two different objects could easily produce diffs that
        look like they belong to the same instance if id() were the only key
        sent over the wire. `instance_seq` is assigned once, monotonically,
        the first time an instance is seen, and travels with every marker for
        that instance so the collector can rebuild each instance's diff chain
        unambiguously without needing (or trusting) raw ids.
        """
        oid = id(obj)
        entry = self._entries.get(oid)
        if entry is None or entry["ref"]() is not obj:
            ref = weakref.ref(obj, self._on_die(oid))
            seq = self._next_seq
            self._next_seq += 1
            self._entries[oid] = {"ref": ref, "last": snapshot, "since_checkpoint": 0, "seq": seq}
            self.full_snapshots += 1
            return {"__full__": snapshot, "instance_seq": seq}
        diff = {k: v for k, v in snapshot.items() if entry["last"].get(k) != v}
        entry["since_checkpoint"] += 1
        entry["last"] = snapshot
        seq = entry["seq"]
        if entry["since_checkpoint"] >= self.checkpoint_every:
            entry["since_checkpoint"] = 0
            self.full_snapshots += 1
            return {"__full__": snapshot, "instance_seq": seq}
        self.diffs += 1
        return {"__diff__": diff, "instance_seq": seq}
