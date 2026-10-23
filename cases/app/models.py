from abc import ABC, abstractmethod


class Repo(ABC):
    def __init__(self, rows):
        self.rows = rows

    def get(self, key):
        return self._load(key)

    def _load(self, key):
        return self.rows.get(key)

    @abstractmethod
    def save(self, key, value): ...

    def describe(self):
        return f"{type(self).__name__}({len(self.rows)})"
