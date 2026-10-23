"""Processes: ProcessPoolExecutor is traced (subtree shipped back); multiprocessing.Pool is not."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import perf
from _common import setup
from app import workers

EXPECT = {
    "summary": "ProcessPoolExecutor submit/map/exceptions/bound methods (spawn), multiprocessing.Pool",
    "checks": [
        {"status": "works", "desc": "ProcessPoolExecutor.submit: the worker-side subtree is grafted under the submitting call",
         "path": ["main", "__main__.submit_jobs", "app.workers.square", "app.workers.helper"]},
        {"status": "works", "desc": "worker-side args/result are captured",
         "node": "app.workers.square", "args": [3], "result": 9},
        {"status": "works", "desc": "an exception raised in the worker is recorded on the worker node",
         "node": "app.workers.explode", "exception": "ValueError('bad input 1')"},
        {"status": "works", "desc": "a submitted bound method gets exactly one node",
         "path": ["main", "__main__.submit_method", "Worker.scale", "app.workers.helper"]},
        {"status": "works", "desc": "Executor.map under spawn/forkserver: every item gets a node for the mapped function, including the first chunk on a fresh worker",
         "count": "app.workers.square", "file": "mapped", "n": 4},
        {"status": "works", "desc": "...and functions called inside the mapped function are traced for every item",
         "count": "app.workers.helper", "file": "mapped", "n": 4},
        {"status": "works", "fixes": "was a limitation: multiprocessing.Pool workers were not traced at all",
         "desc": "multiprocessing.Pool.map: each item's worker-side node is grafted under the caller (full coverage in c15_mp_pool)",
         "file": "mp_pool", "count": "app.workers.square", "n": 2},
    ],
}

CTX = multiprocessing.get_context("spawn")


def submit_jobs(pool):
    return pool.submit(workers.square, 3).result()


def submit_failing(pool):
    try:
        return pool.submit(workers.explode, 1).result()
    except ValueError:
        return None


def submit_method(pool):
    return pool.submit(workers.Worker(2).scale, 5).result()


@perf.trace
def main():
    with ProcessPoolExecutor(1, mp_context=CTX) as pool:
        submit_jobs(pool)
        submit_failing(pool)
        submit_method(pool)


@perf.trace(OUTPUT_FILE="mapped")
def mapped():
    with ProcessPoolExecutor(1, mp_context=CTX) as pool:
        return list(pool.map(workers.square, range(4), chunksize=2))


@perf.trace(OUTPUT_FILE="mp_pool")
def mp_pool():
    with CTX.Pool(1) as pool:
        return pool.map(workers.square, range(2))


if __name__ == "__main__":
    setup(__file__)
    main()
    mapped()
    mp_pool()
