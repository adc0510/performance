"""Workers that die abruptly (c15_worker_failure)."""

import os


def die(x):
    os._exit(1)


def fine(x):
    return x + 1
