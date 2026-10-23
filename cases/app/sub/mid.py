from . import leaf
from .leaf import leaf_value
from ..util import normalize


def combine(text, x):
    return f"{normalize(text)}:{leaf_value(x)}:{leaf.leaf_value(x + 1)}"
