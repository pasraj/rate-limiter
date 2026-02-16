import time
import asyncio


# Simple lock manager for atomicity
_locks = {}
_lock_creation_lock = None
_lock_loop_id = None


def _get_lock_creation_lock():
    """Get or create the lock creation lock for the current event loop."""
    global _lock_creation_lock, _lock_loop_id
    try:
        loop = asyncio.get_running_loop()
        current_loop_id = id(loop)
    except RuntimeError:
        current_loop_id = None

    if _lock_creation_lock is None or _lock_loop_id != current_loop_id:
        _lock_creation_lock = asyncio.Lock()
        _lock_loop_id = current_loop_id
        _locks.clear()  # Clear locks from previous event loop
    return _lock_creation_lock


async def _get_lock(key: str):
    """Get or create a lock for the given key."""
    lock_creation_lock = _get_lock_creation_lock()
    async with lock_creation_lock:
        if key not in _locks:
            _locks[key] = asyncio.Lock()
        return _locks[key]


class SlidingWindowCounter:
    def __init__(self, redis_client, rate_limit: int, window_size: int = 1):
        self.redis = redis_client
        self.rate_limit = rate_limit
        self.window_size = window_size

    async def is_allowed(self, client_id: str, tenant_id: str = None):
        """Check if a request is allowed under the sliding window rate limit.

        Args:
            client_id: The client identifier.
            tenant_id: Optional tenant identifier. When provided, rate limiting
                       is enforced per-tenant (all clients under a tenant share
                       the same counter). When None, falls back to per-client.
        """
        # Use tenant_id for rate limiting key if provided (per-tenant enforcement)
        rate_key = f"{tenant_id}:{client_id}" if tenant_id else client_id
        lock = await _get_lock(rate_key)
        async with lock:
            current_time = time.time()
            current_window = int(current_time / self.window_size)
            previous_window = current_window - 1

            current_key = f"rate:{rate_key}:{current_window}"
            previous_key = f"rate:{rate_key}:{previous_window}"

            # Get previous window count
            previous_val = await self.redis.get(previous_key)
            previous_count = int(previous_val) if previous_val else 0

            elapsed_time_in_window = current_time - (current_window * self.window_size)
            weight = elapsed_time_in_window / self.window_size

            # Atomically increment current window counter (optimistic approach)
            current_count = await self.redis.incr(current_key)
            await self.redis.expire(current_key, self.window_size * 2)

            # Calculate estimated count including this request
            estimated_count = (previous_count * (1 - weight)) + current_count

            if estimated_count > self.rate_limit:
                # We went over the limit, decrement and reject
                await self.redis.decr(current_key)
                retry_after = self.window_size - elapsed_time_in_window
                return False, retry_after

            return True, None

    async def rollback(self, client_id: str, tenant_id: str = None):
        """Rollback a previously allowed request (decrement counter)"""
        rate_key = f"{tenant_id}:{client_id}" if tenant_id else client_id
        lock = await _get_lock(rate_key)
        async with lock:
            current_time = time.time()
            current_window = int(current_time / self.window_size)
            current_key = f"rate:{rate_key}:{current_window}"
            await self.redis.decr(current_key)
