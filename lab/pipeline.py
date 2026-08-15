"""Producer/consumer pipeline: flat call records, WAL-backed local queue.

Unlike perf.trace's nested call tree (held fully in RAM until the top-level
call returns, with every arg/kwarg/result copied inline and retained by its
parent for the whole trace), each call here becomes a flat record the moment
it finishes and is hand off to a bounded local queue backed by an append-only
WAL file. A parent never holds its children's payloads -- only their ids are
implied by parent_id links reconstructed later -- so RAM is bounded by what's
currently in flight, not by the whole trace.

Both `_current_trace_id` and `_current_instance_cache` are scoped to a single
top-level `trace()` invocation (fresh per call, via ContextVar), NOT shared
process-wide. Two bugs motivated this:

* A single process-wide InstanceCache handed out a `self` dedup baseline
  (`__full__`) only the very first time an instance was ever seen -- every
  later trace touching that same long-lived instance got diff-only markers
  with no baseline of its own, so any trace but the first reconstructed an
  incomplete/wrong `self`. Scoping the cache per trace means every trace is
  self-contained: it always owns its own full baseline for anything it
  touches, even if a completely different trace touched the same object
  moments earlier.
* A single fixed trace_id (set once when the pipeline was built) meant every
  concurrent top-level call funneled into one collector-side bucket, and the
  collector's reconstruction (built for one root per trace_id) silently kept
  only the earliest and dropped the rest. Generating a fresh trace_id per
  top-level call, and stamping it onto every record produced under it
  (nested calls read the ambient one), gives each concurrent trace its own
  bucket and its own root.
"""

import json
import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from itertools import count
from pathlib import Path
from queue import Empty, Queue
from time import perf_counter
from typing import Any

from capture import InstanceCache, SpillStore, capture_value

_node_ids = count()
_current: ContextVar[int | None] = ContextVar("_current", default=None)
_current_trace_id: ContextVar[str | None] = ContextVar("_current_trace_id", default=None)
_current_instances: ContextVar[InstanceCache | None] = ContextVar("_current_instances", default=None)


