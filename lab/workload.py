"""Synthetic workload that triggers both RAM problems discussed:

1. A class whose `self` carries a sizeable dict of state, with many methods
   called repeatedly on the same instance (the "duplicate self capture"
   problem).
2. One call that produces a single genuinely large value (the "single
   oversized value" problem).

Both the baseline (`perf.trace`) and the new pipeline are pointed at the same
`run()` so the RAM numbers are comparable.
"""

import random
import string


def _rand_str(n: int) -> str:
    return "".join(random.choices(string.ascii_letters, k=n))


class Service:
    """Stand-in for a typical long-lived service object with real state."""

    def __init__(self) -> None:
        # ~200 KB of baseline state, present on every call's `self` snapshot.
        self.config = {f"key_{i}": _rand_str(100) for i in range(2000)}
        self.cache: dict[str, str] = {}
        self.counter = 0

    def handle(self, item_id: int) -> dict:
        """Typical small unit of work: reads/mutates a sliver of self."""
        self.counter += 1
        self.cache[f"item_{item_id % 20}"] = _rand_str(20)
        return {"item_id": item_id, "counter": self.counter}

    def summarize(self) -> dict:
        """Another frequently-called method touching the same self."""
        return {"cached": len(self.cache), "counter": self.counter}

    def bulk_export(self) -> str:
        """Occasionally returns one genuinely large single value."""
        return _rand_str(5_000_000)  # ~5 MB single return value


def run(calls: int = 3000) -> int:
    svc = Service()
    return run_request(svc, calls)


def run_request(svc: Service, calls: int = 300) -> int:
    """Like `run`, but against an existing (possibly shared, possibly
    concurrently-used) Service instance -- models one request among many
    hitting the same long-lived service object, for testing cross-trace
    dedup correctness.
    """
    total = 0
    for i in range(calls):
        r = svc.handle(i)
        total += r["counter"]
        if i % 50 == 0:
            svc.summarize()
        if i % 400 == 0:
            svc.bulk_export()
    return total
