"""Streams flat trace records to a collector-controlled ENDPOINT_URL, instead
of building the old {"status", "tags", "trace": <nested tree>} payload and
POSTing it once at the end.

New wire format (three envelope kinds, all POSTed as JSON to the same URL):

  {"kind": "batch", "tags": [...],
   "records": [{"id", "parent_id", "trace_id", "name", "time", "args",
                "kwargs", "result"/"exception"}, ...]}

  {"kind": "blob", "trace_id": ..., "ref": "<spill ref>", "value": <full value>}

  {"kind": "end", "trace_id": ..., "status": "ok"|"error"}

Each RECORD (not the batch envelope) carries its own trace_id -- a single
batch/WAL file/queue is shared process-wide across every top-level traced
call, so two concurrent traces' records can legitimately land in the same
batch. Bucketing must happen per-record on the collector side; a batch-level
trace_id would silently merge concurrent traces into one (see pipeline.py's
`_current_trace_id`, freshly generated per top-level `trace()` invocation,
for where each record's trace_id actually comes from).

A record's args/kwargs/result may contain:
  * {"__full__": {...}, "instance_seq": N}  -- full self-like snapshot
  * {"__diff__": {...}, "instance_seq": N}  -- only the changed keys since
    the last snapshot for that instance_seq
  * {"__spilled__": "<ref>", "size": N}     -- value shipped separately as a
    "blob" envelope; the collector resolves it by ref

instance_seq is scoped to a single trace (a fresh InstanceCache per top-level
`trace()` call -- see pipeline.py) specifically so every trace's reconstruction
is self-contained: it always includes its own __full__ baseline for any
instance it touches, rather than depending on a baseline emitted under a
different trace_id that a given collector query might not have visibility
into (or might process-wide dedup have merged away).

Mirrors perf._send_trace's own conventions: zero extra dependencies
(urllib.request only), X-API-Key header. Unlike _send_trace, a failed send
here is NOT simply swallowed -- send_batch/_post report success/failure so
callers (WalShipper for batches, the retry loop below for blobs and "end")
can retry instead of silently losing data. Nothing here may ever raise into
the traced call or the drain thread, though -- exceptions are always caught.

Blob and "end" sends retry INDEFINITELY (capped backoff, no attempt limit) --
same policy as WalShipper's batch retries. There is no good universal answer
to "how many attempts is too many" baked into the library; that's a
deployment decision, made instead by whoever calls join(timeout=...) at
shutdown. A short outage is invisible (it just retries and succeeds); an
outage longer than the caller's chosen timeout is reported as still-pending
(on_giveup fires) rather than either blocking shutdown forever or silently
abandoning data after an arbitrary fixed delay.

EndpointSender itself is trace-agnostic and long-lived -- one instance shared
by the whole pipeline across every trace, not recreated per call. trace_id is
passed explicitly to each send_* method instead of being baked into the
sender, since a single sender legitimately ships records for many
independent (possibly concurrent) traces over its lifetime.
"""

import json
import sys
import threading
import time
import urllib.request
from pathlib import Path


