"""Bounded async cache with single-flight loading and explicit invalidation."""

import asyncio
from collections import OrderedDict
from time import monotonic


class SourceCache:
    def __init__(self, capacity=128):
        self.capacity = capacity
        self.entries = OrderedDict()
        self.locks = {}

    async def get(self, key, ttl, loader, *, refresh=False):
        state = self.locks.setdefault(key, [asyncio.Lock(), 0])
        lock = state[0]
        state[1] += 1
        try:
            async with lock:
                entry = self.entries.get(key)
                if entry and not refresh and monotonic() - entry[0] < ttl:
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
