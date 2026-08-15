"""Minimal collector implementing the new streamed wire format.

Accepts the "batch" / "blob" / "end" envelopes sender.EndpointSender posts,
and reconstructs the same shape perf._send_trace used to send in one shot --
{"name", "time", "children": [...], "args", "kwargs", "result"} -- by walking
flat records via parent_id and resolving each __diff__/__full__/__spilled__
marker. This is what proves the streamed format is a lossless, format-
compatible replacement for the old single nested-tree POST, just delivered
incrementally instead of all at once at the end.

Deliberately a toy HTTP server (stdlib only, in-process, threaded) -- good
enough to measure request/byte counts and verify reconstruction in the lab;
not meant to represent a production collector's storage layer.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class TraceStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.records: dict = {}  # trace_id -> {id: record}
        self.blobs: dict = {}  # trace_id -> {ref: value}
        self.status: dict = {}  # trace_id -> "ok"/"error"
        self.requests_received = 0
        self.bytes_received = 0

    def add_batch(self, records: list) -> None:
        """Buckets each record by its OWN trace_id, not a shared envelope-level
        one -- a single batch legitimately mixes records from multiple
        concurrent traces (see sender.py's module docstring), so bucketing
        by envelope would silently merge unrelated traces together.
        """
        with self.lock:
            for r in records:
                self.records.setdefault(r["trace_id"], {})[r["id"]] = r

    def add_blob(self, trace_id: str, ref: str, value) -> None:
        with self.lock:
            self.blobs.setdefault(trace_id, {})[ref] = value

    def set_status(self, trace_id: str, status: str) -> None:
        with self.lock:
            self.status[trace_id] = status

    def reconstruct(self, trace_id: str) -> dict | None:
        with self.lock:
            records = dict(self.records.get(trace_id, {}))
            blobs = dict(self.blobs.get(trace_id, {}))
        if not records:
            return None

        instance_full: dict = {}

        def resolve(v):
            if isinstance(v, dict):
                if "__spilled__" in v:
                    return blobs.get(v["__spilled__"], f"<blob {v['__spilled__']} not yet received>")
                if "__full__" in v:
                    instance_full[v["instance_seq"]] = v["__full__"]
                    return v["__full__"]
                if "__diff__" in v:
                    base = instance_full.get(v["instance_seq"], {})
                    merged = {**base, **v["__diff__"]}
                    instance_full[v["instance_seq"]] = merged
                    return merged
            return v

        children_of: dict = {}
        roots = []
        for rid, r in records.items():
            children_of.setdefault(r["parent_id"], []).append(rid)
            if r["parent_id"] is None or r["parent_id"] not in records:
                roots.append(rid)

        def build(rid: int) -> dict:
            r = records[rid]
            node = {
                "name": r["name"],
                "time": r["time"],
                "args": [resolve(a) for a in r["args"]],
                "kwargs": {k: resolve(v) for k, v in r["kwargs"].items()},
                "children": [build(cid) for cid in sorted(children_of.get(rid, []))],
            }
            if "exception" in r:
                node["exception"] = r["exception"]
            else:
                node["result"] = resolve(r.get("result"))
            return node

        # id order == capture order == a stable proxy for "this one ran first";
        # the true root is the earliest record with no known parent.
        root_id = min(roots)
        return build(root_id)


def make_handler(store: TraceStore):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            store.requests_received += 1
            store.bytes_received += length
            payload = json.loads(body)
            kind = payload.get("kind")
            if kind == "batch":
                store.add_batch(payload["records"])
            elif kind == "blob":
                store.add_blob(payload["trace_id"], payload["ref"], payload["value"])
            elif kind == "end":
                store.set_status(payload["trace_id"], payload["status"])
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:
            trace_id = self.path.rsplit("/", 1)[-1]
            tree = store.reconstruct(trace_id)
            self.send_response(200 if tree else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(tree).encode("utf-8"))

    return Handler


def start_collector(port: int = 0, store: TraceStore | None = None) -> tuple:
    """Pass an existing `store` to simulate the collector process restarting
    while its backing storage (a real collector's DB, unlike this toy
    in-memory dict) persists across the restart.
    """
    store = store or TraceStore()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, store
