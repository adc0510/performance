"""Functions stored in containers at import time, before perf patches anything."""


def handle_upper(x):
    return x.upper()


def handle_lower(x):
    return x.lower()


HANDLERS = {"upper": handle_upper, "lower": handle_lower}
_DECORATED = {}


def register(name):
    def deco(fn):
        _DECORATED[name] = fn
        return fn

    return deco


@register("reverse")
def handle_reverse(x):
    return x[::-1]


def dispatch(name, x):
    return HANDLERS[name](x)


def dispatch_decorated(name, x):
    return _DECORATED[name](x)


def dispatch_by_lookup(name, x):
    return globals()[f"handle_{name}"](x)