class EndpointSender:
    def __init__(self, url: str, tags: list, api_key: str | None = None) -> None:
        self.url = url
        self.tags = tags
        self.api_key = api_key
        self.batches_sent = 0
        self.blobs_sent = 0
        self.blobs_abandoned = 0  # retries exhausted; file deliberately left on disk, not deleted
        self.ends_abandoned = 0
        self.spill_bytes_reclaimed = 0
        self.bytes_sent = 0  # only successful posts
        self.requests_sent = 0  # only successful posts
        self.failed_attempts = 0
        # (thread, its own stop Event) per outstanding retry -- NOT one
        # shared Event: join() must be able to give up on whatever's pending
        # right now without permanently disabling retries for every send
        # this long-lived sender is asked to make afterward.
        self._pending: list[tuple[threading.Thread, threading.Event]] = []
        self._pending_lock = threading.Lock()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _post(self, payload: dict) -> bool:
        """POSTs `payload`. Returns whether it actually succeeded -- callers
        that can't afford to lose the data (batches, blobs) use this to
        decide whether to retry, instead of treating "attempted" as "done".
        """
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        req = urllib.request.Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as e:  # never let telemetry break the caller
            print(f"[perf] failed to POST to {self.url}: {e}", file=sys.stderr)
            self.failed_attempts += 1
            return False
        self.bytes_sent += len(data)
        self.requests_sent += 1
        return True

    def send_batch(self, records: list) -> bool:
        """Called by WalShipper, off the traced call's hot path AND off the
        local WAL-writing thread -- so a slow/unreachable collector only
        delays shipping, never local capture (see WalShipper in shipper.py).
        `records` may span multiple concurrent traces; each carries its own
        `trace_id` already (see module docstring).
        """
        if not records:
            return True
        ok = self._post({"kind": "batch", "tags": self.tags, "records": records})
        if ok:
            self.batches_sent += 1
        return ok

    def _prune_pending_locked(self) -> None:
        """Drops finished (thread, stop_event) pairs. Called under
        `_pending_lock` whenever the list is touched, so a long-lived sender
        making many blob/end sends over its lifetime doesn't accumulate an
        ever-growing list of dead thread handles -- the same "release what's
        already done, don't just keep appending" discipline as the WAL/spill
        reclaim logic elsewhere in this pipeline.
        """
        self._pending = [(t, e) for t, e in self._pending if t.is_alive()]

    def _post_with_retry(self, payload: dict, on_success, give_up_msg: str, on_giveup=None) -> None:
        """Runs in its own daemon thread -- never blocks the caller, so a
        slow/unreachable collector only delays this send, never the traced
        call or the WAL. Retries indefinitely with capped exponential
        backoff -- there's no fixed attempt limit, matching WalShipper's own
        batch-retry policy (see module docstring for why). Retrying stops
        only when THIS call's own stop Event is set (by join(), after its
        timeout elapses) -- a fresh Event per call, so giving up on one
        outstanding send can never affect any other send in flight, past or
        future. When it does stop, `on_giveup` fires so the caller can
        report/track whatever is still unsent (the caller-side data this
        payload came from -- a spill file, or nothing at all for "end" -- is
        what's left for out-of-band recovery; see the blob/`end` callers
        below for what that means in each case).
        """
        stop_event = threading.Event()

        def _do() -> None:
            backoff = 0.2
            attempt = 0
            while not stop_event.is_set():
                attempt += 1
                if self._post(payload):
                    on_success()
                    return
                stop_event.wait(backoff)  # sleep, but wake immediately if told to stop
                backoff = min(backoff * 2, 5.0)
            print(f"[perf] {give_up_msg} after {attempt} attempts (shutdown requested)", file=sys.stderr)
            if on_giveup is not None:
                on_giveup()

        t = threading.Thread(target=_do, daemon=True)
        with self._pending_lock:
            self._prune_pending_locked()
            self._pending.append((t, stop_event))
        t.start()

    def send_blob_from_file(self, ref: str, path: Path, trace_id: str | None) -> None:
        """Uploads a spilled value read back from its local spill file.

        The local file is deleted only once the upload is CONFIRMED -- until
        then it's the crash/loss backstop. If every retry is exhausted, the
        file is deliberately left in place (not deleted) as the last-resort
        record of what still needs shipping; this is the one case a spill
        file survives, and it's an explicit, logged exception, not silent
        growth -- see EndpointSender.blobs_abandoned.
        """
        with open(path) as f:
            value = json.load(f)
        payload = {"kind": "blob", "trace_id": trace_id, "ref": ref, "value": value}

        def on_success() -> None:
            self.blobs_sent += 1
            self.spill_bytes_reclaimed += path.stat().st_size
            path.unlink(missing_ok=True)

        def on_giveup() -> None:
            self.blobs_abandoned += 1

        self._post_with_retry(
            payload,
            on_success,
            f"giving up shipping blob {ref}; it remains on local disk at {path} for out-of-band replay",
            on_giveup=on_giveup,
        )

    def send_end(self, trace_id: str, status: str) -> None:
        """Marks a trace complete. Retried the same way batches/blobs are --
        an "end" lost to a transient outage would leave a trace whose data
        all arrived but whose completion is never recorded, which is exactly
        the kind of silent gap this whole redesign exists to avoid.
        """
        payload = {"kind": "end", "trace_id": trace_id, "status": status}

        def on_giveup() -> None:
            self.ends_abandoned += 1

        self._post_with_retry(
            payload, lambda: None, f"giving up marking trace {trace_id} as {status}", on_giveup=on_giveup
        )

    def join(self, timeout: float = 30) -> None:
        """Waits up to `timeout` for every send outstanding RIGHT NOW to be
        confirmed. Whatever's still retrying after that is told to stop (its
        own stop_event only -- never anything started after this call) --
        `on_giveup` fires for it (counted in blobs_abandoned/ends_abandoned)
        instead of retrying forever unobserved. This is the one place that
        turns "retry indefinitely" into a bounded wait: the caller decides
        how long is long enough, not a fixed constant baked into the sender.
        A sender is long-lived and may be asked to send again after this
        returns -- only the pending set captured at the start of this call is
        affected.
        """
        with self._pending_lock:
            self._prune_pending_locked()
            pending = list(self._pending)

        deadline = time.monotonic() + timeout
        for t, _ in pending:
            t.join(timeout=max(deadline - time.monotonic(), 0))
        for t, stop_event in pending:
            if t.is_alive():
                stop_event.set()
        for t, _ in pending:
            t.join(timeout=2)  # brief grace period for on_giveup to run and log

        with self._pending_lock:
            self._prune_pending_locked()
