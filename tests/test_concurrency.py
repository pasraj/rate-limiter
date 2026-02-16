"""
Concurrency and Race Condition Tests
- Verifies that concurrent requests to the same client don't exceed the rate limit
- Tests atomic operations under high concurrency
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_concurrent_same_client_no_over_count():
    """Concurrent requests from the same client_id should not exceed the rate limit.

    This tests for race conditions in the sliding window increment/check logic.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "race_tenant",
                "rate_limit": 50,
                "burst_capacity": 50,
                "refill_rate": 50
            }
        )

        # Fire 200 concurrent requests from the SAME client
        tasks = []
        for i in range(200):
            tasks.append(client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "same_client",
                    "tenant_id": "race_tenant",
                    "priority": 2
                }
            ))

        responses = await asyncio.gather(*tasks)
        allowed = sum(1 for r in responses if r.json()["allowed"])

        print(f"Concurrent race test: {allowed}/200 allowed (limit: 50)")

        # Should not exceed the rate limit even under concurrent access
        assert allowed <= 50, (
            f"Race condition: {allowed} requests allowed (limit: 50)"
        )
        assert allowed >= 40, (
            f"Too few requests allowed: {allowed} (expected ~50)"
        )


@pytest.mark.asyncio
async def test_concurrent_different_tenants_independent():
    """Concurrent requests to different tenants should be completely independent."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure 5 tenants with limit of 20 each
        for t in range(5):
            await client.put(
                "/v1/tenant/config",
                json={
                    "tenant_id": f"concurrent_t{t}",
                    "rate_limit": 20,
                    "burst_capacity": 20,
                    "refill_rate": 20
                }
            )

        # Send 15 requests to each tenant concurrently (under each tenant's limit)
        all_tasks = []
        for t in range(5):
            for i in range(15):
                all_tasks.append((f"concurrent_t{t}", client.post(
                    "/v1/rate-limit/check",
                    json={
                        "client_id": f"client_t{t}",
                        "tenant_id": f"concurrent_t{t}",
                        "priority": 2
                    }
                )))

        tenant_ids = [t[0] for t in all_tasks]
        responses = await asyncio.gather(*[t[1] for t in all_tasks])

        # Count per-tenant
        per_tenant = {}
        for tid, resp in zip(tenant_ids, responses):
            per_tenant.setdefault(tid, {"allowed": 0, "total": 0})
            per_tenant[tid]["total"] += 1
            if resp.json()["allowed"]:
                per_tenant[tid]["allowed"] += 1

        for tid, counts in per_tenant.items():
            print(f"{tid}: {counts['allowed']}/{counts['total']} allowed")
            # Each tenant should allow all 15 (well under 20 limit)
            assert counts["allowed"] == 15, (
                f"{tid}: expected 15 allowed, got {counts['allowed']}"
            )
