from .models import Repo


class SqlRepo(Repo):
    def save(self, key, value):
        self.rows[key] = value
        return True

    def get(self, key):  # override that delegates to the base via super()
        return super().get(key) or "<default>"
