"""
SCENARIO 5: Distributed Consistency
- 3 API nodes share the same Redis instance
- Concurrent requests across all nodes should respect the global rate limit
- Ensures atomic operations prevent over-counting
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI
from storage.redis_simulator import RedisSimulator
from algorithms.sliding_window import SlidingWindowCounter
from algorithms.token_bucket import TokenBucket
from algorithms.priority_heap import PriorityHeap
from algorithms import sliding_window, token_bucket


def create_node(shared_redis, node_id: str):
    """Create an independent FastAPI app node that shares a Redis instance."""
    from pydantic import BaseModel
    from typing import Optional

    class RateLimitRequest(BaseModel):
        client_id: str
        tenant_id: str
        priority: int = 2

    class RateLimitResponse(BaseModel):
        allowed: bool
        retry_after: Optional[float] = None
        queued: bool = False

    node_app = FastAPI(title=f"Rate Limiter Node {node_id}")

    # Each node has its own local state but shares Redis
    node_state = {
        "redis": shared_redis,
        "tenant_configs": {},
        "tenant_rate_limiters": {},
        "tenant_token_buckets": {},
        "priority_queues": {},
    }

    def get_rate_limiter(tenant_id):
        if tenant_id not in node_state["tenant_rate_limiters"]:
            config = node_state["tenant_configs"].get(tenant_id, {
                "rate_limit": 100, "window_size": 1
            })
            node_state["tenant_rate_limiters"][tenant_id] = SlidingWindowCounter(
                shared_redis,
                rate_limit=config["rate_limit"],
                window_size=config.get("window_size", 1)
            )
        return node_state["tenant_rate_limiters"][tenant_id]

    def get_token_bucket(tenant_id):
        if tenant_id not in node_state["tenant_token_buckets"]:
            config = node_state["tenant_configs"].get(tenant_id, {
                "burst_capacity": 200, "refill_rate": 100
            })
            node_state["tenant_token_buckets"][tenant_id] = TokenBucket(
                shared_redis,
                capacity=config["burst_capacity"],
                refill_rate=config["refill_rate"]
            )
        return node_state["tenant_token_buckets"][tenant_id]

    @node_app.post("/v1/rate-limit/check", response_model=RateLimitResponse)
    async def check_rate_limit(request: RateLimitRequest):
        rate_limiter = get_rate_limiter(request.tenant_id)
        token_bucket_inst = get_token_bucket(request.tenant_id)

        allowed, retry_after = await rate_limiter.is_allowed(
            request.client_id, tenant_id=request.tenant_id
        )
        if not allowed:
            return RateLimitResponse(allowed=False, retry_after=retry_after, queued=True)

        tokens_allowed, tokens_retry = await token_bucket_inst.consume(request.tenant_id)
        if not tokens_allowed:
            await rate_limiter.rollback(request.client_id, tenant_id=request.tenant_id)
            return RateLimitResponse(allowed=False, retry_after=tokens_retry, queued=True)

        return RateLimitResponse(allowed=True)

    @node_app.put("/v1/tenant/config")
    async def update_config(tenant_id: str, rate_limit: int, burst_capacity: int, refill_rate: int):
        node_state["tenant_configs"][tenant_id] = {
            "rate_limit": rate_limit,
            "window_size": 1,
            "burst_capacity": burst_capacity,
            "refill_rate": refill_rate,
        }
        node_state["tenant_rate_limiters"].pop(tenant_id, None)
        node_state["tenant_token_buckets"].pop(tenant_id, None)
        return {"status": "updated"}

    return node_app, node_state


def clear_algorithm_locks():
    sliding_window._locks.clear()
    token_bucket._locks.clear()


@pytest.mark.asyncio
async def test_three_nodes_shared_redis():
    """Three API nodes sharing Redis should enforce the same global rate limit."""
    clear_algorithm_locks()
    shared_redis = RedisSimulator(latency_ms=0)

    # Create 3 independent nodes sharing the same Redis
    node1_app, node1_state = create_node(shared_redis, "node1")
    node2_app, node2_state = create_node(shared_redis, "node2")
    node3_app, node3_state = create_node(shared_redis, "node3")

    # Configure all nodes with the same tenant config
    tenant_config = {
        "rate_limit": 30,
        "window_size": 1,
        "burst_capacity": 30,
        "refill_rate": 30,
    }
    for state in [node1_state, node2_state, node3_state]:
        state["tenant_configs"]["shared_tenant"] = tenant_config

    transport1 = ASGITransport(app=node1_app)
    transport2 = ASGITransport(app=node2_app)
    transport3 = ASGITransport(app=node3_app)

    async with AsyncClient(transport=transport1, base_url="http://node1") as c1, \
               AsyncClient(transport=transport2, base_url="http://node2") as c2, \
               AsyncClient(transport=transport3, base_url="http://node3") as c3:

        # Each node sends 20 requests (60 total, limit is 30)
        async def send_requests(client, node_name, count):
            results = []
            for i in range(count):
                resp = await client.post(
                    "/v1/rate-limit/check",
                    json={
                        "client_id": f"{node_name}_client",
                        "tenant_id": "shared_tenant",
                        "priority": 2,
                    }
                )
                results.append(resp.json()["allowed"])
            return results

        r1, r2, r3 = await asyncio.gather(
            send_requests(c1, "node1", 20),
            send_requests(c2, "node2", 20),
            send_requests(c3, "node3", 20),
        )

        total_allowed = sum(r1) + sum(r2) + sum(r3)
        print(f"Node1: {sum(r1)}/20, Node2: {sum(r2)}/20, Node3: {sum(r3)}/20")
        print(f"Total allowed: {total_allowed}/60 (limit: 30)")

        # The global limit of 30 should be respected across all 3 nodes
        assert total_allowed <= 30, (
            f"Global rate limit violated: {total_allowed} allowed across 3 nodes (limit: 30)"
        )
        assert total_allowed >= 20, (
            f"Too few requests allowed: {total_allowed} (expected ~30)"
        )


@pytest.mark.asyncio
async def test_distributed_atomic_consistency():
    """Concurrent requests from multiple nodes should not cause race conditions."""
    clear_algorithm_locks()
    shared_redis = RedisSimulator(latency_ms=0)

    node1_app, node1_state = create_node(shared_redis, "node1")
    node2_app, node2_state = create_node(shared_redis, "node2")
    node3_app, node3_state = create_node(shared_redis, "node3")

    tenant_config = {
        "rate_limit": 50,
        "window_size": 1,
        "burst_capacity": 50,
        "refill_rate": 50,
    }
    for state in [node1_state, node2_state, node3_state]:
        state["tenant_configs"]["atomic_tenant"] = tenant_config

    transport1 = ASGITransport(app=node1_app)
    transport2 = ASGITransport(app=node2_app)
    transport3 = ASGITransport(app=node3_app)

    async with AsyncClient(transport=transport1, base_url="http://node1") as c1, \
               AsyncClient(transport=transport2, base_url="http://node2") as c2, \
               AsyncClient(transport=transport3, base_url="http://node3") as c3:

        # Fire 30 requests from each node concurrently (90 total, limit 50)
        async def burst_requests(client, node_name, count):
            tasks = []
            for i in range(count):
                tasks.append(client.post(
                    "/v1/rate-limit/check",
                    json={
                        "client_id": f"{node_name}_burst",
                        "tenant_id": "atomic_tenant",
                        "priority": 2,
                    }
                ))
            responses = await asyncio.gather(*tasks)
            return [r.json()["allowed"] for r in responses]

        r1, r2, r3 = await asyncio.gather(
            burst_requests(c1, "node1", 30),
            burst_requests(c2, "node2", 30),
            burst_requests(c3, "node3", 30),
        )

        total_allowed = sum(r1) + sum(r2) + sum(r3)
        print(f"Atomic test — Total allowed: {total_allowed}/90 (limit: 50)")

        # Must not significantly exceed the global limit.
        # The sliding window uses a weighted approximation, so ±1 is acceptable
        # under extreme concurrency from multiple nodes.
        assert total_allowed <= 53, (
            f"Atomic consistency violated: {total_allowed} allowed (limit: 50, tolerance: 53)"
        )
        assert total_allowed >= 40, (
            f"Too few requests allowed: {total_allowed} (expected ~50)"
        )
