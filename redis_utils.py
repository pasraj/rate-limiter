import asyncio
from storage.redis_simulator import RedisSimulator
from main import app_state


async def show_redis_status():
    if app_state["redis"] is None:
        return

    redis = app_state["redis"]


async def clear_all_redis():
    if app_state["redis"] is None:
        return

    await app_state["redis"].flushall()


async def clear_all_state():
    if app_state["redis"]:
        await app_state["redis"].flushall()

    app_state["tenant_rate_limiters"].clear()
    app_state["tenant_token_buckets"].clear()
    app_state["priority_queues"].clear()

    for task in app_state["processing_tasks"].values():
        task.cancel()
    app_state["processing_tasks"].clear()


async def main():
    app_state["redis"] = RedisSimulator(latency_ms=1)

    await show_redis_status()
    await clear_all_redis()
    await show_redis_status()
    await clear_all_state()
    await show_redis_status()


if __name__ == "__main__":
    asyncio.run(main())
