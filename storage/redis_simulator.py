import asyncio
import time
from typing import Dict

class RedisSimulator:
    def __init__(self, latency_ms: int = 50):
        self.latency_sec = latency_ms / 1000.0
        self._data: Dict[str, str] = {}
        self._ttls: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def _simulate_latency(self):
        await asyncio.sleep(self.latency_sec)

    async def get(self, key: str):
        async with self._lock:
            await self._simulate_latency()
            if key in self._ttls and time.time() > self._ttls[key]:
                del self._data[key]
                del self._ttls[key]
                return None
            return self._data.get(key)

    async def set(self, key: str, value: str, ex=None):
        async with self._lock:
            await self._simulate_latency()
            self._data[key] = value
            if ex:
                self._ttls[key] = time.time() + ex

    async def incr(self, key: str, amount: int = 1):
        async with self._lock:
            await self._simulate_latency()
            val = int(self._data.get(key, 0))
            val += amount
            self._data[key] = str(val)
            return val

    async def decr(self, key: str, amount: int = 1):
        async with self._lock:
            await self._simulate_latency()
            val = int(self._data.get(key, 0))
            val -= amount
            self._data[key] = str(val)
            return val

    async def expire(self, key: str, seconds: int):
        async with self._lock:
            await self._simulate_latency()
            if key in self._data:
                self._ttls[key] = time.time() + seconds

    async def delete(self, key: str):
        async with self._lock:
            await self._simulate_latency()
            self._data.pop(key, None)
            self._ttls.pop(key, None)

    async def flushall(self):
        async with self._lock:
            await self._simulate_latency()
            self._data.clear()
            self._ttls.clear()

    async def pipeline(self):
        return RedisPipeline(self)

class RedisPipeline:
    def __init__(self, simulator: RedisSimulator):
        self.simulator = simulator
        self.commands = []

    def get(self, key: str):
        self.commands.append(('get', (key,)))
        return self

    def set(self, key: str, value: str):
        self.commands.append(('set', (key, value)))
        return self

    def incr(self, key: str):
        self.commands.append(('incr', (key,)))
        return self

    def expire(self, key: str, seconds: int):
        self.commands.append(('expire', (key, seconds)))
        return self

    async def execute(self):
        # Execute all commands atomically under a single lock
        async with self.simulator._lock:
            # Single latency for the entire pipeline to avoid compounding delays
            await self.simulator._simulate_latency()

            results = []
            for cmd, args in self.commands:
                # Execute without additional latency (already simulated once for pipeline)
                if cmd == 'get':
                    key = args[0]
                    if key in self.simulator._ttls and time.time() > self.simulator._ttls[key]:
                        del self.simulator._data[key]
                        del self.simulator._ttls[key]
                        results.append(None)
                    else:
                        results.append(self.simulator._data.get(key))
                elif cmd == 'set':
                    key, value = args
                    self.simulator._data[key] = value
                    results.append(None)
                elif cmd == 'incr':
                    key = args[0]
                    val = int(self.simulator._data.get(key, 0))
                    val += 1
                    self.simulator._data[key] = str(val)
                    results.append(val)
                elif cmd == 'expire':
                    key, seconds = args
                    if key in self.simulator._data:
                        self.simulator._ttls[key] = time.time() + seconds
                    results.append(None)
            return results