class WalQueue:
    """Bounded in-memory queue backed by an append-only local WAL, split into
    rotating segment files instead of one ever-growing file.

    Normal path: push into the in-memory Queue (fast, bytes-bounded). On
    overflow, instead of dropping the record, it's written straight to the
    current WAL segment, synchronously -- slower for that one record, but
    nothing is lost. The drain thread empties the in-memory queue into the
    WAL on a short interval, so a process crash loses at most the last
    unflushed batch, never the whole in-flight backlog.

    Segmentation exists so disk usage doesn't grow forever: once a segment
    hits `segment_max_bytes`, a new one is opened and the old one is never
    written to again -- permanently closed the moment rotation happens. That
    lets WalShipper (shipper.py) safely delete a closed segment as soon as
    every record in it has been confirmed shipped, without ever racing a
    writer that might still be appending to it (only the CURRENT, highest-
    numbered segment is ever open for writes).

    Purely local durability -- no network calls happen here. Shipping (and
    reclaiming shipped segments) is WalShipper's job, running as an
    independent thread, so a slow/unreachable collector can never block
    writes here (see shipper.py for why that coupling was a real bug in an
    earlier version).
    """

    def __init__(
        self,
        wal_path: Path,
        maxsize_bytes: int = 20_000_000,
        flush_interval: float = 0.02,
        segment_max_bytes: int = 5_000_000,
    ) -> None:
        self.wal_path = wal_path
        self.maxsize_bytes = maxsize_bytes
        self.segment_max_bytes = segment_max_bytes
        self._q: Queue = Queue()
        self._bytes = 0
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()  # guards segment rotation + the write itself
        self._segment_index = 0
        self._segment_bytes = 0
        self._f = open(self._segment_path(0), "a", buffering=1)
        self._stop = threading.Event()
        self.overflow_count = 0
        self.record_count = 0
        self.write_calls = 0
        self._flush_interval = flush_interval
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _segment_path(self, index: int) -> Path:
        return self.wal_path.with_name(f"{self.wal_path.name}.{index:06d}")

    def _write_line(self, payload: str) -> None:
        """Writes one record to the current segment, rotating first if it's
        full. Always called under `_file_lock` -- both the drain thread and
        put()'s overflow path write here, and rotation must never race.
        """
        line = payload + "\n"
        if self._segment_bytes > 0 and self._segment_bytes + len(line) > self.segment_max_bytes:
            self._f.close()
            self._segment_index += 1
            self._segment_bytes = 0
            self._f = open(self._segment_path(self._segment_index), "a", buffering=1)
        self._f.write(line)
        self._segment_bytes += len(line)

    def put(self, record: dict) -> None:
        payload = json.dumps(record, default=str)
        size = len(payload)
        with self._lock:
            if self._bytes + size > self.maxsize_bytes:
                # overflow: write straight to the WAL, synchronously -- never drop
                with self._file_lock:
                    self._write_line(payload)
                self.overflow_count += 1
                self.record_count += 1
                self.write_calls += 1
                return
            self._bytes += size
        self._q.put((payload, size))

    def _drain(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                payload, size = self._q.get(timeout=self._flush_interval)
            except Empty:
                continue
            with self._file_lock:
                self._write_line(payload)
            self.write_calls += 1
            with self._lock:
                self._bytes -= size
            self.record_count += 1
            self._q.task_done()
        with self._file_lock:
            self._f.flush()

    def close(self) -> None:
        self._stop.set()
        self._thread.join()
        self._f.close()

    def segments_on_disk(self) -> list[Path]:
        """All segment files that currently exist -- for measurement/debugging.
        In local-only mode (no WalShipper running to reclaim them), this is
        every segment ever written; with a shipper attached, already-shipped
        segments are deleted and won't appear here. Filters to numeric
        suffixes only -- the same glob also matches the shipper's
        `<wal>.offsets` manifest, which isn't a segment.
        """
        prefix = self.wal_path.name + "."
        return sorted(
            p
            for p in self.wal_path.parent.glob(f"{self.wal_path.name}.*")
            if p.name.startswith(prefix) and p.name[len(prefix):].isdigit()
        )

    def total_bytes_on_disk(self) -> int:
        return sum(p.stat().st_size for p in self.segments_on_disk())


@dataclass
class PipelineConfig:
    max_size_capture: int = 4096
    checkpoint_every: int = 10
    wal_path: Path = Path("lab_wal.jsonl")
    spill_dir: Path = Path("lab_spill")
    queue_maxsize_bytes: int = 5_000_000
    segment_max_bytes: int = 2_000_000
    endpoint_url: str | None = None
    tags: list = None
    api_key: str | None = None
    batch_size: int = 50


def make_pipeline(cfg: PipelineConfig):
    sender = None
    shipper = None
    if cfg.endpoint_url:
        from sender import EndpointSender
        from shipper import WalShipper

        sender = EndpointSender(cfg.endpoint_url, cfg.tags or [], cfg.api_key)
        shipper = WalShipper(cfg.wal_path, sender, batch_size=cfg.batch_size).start()
    spill = SpillStore(cfg.spill_dir, sender=sender)
    wal = WalQueue(cfg.wal_path, maxsize_bytes=cfg.queue_maxsize_bytes, segment_max_bytes=cfg.segment_max_bytes)

    def capture_positional(a: Any, trace_id: str | None) -> Any:
        # bound-method receivers (and similarly shaped objects) go through
        # the CURRENT TRACE's dedup cache -- never a process-wide one, so
        # every trace owns its own full baseline (see module docstring).
        if hasattr(a, "__dict__") and vars(a):
            budget = [cfg.max_size_capture]
            from capture import _budgeted

            snap = _budgeted(vars(a), budget)
            if not isinstance(snap, dict):
                snap = {"_": snap}
            cache = _current_instances.get()
            return cache.diff_capture(a, snap) if cache is not None else {"__full__": snap, "instance_seq": -1}
        return capture_value(a, cfg.max_size_capture, spill, trace_id)

    def capture_args(args: tuple, kwargs: dict, trace_id: str | None) -> tuple:
        cargs = [capture_positional(a, trace_id) for a in args]
        ckwargs = {k: capture_value(v, cfg.max_size_capture, spill, trace_id) for k, v in kwargs.items()}
        return cargs, ckwargs

    def trace(func, on_root: Any = None):
        """`on_root(trace_id, instance_cache)`, if given, is called the
        moment a top-level (non-nested) call starts -- the only way a caller
        can learn either, since by the time the call returns both have
        already been reset back (see the `finally` below). Useful for
        correlating a request to its trace_id for logging, or (as in the lab
        benchmarks) for later querying the collector or reading dedup stats
        for this specific trace.
        """
        name = func.__qualname__

        @wraps(func)
        def wrapper(*args, **kwargs):
            is_root = _current.get() is None
            trace_token = instances_token = None
            if is_root:
                # Fresh identity for this trace: a new trace_id so concurrent
                # top-level calls never collide in the collector, and a fresh
                # InstanceCache so this trace's dedup baselines are entirely
                # its own -- not inherited from whatever trace last touched
                # the same long-lived instance.
                new_trace_id = str(uuid.uuid4())
                trace_token = _current_trace_id.set(new_trace_id)
                instances_token = _current_instances.set(InstanceCache(checkpoint_every=cfg.checkpoint_every))
                if on_root is not None:
                    on_root(new_trace_id, _current_instances.get())
            trace_id = _current_trace_id.get()

            node_id = next(_node_ids)
            parent_id = _current.get()
            token = _current.set(node_id)
            start = perf_counter()
            exc: BaseException | None = None
            result = None
            try:
                result = func(*args, **kwargs)
                return result
            except BaseException as e:
                exc = e
                raise
            finally:
                elapsed = perf_counter() - start
                _current.reset(token)
                cargs, ckwargs = capture_args(args, kwargs, trace_id)
                record = {
                    "id": node_id,
                    "parent_id": parent_id,
                    "trace_id": trace_id,
                    "name": name,
                    "time": elapsed,
                    "args": cargs,
                    "kwargs": ckwargs,
                }
                if exc is None:
                    record["result"] = capture_value(result, cfg.max_size_capture, spill, trace_id)
                else:
                    record["exception"] = repr(exc)
                wal.put(record)
                if is_root:
                    if sender is not None:
                        sender.send_end(trace_id, "error" if exc is not None else "ok")
                    _current_instances.reset(instances_token)
                    _current_trace_id.reset(trace_token)

        return wrapper

    return trace, wal, spill, sender, shipper
