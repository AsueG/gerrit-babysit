"""A memo that only keeps what the latest poll asked for: keyed by branch tip or patch set, it would otherwise grow with
every push."""
import contextlib


class PollMemo:
    def __init__(self):
        self.kept = {}
        self._asked = {}

    def __contains__(self, key):
        return key in self._asked or key in self.kept

    @contextlib.contextmanager
    def poll(self):
        try:
            yield self
        except BaseException:
            # A poll cut short (REST down) must not throw away what the next one would ask again.
            self.kept, self._asked = {**self.kept, **self._asked}, {}
            raise
        self.kept, self._asked = self._asked, {}

    def get(self, key, compute, *args):
        """`compute` returning None means unknown: nothing is kept, so the next poll asks again."""
        if key in self._asked:
            return self._asked[key]
        value = self.kept[key] if key in self.kept else compute(*args)
        if value is not None:
            self._asked[key] = value
        return value
