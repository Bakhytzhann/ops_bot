import asyncio
from collections import OrderedDict
from time import monotonic


class SourceCache:
    def __init__(self, capacity=128):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.locks = {}

    def peek(self, key):
        return self.entries.get(key)

    async def get(self, key, ttl, loader, *, refresh=False, refresh_interval=0):
        previous = self.entries.get(key)

        def usable(entry):
            if entry is None:
                return False
            if entry is not previous:
                return True
            age = monotonic() - entry[0]
            if refresh:
                return age < refresh_interval
            return age < ttl

        state = self.locks.get(key)
        entry = previous
        if state is None and usable(entry):
            self.entries.move_to_end(key)
            return entry[1]
        if state is None:
            state = self.locks[key] = [asyncio.Lock(), 0]
        lock = state[0]
        state[1] += 1
        try:
            async with lock:
                entry = self.entries.get(key)
                if usable(entry):
                    self.entries.move_to_end(key)
                    return entry[1]
                value = await loader()
                self.entries[key] = (monotonic(), value)
                self.entries.move_to_end(key)
                while len(self.entries) > self.capacity:
                    self.entries.popitem(last=False)
                return value
        finally:
            state[1] -= 1
            if state[1] == 0:
                self.locks.pop(key, None)
