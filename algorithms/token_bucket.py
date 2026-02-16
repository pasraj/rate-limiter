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


class TokenBucket:
    def __init__(self, redis_client, capacity: int, refill_rate: int):
        self.redis = redis_client
        self.capacity = capacity
        self.refill_rate = refill_rate

    async def consume(self, tenant_id: str, tokens: int = 1):
        # Use application-level lock for complete atomicity
        lock_key = f"bucket_lock:{tenant_id}"
        lock = await _get_lock(lock_key)
        async with lock:
            current_time = time.time()
            key_consumed = f"bucket:{tenant_id}:consumed"
            key_timestamp = f"bucket:{tenant_id}:timestamp"

            # Get current state
            consumed_val = await self.redis.get(key_consumed)
            timestamp_val = await self.redis.get(key_timestamp)

            consumed_tokens = int(consumed_val) if consumed_val is not None else 0
            last_refill = float(timestamp_val) if timestamp_val is not None else current_time

            # Calculate refill based on time passed
            time_passed = current_time - last_refill

            if timestamp_val is None:
                # First request, initialize timestamp
                await self.redis.set(key_timestamp, str(current_time))
                consumed_tokens = 0
            elif time_passed > 0 and self.refill_rate > 0:
                # Refill tokens based on time passed
                refill_amount = time_passed * self.refill_rate
                consumed_tokens = max(0, consumed_tokens - int(refill_amount))
                # Only update timestamp if we actually refilled something
                if refill_amount >= 1:
                    await self.redis.set(key_consumed, str(consumed_tokens))
                    await self.redis.set(key_timestamp, str(current_time))

            # Check if we can consume
            if consumed_tokens + tokens <= self.capacity:
                # Consume tokens
                new_consumed = consumed_tokens + tokens
                await self.redis.set(key_consumed, str(new_consumed))
                return True, None
            else:
                tokens_needed = (consumed_tokens + tokens) - self.capacity
                retry_after = tokens_needed / self.refill_rate if self.refill_rate > 0 else 1.0
                return False, retry_after

    async def get_status(self, tenant_id: str):
        """Get the current status of the token bucket for a tenant."""
        lock_key = f"bucket_lock:{tenant_id}"
        lock = await _get_lock(lock_key)
        async with lock:
            current_time = time.time()
            key_consumed = f"bucket:{tenant_id}:consumed"
            key_timestamp = f"bucket:{tenant_id}:timestamp"

            consumed_val = await self.redis.get(key_consumed)
            timestamp_val = await self.redis.get(key_timestamp)

            consumed_tokens = int(consumed_val) if consumed_val is not None else 0
            last_refill = float(timestamp_val) if timestamp_val is not None else current_time

            # Apply refill based on elapsed time
            time_passed = current_time - last_refill
            if time_passed > 0 and self.refill_rate > 0:
                refill_amount = time_passed * self.refill_rate
                consumed_tokens = max(0, consumed_tokens - int(refill_amount))

            available_tokens = self.capacity - consumed_tokens

            return {
                "available_tokens": available_tokens,
                "capacity": self.capacity,
                "refill_rate": self.refill_rate
            }
