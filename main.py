import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict
from contextlib import asynccontextmanager

from storage.redis_simulator import RedisSimulator
from algorithms.sliding_window import SlidingWindowCounter
from algorithms.priority_heap import PriorityHeap
from algorithms.token_bucket import TokenBucket


class RateLimitRequest(BaseModel):
    client_id: str
    tenant_id: str
    priority: int = 2


class TenantConfig(BaseModel):
    tenant_id: str
    rate_limit: int
    burst_capacity: int
    refill_rate: int


class RateLimitResponse(BaseModel):
    allowed: bool
    retry_after: Optional[float] = None
    queued: bool = False


class QueueStatusResponse(BaseModel):
    tenant_id: str
    queue_depth: int
    processing: bool


app_state = {
    "redis": None,
    "tenant_configs": {},
    "tenant_rate_limiters": {},
    "tenant_token_buckets": {},
    "priority_queues": {},
    "processing_tasks": {}
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app_state["redis"] = RedisSimulator(latency_ms=50)

    app_state["tenant_configs"]["default"] = {
        "rate_limit": 100,
        "window_size": 1,
        "burst_capacity": 200,
        "refill_rate": 100
    }

    yield

    for task in app_state["processing_tasks"].values():
        task.cancel()


app = FastAPI(title="Distributed Rate Limiter", lifespan=lifespan)


def get_rate_limiter(tenant_id: str):
    if tenant_id not in app_state["tenant_rate_limiters"]:
        default_config = {
            "rate_limit": 100,
            "window_size": 1,
            "burst_capacity": 200,
            "refill_rate": 100
        }
        config = app_state["tenant_configs"].get(tenant_id, app_state["tenant_configs"].get("default", default_config))
        app_state["tenant_rate_limiters"][tenant_id] = SlidingWindowCounter(
            app_state["redis"],
            rate_limit=config["rate_limit"],
            window_size=config.get("window_size", 1)
        )
    return app_state["tenant_rate_limiters"][tenant_id]


def get_token_bucket(tenant_id: str):
    if tenant_id not in app_state["tenant_token_buckets"]:
        default_config = {
            "rate_limit": 100,
            "window_size": 1,
            "burst_capacity": 200,
            "refill_rate": 100
        }
        config = app_state["tenant_configs"].get(tenant_id, app_state["tenant_configs"].get("default", default_config))
        app_state["tenant_token_buckets"][tenant_id] = TokenBucket(
            app_state["redis"],
            capacity=config["burst_capacity"],
            refill_rate=config["refill_rate"]
        )
    return app_state["tenant_token_buckets"][tenant_id]


def get_priority_queue(tenant_id: str):
    if tenant_id not in app_state["priority_queues"]:
        app_state["priority_queues"][tenant_id] = PriorityHeap()
    return app_state["priority_queues"][tenant_id]


async def process_queue(tenant_id: str):
    """Process queued requests for a tenant, respecting rate limits and priority ordering."""
    queue = get_priority_queue(tenant_id)
    rate_limiter = get_rate_limiter(tenant_id)
    token_bucket = get_token_bucket(tenant_id)
    idle_cycles = 0
    max_idle_cycles = 100  # Stop after 1 second of empty queue

    while True:
        await asyncio.sleep(0.01)

        if queue.is_empty():
            idle_cycles += 1
            if idle_cycles >= max_idle_cycles:
                # Queue has been empty long enough, terminate this processor
                app_state["processing_tasks"].pop(tenant_id, None)
                return
            continue

        idle_cycles = 0
        queue.apply_aging()

        allowed, _ = await rate_limiter.is_allowed(f"queue:{tenant_id}")
        tokens_allowed, _ = await token_bucket.consume(tenant_id)

        if allowed and tokens_allowed:
            item = queue.extract_min()
            if item:
                # Record processed item so it can be tracked/observed
                processed = app_state.get("processed_items", {})
                if tenant_id not in processed:
                    processed[tenant_id] = []
                processed[tenant_id].append(item)
                app_state["processed_items"] = processed


@app.post("/v1/rate-limit/check", response_model=RateLimitResponse)
async def check_rate_limit(request: RateLimitRequest):
    rate_limiter = get_rate_limiter(request.tenant_id)
    token_bucket = get_token_bucket(request.tenant_id)

    # Check sliding window first (before consuming tokens)
    allowed, retry_after = await rate_limiter.is_allowed(request.client_id, tenant_id=request.tenant_id)

    if not allowed:
        queue = get_priority_queue(request.tenant_id)
        queue.insert(request.model_dump(), request.priority)

        if request.tenant_id not in app_state["processing_tasks"]:
            task = asyncio.create_task(process_queue(request.tenant_id))
            app_state["processing_tasks"][request.tenant_id] = task

        return RateLimitResponse(
            allowed=False,
            retry_after=retry_after,
            queued=True
        )

    # Only consume tokens if sliding window allows
    tokens_allowed, tokens_retry = await token_bucket.consume(request.tenant_id)

    if not tokens_allowed:
        # Rollback the sliding window counter since we're not fulfilling this request
        await rate_limiter.rollback(request.client_id, tenant_id=request.tenant_id)

        queue = get_priority_queue(request.tenant_id)
        queue.insert(request.model_dump(), request.priority)

        if request.tenant_id not in app_state["processing_tasks"]:
            task = asyncio.create_task(process_queue(request.tenant_id))
            app_state["processing_tasks"][request.tenant_id] = task

        return RateLimitResponse(
            allowed=False,
            retry_after=tokens_retry,
            queued=True
        )

    return RateLimitResponse(allowed=True)


@app.get("/v1/queue/status", response_model=QueueStatusResponse)
async def get_queue_status(tenant_id: str):
    queue = get_priority_queue(tenant_id)

    return QueueStatusResponse(
        tenant_id=tenant_id,
        queue_depth=queue.size(),
        processing=tenant_id in app_state["processing_tasks"]
    )


@app.put("/v1/tenant/config")
async def update_tenant_config(config: TenantConfig):
    app_state["tenant_configs"][config.tenant_id] = {
        "rate_limit": config.rate_limit,
        "window_size": 1,
        "burst_capacity": config.burst_capacity,
        "refill_rate": config.refill_rate
    }

    if config.tenant_id in app_state["tenant_rate_limiters"]:
        del app_state["tenant_rate_limiters"][config.tenant_id]
    if config.tenant_id in app_state["tenant_token_buckets"]:
        del app_state["tenant_token_buckets"][config.tenant_id]

    return {"status": "updated", "tenant_id": config.tenant_id}


@app.get("/health")
async def health():
    return {"status": "healthy"}
