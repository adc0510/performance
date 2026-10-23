"""Top-level (picklable) callables for the process-pool cases."""


def square(x):
    return helper(x) * x


def helper(x):
    return x


def explode(x):
    raise ValueError(f"bad input {x}")


class Worker:
    def __init__(self, factor):
        self.factor = factor

    def scale(self, x):
        return helper(x) * self.factor
