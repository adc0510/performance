"""Only imported from inside a traced call (see c11_imports)."""


def late_function(x):
    return x + 1


class LateClass:
    def method(self, x):
        return x * 10
