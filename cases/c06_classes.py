"""Methods of every kind on a project class."""

import perf
from _common import setup

EXPECT = {
    "summary": "Instance/class/static methods, private and name-mangled methods, properties, dunders",
    "checks": [
        {"status": "works", "desc": "methods are named `Class.method` (class name only, no module)",
         "path": ["main", "Cart.checkout", "Cart.total", "Cart._discount"]},
        {"status": "works", "desc": "`self` is args[0], captured as {__type__, ...instance __dict__} as it was when the call STARTED (first add() sees an empty cart)",
         "node": "Cart.add", "args": [{"__type__": "Cart", "owner": "ann", "items": []}, "apple", 2.5]},
        {"status": "works", "desc": "every call gets a fresh snapshot of `self`: checkout() sees both items and the renamed owner (see c09)",
         "node": "Cart.checkout", "args": [{"__type__": "Cart", "owner": "zoe", "items": [["apple", 2.5], ["pear", 7.5]]}]},
        {"status": "works", "fixes": "was a limitation: a class passed as a value (cls, exc_type) was dumped as its whole __dict__",
         "desc": "@classmethod: args[0] (cls) is captured compactly as {__class__: module.QualName}",
         "node": "Cart.empty", "args": [{"__class__": "__main__.Cart"}, "bob"],
         "result": {"__type__": "Cart", "owner": "bob", "items": []}},
        {"status": "works", "desc": "@staticmethod: no self/cls in args",
         "node": "Cart.tax", "args": [10.0], "result": 1.0},
        {"status": "works", "desc": "single-underscore methods ARE traced (unlike `_` module functions)",
         "node": "Cart._discount"},
        {"status": "works", "desc": "name-mangled `__x` methods are traced under the mangled name",
         "node": "Cart._Cart__audit"},
        {"status": "works", "fixes": "was a limitation: @property getters/setters were invisible",
         "desc": "@property getter: traced as `Class.prop`, its own calls nest under it", "path": ["main", "Cart.count", "Cart._discount"]},
        {"status": "works", "new": True, "desc": "@prop.setter: traced as `Class.prop.setter` with the assigned value",
         "node": "Cart.owner_name.setter", "args": [{"__type__": "Cart", "...": True}, "zoe"]},
        {"status": "works", "new": True, "desc": "a property read by perf ITSELF while capturing a value is not recorded (only the program's own reads are)",
         "count": "Cart.count", "n": 1},
        {"status": "works", "fixes": "was a limitation: __init__ (and __call__/__enter__/__exit__) were invisible",
         "desc": "__init__ is traced: args are the constructor arguments (self is captured before __init__ ran, so it is still empty)",
         "node": "Cart.__init__", "args": ["<Cart>", "ann"], "result": None},
        {"status": "works", "new": True, "desc": "__enter__ / __exit__ are traced; __exit__ receives the exception raised in the `with` body",
         "node": "Cart.__exit__", "select": {"result": False}, "args": [{"__type__": "Cart", "...": True}, {"__class__": "builtins.ValueError"}, "ValueError('in with')", "<traceback>"]},
        {"status": "limitation", "desc": "other dunders (__len__, __eq__, __repr__, __hash__, …) stay untraced: perf calls them itself while capturing values",
         "absent": "Cart.__len__"},
        {"status": "works", "desc": "project calls made inside an untraced dunder attach to the caller of that dunder",
         "path": ["main", "Cart.checkout", "Cart._Cart__audit"]},
    ],
}


class Cart:
    def __init__(self, owner):
        self.owner = owner
        self.items = []

    @property
    def owner_name(self):
        return self.owner

    @owner_name.setter
    def owner_name(self, value):
        self.owner = value

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __len__(self):
        return len(self.items)

    @property
    def count(self):
        self._discount(0)
        return len(self.items)

    @classmethod
    def empty(cls, owner):
        return cls(owner)

    @staticmethod
    def tax(amount):
        return amount * 0.1

    def add(self, name, price):
        self.items.append((name, price))
        return self

    def _discount(self, subtotal):
        return subtotal * 0.05

    def __audit(self):
        return len(self)

    def total(self):
        subtotal = sum(price for _, price in self.items)
        return subtotal - self._discount(subtotal) + self.tax(subtotal)

    def checkout(self):
        self.__audit()
        return round(self.total(), 2)


@perf.trace
def main():
    cart = Cart("ann").add("apple", 2.5).add("pear", 7.5)
    Cart.empty("bob")
    _ = cart.count
    cart.owner_name = "zoe"
    try:
        with cart:
            raise ValueError("in with")
    except ValueError:
        pass
    return cart.checkout()


if __name__ == "__main__":
    setup(__file__)
    main()
