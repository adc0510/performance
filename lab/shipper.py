"""Tails the local WAL's segment files and ships records to the collector,
entirely independently of the thread that writes them -- and reclaims disk
space by deleting each segment once every record in it is confirmed shipped.

Builds on two earlier fixes:

1. Coupling: a slow/unreachable collector must never block the thread
   responsible for local durability. Writer and shipper are different
   threads operating on different segment files (the writer only ever
   touches the newest one), so network latency only ever delays shipping.

2. Silent loss on send failure: this tails by (segment, byte offset),
   persisted to `<wal>.offsets`, and only advances that offset once a batch
   is CONFIRMED sent -- retrying with backoff otherwise.

3. NEW -- unbounded disk growth: without this, every record ever captured
   stays on disk forever (the same class of bug -- unbounded growth from
   never releasing what's already been safely handed off -- that motivated
   this whole redesign, just moved from RAM to disk). Once a segment is
   fully shipped AND it's not the current (highest-numbered, still being
   written) segment, it's deleted. WalQueue guarantees a segment is never
   written to again once it rotates past it, so deleting an old segment can
   never race a concurrent write to that same file -- only the newest
   segment is ever left alone.
"""

import json
import threading
import time
from pathlib import Path


class WalShipper:
    def __init__(
        self,
        wal_path: Path,
        sender,
        batch_size: int = 50,
        poll_interval: float = 0.05,
        max_backoff: float = 5.0,
    ) -> None:
        self.wal_path = wal_path
        self.sender = sender
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff
        self.offsets_path = wal_path.with_name(wal_path.name + ".offsets")
        self._stop = threading.Event()
        self.records_shipped = 0
        self.batches_retried = 0
        self.segments_deleted = 0
        self.bytes_reclaimed = 0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "WalShipper":
        self._thread.start()
        return self

    def _segments(self) -> list[tuple[int, Path]]:
        """All segment files, oldest first, as (index, path)."""
        prefix = self.wal_path.name + "."
        segs = []
        for p in self.wal_path.parent.glob(f"{self.wal_path.name}.*"):
            suffix = p.name[len(prefix):]
            if suffix.isdigit():
                segs.append((int(suffix), p))
        return sorted(segs)

    def _load_offsets(self) -> dict:
        if self.offsets_path.exists():
            try:
                return {int(k): v for k, v in json.loads(self.offsets_path.read_text()).items()}
            except (ValueError, json.JSONDecodeError):
                return {}
        return {}

    def _save_offsets(self, offsets: dict) -> None:
        self.offsets_path.write_text(json.dumps(offsets))

    def _read_batch(self, path: Path, offset: int) -> tuple[list, int]:
        """Reads up to `batch_size` complete (newline-terminated) lines
        starting at `offset`. A trailing partial line (the writer mid-write,
        only possible on the current/newest segment) is left for next poll.
        """
        records = []
        new_offset = offset
        with open(path, "rb") as f:
            f.seek(offset)
            while len(records) < self.batch_size:
                line = f.readline()
                if not line.endswith(b"\n"):
                    break
                records.append(json.loads(line))
                new_offset += len(line)
        return records, new_offset

    def _run(self) -> None:
        offsets = self._load_offsets()
        backoff = 0.2
        while True:
            segs = self._segments()
            if not segs:
                if self._stop.is_set():
                    return
                time.sleep(self.poll_interval)
                continue

            newest_index = segs[-1][0]
            progressed = False
            failed = False

            for index, path in segs:
                offset = offsets.get(index, 0)
                records, new_offset = self._read_batch(path, offset)
                if records:
                    if self.sender.send_batch(records):
                        offsets[index] = new_offset
                        self._save_offsets(offsets)
                        self.records_shipped += len(records)
                        progressed = True
                    else:
                        self.batches_retried += 1
                        failed = True
                        break  # back off before hammering the collector again

                # Reclaim: only a CLOSED segment (writer has moved past it),
                # and only once every byte in it has been confirmed shipped.
                if index < newest_index and offsets.get(index, 0) >= path.stat().st_size:
                    size = path.stat().st_size
                    path.unlink(missing_ok=True)
                    offsets.pop(index, None)
                    self._save_offsets(offsets)
                    self.segments_deleted += 1
                    self.bytes_reclaimed += size
                    progressed = True

            if failed:
                time.sleep(min(backoff, self.max_backoff))
                backoff = min(backoff * 2, self.max_backoff)
                continue
            backoff = 0.2

            if not progressed:
                if self._stop.is_set() and self._fully_caught_up(segs, offsets):
                    return
                time.sleep(self.poll_interval)

    def _fully_caught_up(self, segs: list, offsets: dict) -> bool:
        return all(offsets.get(i, 0) >= p.stat().st_size for i, p in segs if p.exists())

    def pending_bytes(self) -> int:
        """How far behind shipping currently is: bytes written to the WAL
        but not yet confirmed sent to the collector, summed across every
        segment. Safe to poll from any thread at any time (read-only, off
        disk) -- meant for monitoring/alerting: a healthy shipper keeps this
        near zero; a value that keeps climbing means the collector can't
        keep up (or is down), which is exactly the condition #3's disk-growth
        fix depends on eventually clearing -- this is the signal an operator
        would want well before disk becomes the problem.
        """
        offsets = self._load_offsets()
        total = 0
        for index, path in self._segments():
            if not path.exists():
                continue
            total += max(path.stat().st_size - offsets.get(index, 0), 0)
        return total

    def pending_segments(self) -> int:
        """How many segments currently exist but aren't yet fully shipped
        (a coarser, cheaper-to-reason-about companion to pending_bytes)."""
        offsets = self._load_offsets()
        return sum(1 for index, path in self._segments() if path.exists() and offsets.get(index, 0) < path.stat().st_size)

    def stop(self, timeout: float = 30) -> None:
        """Signals shutdown and waits for every already-written byte to be
        shipped before returning. Callers must call this AFTER the writer
        (WalQueue.close()) has already stopped -- only then is it safe to
        treat what was the "current" segment as permanently closed too, so
        the final reclaim pass can delete it like any other fully-shipped
        segment instead of leaving it behind forever (see _final_reclaim).
        """
        self._stop.set()
        self._thread.join(timeout=timeout)
        self._final_reclaim()

    def _final_reclaim(self) -> None:
        """One last pass, once the writer is confirmed done: by now every
        segment (including what was the active one during normal operation)
        is permanently closed, so any that's fully shipped can be deleted --
        otherwise the final segment of every trace would linger on disk
        forever, one bounded leak per pipeline lifetime instead of zero.
        """
        offsets = self._load_offsets()
        changed = False
        for index, path in self._segments():
            if not path.exists():
                continue
            if offsets.get(index, 0) >= path.stat().st_size:
                size = path.stat().st_size
                path.unlink(missing_ok=True)
                offsets.pop(index, None)
                self.segments_deleted += 1
                self.bytes_reclaimed += size
                changed = True
        if changed:
            self._save_offsets(offsets)
