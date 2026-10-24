"""TRACE_LOCALS stress test: realistic logic, threads, async, processes.

Run from the repo root:

    uv run python examples/locals_stress.py

An order-fulfilment simulator. Every scenario is its own @perf.trace entry
point with TRACE_LOCALS scoped to its functions. After all scenarios run, the
written traces are read back and checked against the values the code must
have taken, so this is a test, not just a demo:

  PASS / FAIL  the recorded locals match (or don't) what the code did
  OBSERVE      behaviour worth knowing, with no single right answer

Needs Python 3.12+ (sys.monitoring).
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import queue
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import perf

OUT = Path(__file__).parent / "out" / "locals_stress"
perf.OUTPUT_DIR = str(OUT)
perf.HOT_SPOTS = 0
perf.MIN_PCT = 1.0  # print only the root line of each tree: the checks are the output


# ═════════════════════════════════════════════════════════════════════════════
# A. Sync business logic: classes, match, try/except/else/finally, continue,
#    nonlocal closures, walrus, while/else, globals, context managers
# ═════════════════════════════════════════════════════════════════════════════

ORDERS_SEEN = 0


class OutOfStock(Exception):
    pass


@dataclass
class LineItem:
    sku: str
    qty: int
    unit_price: int


@dataclass
class Order:
    id: int
    customer: str
    lines: list
    coupon: str | None = None
    tags: list = field(default_factory=list)


class Inventory:
    def __init__(self, stock):
        self.stock = dict(stock)
        self.reserved = {}

    def available(self, sku):
        return self.stock.get(sku, 0)

    def reserve(self, sku, qty):
        have = self.stock.get(sku, 0)
        if qty > have:
            raise OutOfStock(f"{sku}: wanted {qty}, have {have}")
        self.stock[sku] = have - qty
        self.reserved[sku] = self.reserved.get(sku, 0) + qty
        return self.stock[sku]


@contextmanager
def audit(log, label):
    log.append(f"begin {label}")
    yield len(log)
    log.append(f"end {label}")


def loyalty(subtotal, order):
    return subtotal // 10 if order.customer == "gold" else 0


def coupon(subtotal, order):
    return 5 if order.coupon == "SAVE5" else 0


def bulk(subtotal, order):
    return 3 if subtotal > 25 else 0


def apply_rules(subtotal, order, rules):
    discount = 0
    applied = []

    def bump(amount, why):
        nonlocal discount
        discount += amount
        applied.append(why)

    i = 0
    while i < len(rules):
        rule = rules[i]
        if (amt := rule(subtotal, order)) > 0:
            bump(amt, rule.__name__)
        i += 1
    else:
        cap = subtotal // 4
    if discount > cap:
        discount = cap
    return discount, applied


def process_order(order, inventory, rules, log):
    global ORDERS_SEEN
    ORDERS_SEEN += 1
    reserved = []
    subtotal = 0
    warnings = []
    attempts = 0
    with audit(log, f"order-{order.id}") as position:
        for idx, line in enumerate(order.lines):
            match line:
                case LineItem(sku=sku, qty=qty) if qty <= 0:
                    warnings.append(f"skip {sku}")
                    continue
                case LineItem(sku=sku, qty=qty, unit_price=price):
                    pass
            try:
                inventory.reserve(sku, qty)
            except OutOfStock as exc:
                warnings.append(str(exc))
                qty = inventory.available(sku)
                if not qty:
                    continue
                inventory.reserve(sku, qty)
            else:
                order.tags.append(f"full:{sku}")
            finally:
                attempts = idx + 1
            reserved.append((sku, qty))
            subtotal += qty * price
    discount, applied = apply_rules(subtotal, order, [loyalty, coupon, bulk])
    total = subtotal - discount
    status = "partial" if warnings else "ok"
    return {"total": total, "status": status, "reserved": reserved, "applied": applied}


@perf.trace(
    OUTPUT_FILE="a_business",
    TRACE_LOCALS=["__main__.process_order", "__main__.apply_rules*", "Inventory.reserve"],
)
def scenario_business():
    inventory = Inventory({"A": 10, "C": 3, "D": 0})
    order = Order(
        1, "gold",
        [LineItem("A", 2, 10), LineItem("B", 0, 5), LineItem("C", 5, 3), LineItem("D", 1, 7)],
        coupon="SAVE5",
    )
    log = []
    return process_order(order, inventory, None, log)


# ═════════════════════════════════════════════════════════════════════════════
# B. Recursion and generators (yield, send, early close)
# ═════════════════════════════════════════════════════════════════════════════

CATALOG = {
    "name": "root", "sales": 1, "children": [
        {"name": "electronics", "sales": 2, "children": [
            {"name": "phones", "sales": 10},
            {"name": "laptops", "sales": 20},
        ]},
        {"name": "books", "sales": 5},
    ],
}


def rollup(node, depth=0):
    total = node.get("sales", 0)
    names = [node["name"]]
    for child in node.get("children", []):
        sub_total, sub_names = rollup(child, depth + 1)
        total += sub_total
        names.extend(sub_names)
    level = "leaf" if not node.get("children") else "branch"
    return total, names


def batched(items, size):
    batch = []
    for item in items:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def running_mean():
    count = 0
    total = 0
    value = yield
    while True:
        count += 1
        total += value
        value = yield total // count


@perf.trace(
    OUTPUT_FILE="b_recursion_generators",
    TRACE_LOCALS=["__main__.rollup", "__main__.batched", "__main__.running_mean"],
)
def scenario_recursion_generators():
    grand, names = rollup(CATALOG)
    batches = list(batched(range(7), 3))
    stats = running_mean()
    next(stats)
    means = [stats.send(v) for v in (10, 20, 30)]
    stats.close()
    return grand, names, batches, means


# ═════════════════════════════════════════════════════════════════════════════
# C. Threads: queue workers sharing a dict, plus a ThreadPoolExecutor
# ═════════════════════════════════════════════════════════════════════════════

def score_job(job):
    weight = job["id"] % 5 + 1
    if job["id"] % 7 == 0:
        raise ValueError(f"bad job {job['id']}")
    score = 0
    for step in range(weight):
        score += job["base"] * (step + 1)
    return score


def worker(worker_id, jobs, results, lock):
    processed = 0
    errors = []
    while True:
        try:
            job = jobs.get_nowait()
        except queue.Empty:
            break
        try:
            score = score_job(job)
        except ValueError as exc:
            errors.append(f"{job['id']}:{exc}")
            continue
        with lock:
            results[job["id"]] = (worker_id, score)
        processed += 1
        time.sleep(0.001)  # let the other workers interleave
    return processed, errors


N_JOBS = 20


@perf.trace(OUTPUT_FILE="c_threads", TRACE_LOCALS=["__main__.worker", "__main__.score_job"])
def scenario_threads():
    jobs = queue.Queue()
    for i in range(1, N_JOBS + 1):
        jobs.put({"id": i, "base": i * 10})
    results, lock = {}, threading.Lock()
    threads = [threading.Thread(target=worker, args=(w, jobs, results, lock)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with ThreadPoolExecutor(max_workers=3) as pool:
        pooled = list(pool.map(score_job, [{"id": i, "base": 1} for i in (1, 2, 3, 4)]))
    return len(results), pooled


# ═════════════════════════════════════════════════════════════════════════════
# D. Async: concurrent coroutines, retries, async with, async for,
#    cancellation, to_thread
# ═════════════════════════════════════════════════════════════════════════════

BASE = {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500}


class RateLimiter:
    def __init__(self, limit):
        self.limit = limit
        self.active = 0
        self.peak = 0

    async def __aenter__(self):
        while self.active >= self.limit:
            await asyncio.sleep(0.001)
        self.active += 1
        self.peak = max(self.peak, self.active)
        return self

    async def __aexit__(self, *exc):
        self.active -= 1
        return False


async def price_stream(sku, n):
    for tick in range(n):
        await asyncio.sleep(0.001 * (ord(sku) % 3))
        price = BASE[sku] + tick
        yield {"sku": sku, "tick": tick, "price": price}


async def fetch_quote(sku, limiter, flaky=False):
    attempt = 0
    quotes = []
    last_error = None
    while True:
        attempt += 1
        try:
            async with limiter:
                await asyncio.sleep(0.002 * (ord(sku) % 4))
                if flaky and attempt == 1:
                    raise ConnectionError(f"{sku} flaky")
                async for q in price_stream(sku, 3):
                    quotes.append(q["price"])
            break
        except ConnectionError as err:
            last_error = str(err)
            if attempt >= 3:
                raise
    best = min(quotes)
    return sku, best, attempt


async def slow_quote(sku):
    ticks = 0
    while True:
        ticks += 1
        await asyncio.sleep(0.01)


def reconcile(quotes):
    matched = {}
    for sku, best, attempt in quotes:
        matched[sku] = best - BASE[sku]
    drift = sum(matched.values())
    return drift


@perf.trace(
    OUTPUT_FILE="d_async",
    TRACE_LOCALS=["__main__.fetch_quote", "__main__.price_stream", "__main__.slow_quote",
                  "__main__.reconcile", "RateLimiter.*"],
)
async def scenario_async():
    limiter = RateLimiter(limit=2)
    quotes = await asyncio.gather(*(fetch_quote(s, limiter, flaky=(s == "C")) for s in BASE))
    try:
        await asyncio.wait_for(slow_quote("Z"), timeout=0.055)
    except TimeoutError:
        pass
    drift = await asyncio.to_thread(reconcile, quotes)
    return quotes, drift, limiter.peak


# ═════════════════════════════════════════════════════════════════════════════
# E. Mixed: async → to_thread → thread pool + process pool (spawn)
# ═════════════════════════════════════════════════════════════════════════════

def heavy_calc(n):
    acc = 0
    for k in range(n):
        acc += k * k
    return acc


def fan_out(sizes):
    partial = []
    with ThreadPoolExecutor(max_workers=2) as threads:
        partial.extend(threads.map(heavy_calc, sizes))
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as procs:
        remote = list(procs.map(heavy_calc, sizes))
    combined = sum(partial) + sum(remote)
    return combined


@perf.trace(OUTPUT_FILE="e_mixed", TRACE_LOCALS=["__main__.fan_out", "__main__.heavy_calc"])
async def scenario_mixed():
    return await asyncio.to_thread(fan_out, [3, 4])


# ═════════════════════════════════════════════════════════════════════════════
# F. Exceptions through several tracked frames; the entry cap in a big loop
# ═════════════════════════════════════════════════════════════════════════════

def level3(x):
    y = x * 2
    z = y - 10
    return 100 // z


def level2(x):
    state = "start"
    try:
        r = level3(x)
        state = "done"
    finally:
        state = "cleanup"
        cleaned = True
    return r


def level1(x):
    tries = 0
    for v in (4, x):
        tries += 1
        try:
            level2(v)
        except ZeroDivisionError as e:
            msg = str(e)
            raise RuntimeError(f"failed after {tries}") from e


def crunch(n):
    total = 0
    for i in range(n):
        total += i
    return total


@perf.trace(OUTPUT_FILE="f_exceptions_cap", TRACE_LOCALS=["__main__.level*", "__main__.crunch"],
            MAX_LOCALS_EVENTS=50)
def scenario_exceptions_cap():
    try:
        level1(5)
    except RuntimeError:
        pass
    return crunch(2000)


# ═════════════════════════════════════════════════════════════════════════════
# Reading traces back and checking them
# ═════════════════════════════════════════════════════════════════════════════

RESULTS: list = []  # (scenario, status, description, detail)
SCENARIO = ""


def check(desc, ok, detail=""):
    RESULTS.append((SCENARIO, "PASS" if ok else "FAIL", desc, "" if ok else detail))


def observe(desc, detail):
    RESULTS.append((SCENARIO, "OBSERVE", desc, detail))


def _resolve(value, variables, depth=0):
    if depth > 50:
        return value
    if isinstance(value, dict):
        if "__ref__" in value:
            target = _resolve(variables.get(value["__ref__"], value), variables, depth + 1)
            rest = {k: _resolve(v, variables, depth + 1) for k, v in value.items() if k != "__ref__"}
            return {**rest, **target} if rest and isinstance(target, dict) else target
        return {k: _resolve(v, variables, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, variables, depth + 1) for v in value]
    return value


def _walk(node):
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


class Trace:
    def __init__(self, name):
        data = perf.expand_locals(json.loads((OUT / f"{name}.json").read_text()))
        self.variables = data.get("variables", {})
        self.sources = data.get("sources", {})
        self.nodes = list(_walk(data["trace"]))
        for n in self.nodes:
            for key in ("args", "kwargs", "result", "yielded"):
                if key in n:
                    n[key] = _resolve(n[key], self.variables)
            lines = self.sources.get(n.get("source"), {}).get("lines", {})
            n["_entries"] = [
                {
                    "text": lines.get(str(e["line"]), "?").split("#")[0].strip(),
                    "set": _resolve(e.get("set", {}), self.variables),
                    "deleted": e.get("deleted", []),
                    "globals": _resolve(e.get("globals", {}), self.variables),
                }
                for e in n.get("locals", [])
            ]

    def named(self, pattern):
        return [n for n in self.nodes if fnmatch(n["name"], pattern)]


def values(node, var):
    """Every value `var` took in this call, in order."""
    return [e["set"][var] for e in node["_entries"] if var in e["set"]]


def at(node, text_prefix):
    """The `set` dicts of the entries recorded at lines starting with text_prefix."""
    return [e["set"] for e in node["_entries"] if e["text"].startswith(text_prefix)]


def verify_business():
    t = Trace("a_business")
    (po,) = t.named("__main__.process_order")
    check("process_order has locals", po["_entries"], "no entries")
    check("subtotal goes 0 → 20 → 29", values(po, "subtotal") == [0, 20, 29], values(po, "subtotal"))
    check("match/case binds sku per line item (B is skipped by the guard but still bound)",
          values(po, "sku") == ["A", "B", "C", "D"], values(po, "sku"))
    check("qty: bound by match, then rebound after OutOfStock (C: 5→3, D: 1→0)",
          values(po, "qty") == [2, 0, 5, 3, 1, 0], values(po, "qty"))
    check("finally runs on every path, including `continue` inside except (attempts 1 → 3 → 4)",
          values(po, "attempts")[-3:] == [1, 3, 4], values(po, "attempts"))
    check("warnings list grows by in-place append",
          values(po, "warnings")[-1] == ["skip B", "C: wanted 5, have 3", "D: wanted 1, have 0"],
          values(po, "warnings")[-1:])
    check("reserved holds (sku, qty) pairs (tuples captured as lists)",
          values(po, "reserved")[-1] == [["A", 2], ["C", 3]], values(po, "reserved")[-1:])
    check("`with ... as position` is recorded", values(po, "position") == [1], values(po, "position"))
    check("tuple-unpacking a call result sets both names at one line",
          at(po, "discount, applied =") == [{"discount": 7, "applied": ["loyalty", "coupon", "bulk"]}],
          at(po, "discount, applied ="))
    check("global ORDERS_SEEN recorded under globals",
          any(e["globals"].get("ORDERS_SEEN") == 1 for e in po["_entries"]),
          [e["globals"] for e in po["_entries"] if e["globals"]])
    inv_changes = at(po, "inventory.reserve(sku, qty)")
    check("an argument object mutated by a called method (inventory.reserve) is recorded at the call line",
          any("inventory" in s for s in inv_changes), inv_changes)
    check("argument `order` mutated in the else branch (order.tags.append) is recorded",
          any("order" in s for s in at(po, "order.tags.append")), at(po, "order.tags.append"))
    check("exception variable `exc` recorded, then deleted at the end of the except block",
          values(po, "exc") and any("exc" in e["deleted"] for e in po["_entries"]),
          [(e["text"], e["deleted"]) for e in po["_entries"] if e["deleted"]])
    check("final status/total", values(po, "status") == ["partial"] and values(po, "total") == [22],
          (values(po, "status"), values(po, "total")))

    (ar,) = t.named("__main__.apply_rules")
    check("apply_rules: walrus amt takes 2, 5, 3", values(ar, "amt") == [2, 5, 3], values(ar, "amt"))
    check("apply_rules: while counter i goes 0..3", values(ar, "i") == [0, 1, 2, 3], values(ar, "i"))
    check("apply_rules: while/else sets cap", values(ar, "cap") == [7], values(ar, "cap"))
    check("apply_rules: nonlocal discount changed by bump() is seen at the bump line (0, 2, 7, 10, then capped 7)",
          values(ar, "discount") == [0, 2, 7, 10, 7], values(ar, "discount"))
    check("apply_rules: applied mutated by the closure", values(ar, "applied")[-1] == ["loyalty", "coupon", "bulk"],
          values(ar, "applied"))
    bumps = t.named("__main__.apply_rules.<locals>.bump")
    check("closure bump() is traced 3 times (matched by the apply_rules* pattern)", len(bumps) == 3, len(bumps))
    observe("what bump() records for its own nonlocal write", [b["_entries"] for b in bumps[:1]])

    reserves = t.named("Inventory.reserve")
    check("Inventory.reserve: 4 calls (A, C fail, C retry, D fail)", len(reserves) == 4, len(reserves))
    failing = [r for r in reserves if r.get("exception")]
    check("failing reserve calls record `have` and the exception",
          len(failing) == 2 and [values(r, "have") for r in failing] == [[3], [0]],
          [(values(r, "have"), r.get("exception")) for r in failing])
    ok = [r for r in reserves if not r.get("exception")]
    check("successful reserve records self mutated (stock then reserved)",
          all(any("self" in s for s in at(r, "self.stock[sku]")) and any("self" in s for s in at(r, "self.reserved[sku]"))
              for r in ok), [r["_entries"] for r in ok])


def verify_recursion_generators():
    t = Trace("b_recursion_generators")
    frames = t.named("__main__.rollup")
    check("rollup: one node per catalog node (5)", len(frames) == 5, len(frames))
    by_name = {f["args"][0]["name"]: f for f in frames}
    check("each recursive frame has its own totals (root: 1 → 33 → 38)",
          values(by_name["root"], "total") == [1, 33, 38], values(by_name["root"], "total"))
    check("electronics frame: 2 → 12 → 32", values(by_name["electronics"], "total") == [2, 12, 32],
          values(by_name["electronics"], "total"))
    check("leaf frames never see a parent's total",
          all(values(by_name[n], "total") == [s] for n, s in (("phones", 10), ("laptops", 20), ("books", 5))),
          {n: values(by_name[n], "total") for n in ("phones", "laptops", "books")})
    check("level is leaf/branch per frame",
          [values(by_name[n], "level") for n in ("root", "phones")] == [["branch"], ["leaf"]],
          [values(by_name[n], "level") for n in ("root", "phones")])
    check("tuple-unpacked recursive results land in the parent frame",
          values(by_name["root"], "sub_total") == [32, 5], values(by_name["root"], "sub_total"))

    (bt,) = t.named("__main__.batched")
    check("batched: batch grows in place, then is rebound to a fresh []",
          values(bt, "batch") == [[], [0], [0, 1], [0, 1, 2], [], [3], [3, 4], [3, 4, 5], [], [6]],
          values(bt, "batch"))
    check("batched: yielded values recorded", bt.get("yielded") == [[0, 1, 2], [3, 4, 5], [6]], bt.get("yielded"))

    (rm,) = t.named("__main__.running_mean")
    check("running_mean: values received by send() recorded at the yield line",
          values(rm, "value") == [10, 20, 30], values(rm, "value"))
    check("running_mean: count/total across sends", values(rm, "count") == [0, 1, 2, 3]
          and values(rm, "total") == [0, 10, 30, 60], (values(rm, "count"), values(rm, "total")))


def verify_threads():
    t = Trace("c_threads")
    workers = t.named("__main__.worker")
    check("4 worker nodes", len(workers) == 4, len(workers))
    ids_per_worker = [{j["id"] for j in values(w, "job")} for w in workers]
    all_ids = [i for ids in ids_per_worker for i in ids]
    check("every job id seen by exactly one worker (no cross-thread leakage of `job`)",
          sorted(all_ids) == list(range(1, N_JOBS + 1)), sorted(all_ids))
    processed = [(values(w, "processed") or [0])[-1] for w in workers]
    errors = [len((values(w, "errors") or [[]])[-1]) for w in workers]
    check("sum of per-worker `processed` + errors == all jobs",
          sum(processed) + sum(errors) == N_JOBS, (processed, errors))
    check("each worker's processed counts up 0,1,2,... with no gaps",
          all(values(w, "processed") == list(range(len(values(w, "processed")))) for w in workers),
          [values(w, "processed") for w in workers])
    check("score_job errors (ids 7, 14) recorded as caught exc in the right worker",
          sorted(int(e.split(":")[0]) for w in workers for e in (values(w, "errors") or [[]])[-1]) == [7, 14],
          [values(w, "errors") for w in workers])
    foreign = []
    for w in workers:
        for e in w["_entries"]:
            if "results" in e["set"] and not e["text"].startswith("results["):
                foreign.append(e["text"])
    observe("shared `results` dict changed by OTHER threads shows up at unrelated lines of this worker",
            f"{len(foreign)} such entries, e.g. {sorted(set(foreign))[:4]}")
    observe("how the queue.Queue argument `jobs` is captured when get_nowait() mutates it",
            str(at(workers[0], "job = jobs.get_nowait()")[:1])[:200])

    scores = t.named("__main__.score_job")
    check("score_job: 20 from workers + 4 from the pool", len(scores) == N_JOBS + 4, len(scores))
    by_id = {}
    for s in scores:
        by_id.setdefault(s["args"][0]["id"], []).append(s)
    j3 = [s for s in by_id[3] if s["args"][0]["base"] == 30][0]
    check("score_job(id=3): score 0 → 30 → 90 → 180 → 300 (weight 4)",
          values(j3, "score") == [0, 30, 90, 180, 300], values(j3, "score"))
    check("raising score_job (id 7) records weight and the exception, no score",
          all(values(s, "weight") == [3] and s.get("exception") and not values(s, "score") for s in by_id[7]),
          [(values(s, "weight"), s.get("exception")) for s in by_id[7]])


def verify_async():
    t = Trace("d_async")
    quotes = t.named("__main__.fetch_quote")
    check("5 concurrent fetch_quote calls", len(quotes) == 5, len(quotes))
    for q in quotes:
        sku = q["args"][0]
        prices = (values(q, "quotes") or [[]])[-1]
        check(f"fetch_quote({sku}): quotes are only its own prices (no mix-up across awaits)",
              prices == [BASE[sku], BASE[sku] + 1, BASE[sku] + 2], prices)
    flaky = [q for q in quotes if q["args"][0] == "C"][0]
    check("flaky fetch_quote(C): attempt 0 → 1 → 2, last_error None → 'C flaky'",
          values(flaky, "attempt") == [0, 1, 2] and values(flaky, "last_error") == [None, "C flaky"],
          (values(flaky, "attempt"), values(flaky, "last_error")))
    check("`async with ... ` and `async for q` lines appear in the entries",
          any(e["text"].startswith("async for q") for e in flaky["_entries"]),
          sorted({e["text"] for e in flaky["_entries"]}))

    streams = t.named("__main__.price_stream")
    check("price_stream: 5 async generator nodes, each tick 0,1,2 with its own price",
          len(streams) == 5 and all(values(s, "tick") == [0, 1, 2]
                                    and values(s, "price") == [BASE[s["args"][0]] + i for i in range(3)]
                                    for s in streams),
          [(s["args"], values(s, "tick"), values(s, "price")) for s in streams])

    (slow,) = t.named("__main__.slow_quote") or [None]
    check("cancelled slow_quote keeps its locals (ticks counted up before the timeout)",
          slow and len(values(slow, "ticks")) >= 3 and values(slow, "ticks") == list(range(len(values(slow, "ticks")))),
          slow and values(slow, "ticks"))
    check("cancelled slow_quote records the cancellation", slow and slow.get("exception"), slow and slow.get("exception"))

    (rec,) = t.named("__main__.reconcile")
    check("reconcile (run via to_thread): matched dict and drift recorded",
          values(rec, "matched")[-1] == {s: 0 for s in BASE} and values(rec, "drift") == [0],
          (values(rec, "matched")[-1:], values(rec, "drift")))

    limiters = t.named("RateLimiter.__aenter__")
    check("RateLimiter.__aenter__ traced for every acquisition (5 + 1 retry)", len(limiters) == 6, len(limiters))
    observe("self.active as seen inside __aenter__ calls",
            [values(n, "self")[-1].get("active") if values(n, "self") else None for n in limiters])


def verify_mixed():
    t = Trace("e_mixed")
    (fo,) = t.named("__main__.fan_out")
    check("fan_out (on a to_thread worker): partial extended by the thread pool",
          values(fo, "partial")[-1:] == [[5, 14]], values(fo, "partial"))
    check("fan_out: remote results from the process pool", values(fo, "remote") == [[5, 14]], values(fo, "remote"))
    check("fan_out: combined", values(fo, "combined") == [38], values(fo, "combined"))
    calcs = t.named("__main__.heavy_calc")
    check("heavy_calc: 2 thread + 2 process nodes", len(calcs) == 4, [c["name"] for c in calcs])
    with_locals = [c for c in calcs if c["_entries"]]
    check("heavy_calc: every node (thread AND process worker) has locals",
          len(with_locals) == len(calcs), f"{len(with_locals)}/{len(calcs)} have locals")
    check("heavy_calc(4): acc 0 → 0 → 1 → 5 → 14 (0+0 is no change)",
          all(values(c, "acc") == [0, 1, 5, 14] for c in with_locals if c["args"] == [4]),
          [values(c, "acc") for c in with_locals if c["args"] == [4]])


def verify_exceptions_cap():
    t = Trace("f_exceptions_cap")
    l3 = t.named("__main__.level3")
    check("level3 called twice (x=4, x=5)", len(l3) == 2, len(l3))
    bad = [n for n in l3 if n["args"] == [5]][0]
    check("level3(5): y=10, z=0 recorded before the ZeroDivisionError",
          values(bad, "y") == [10] and values(bad, "z") == [0] and bad.get("exception"),
          (values(bad, "y"), values(bad, "z"), bad.get("exception")))
    l2 = t.named("__main__.level2")
    bad2 = [n for n in l2 if n["args"] == [5]][0]
    check("level2(5): finally runs after the exception: state start → cleanup, cleaned, no r",
          values(bad2, "state") == ["start", "cleanup"] and values(bad2, "cleaned") == [True]
          and not values(bad2, "r"), (values(bad2, "state"), values(bad2, "cleaned"), values(bad2, "r")))
    good2 = [n for n in l2 if n["args"] == [4]][0]
    check("level2(4): state start → done → cleanup, r = -50",
          values(good2, "state") == ["start", "done", "cleanup"] and values(good2, "r") == [-50],
          (values(good2, "state"), values(good2, "r")))
    (l1,) = t.named("__main__.level1")
    check("level1: tries 0 → 1 → 2, msg recorded, RuntimeError recorded",
          values(l1, "tries") == [0, 1, 2] and values(l1, "msg") == ["integer division or modulo by zero"]
          and "RuntimeError" in str(l1.get("exception")),
          (values(l1, "tries"), values(l1, "msg"), l1.get("exception")))
    (cr,) = t.named("__main__.crunch")
    check("crunch(2000) with MAX_LOCALS_EVENTS=50: exactly 50 entries kept",
          len(cr["_entries"]) == 50, len(cr["_entries"]))
    check("...the rest counted in locals_dropped", cr.get("locals_dropped", 0) > 3000, cr.get("locals_dropped"))
    check("...the call still returns the right result", cr.get("result") == sum(range(2000)), cr.get("result"))


# ═════════════════════════════════════════════════════════════════════════════

def main():
    global SCENARIO
    if sys.version_info < (3, 12):
        sys.exit("TRACE_LOCALS needs Python 3.12+")
    OUT.mkdir(parents=True, exist_ok=True)
    runs = [
        ("A business logic", scenario_business, verify_business),
        ("B recursion/generators", scenario_recursion_generators, verify_recursion_generators),
        ("C threads", scenario_threads, verify_threads),
        ("D async", lambda: asyncio.run(scenario_async()), verify_async),
        ("E mixed async/thread/process", lambda: asyncio.run(scenario_mixed()), verify_mixed),
        ("F exceptions + cap", scenario_exceptions_cap, verify_exceptions_cap),
    ]
    timings = []
    for name, run, verify in runs:
        SCENARIO = name
        start = time.perf_counter()
        try:
            run()
        except Exception as exc:
            check("scenario runs without crashing", False, repr(exc))
        timings.append((name, time.perf_counter() - start))
        try:
            verify()
        except Exception as exc:
            check("verification could read the trace", False, f"{type(exc).__name__}: {exc}")

    print("\n" + "═" * 100)
    current = None
    for scenario, status, desc, detail in RESULTS:
        if scenario != current:
            current = scenario
            print(f"\n■ {scenario}")
        mark = {"PASS": "✅", "FAIL": "❌", "OBSERVE": "👀"}[status]
        print(f"  {mark} {desc}")
        if detail:
            print(f"       → {detail}")
    counts = {s: sum(1 for r in RESULTS if r[1] == s) for s in ("PASS", "FAIL", "OBSERVE")}
    print("\n" + "═" * 100)
    print("timings: " + ", ".join(f"{n} {s:.2f}s" for n, s in timings))
    print(f"PASS {counts['PASS']}  FAIL {counts['FAIL']}  OBSERVE {counts['OBSERVE']}  — traces in {OUT}")
    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
