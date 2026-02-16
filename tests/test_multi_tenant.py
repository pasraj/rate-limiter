"""
SCENARIO 3: Multi-tenant Fairness
- Tenant A: 1000 req/sec limit, submits 2000/sec
- Tenant B: 100 req/sec limit, submits 50/sec
- Tenant C: Burst to 500 req/sec limit
- Expect: Tenant B never starved, Tenant A throttled correctly
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_multi_tenant_fairness():
    """Test that multiple tenants with different limits are handled fairly."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure Tenant A: 1000 req/sec limit
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "tenant_a",
                "rate_limit": 1000,
                "burst_capacity": 1000,
                "refill_rate": 1000
            }
        )

        # Configure Tenant B: 100 req/sec limit
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "tenant_b",
                "rate_limit": 100,
                "burst_capacity": 100,
                "refill_rate": 100
            }
        )

        # Configure Tenant C: 500 req/sec limit
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "tenant_c",
                "rate_limit": 500,
                "burst_capacity": 500,
                "refill_rate": 500
            }
        )

        # Tenant A submits 2000 requests (2x limit) — use shared client_id
        # so the sliding window per-tenant counter is properly tested
        tenant_a_tasks = []
        for i in range(2000):
            task = client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "client_a",
                    "tenant_id": "tenant_a",
                    "priority": 2
                }
            )
            tenant_a_tasks.append(task)

        # Tenant B submits 50 requests (under limit)
        tenant_b_tasks = []
        for i in range(50):
            task = client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "client_b",
                    "tenant_id": "tenant_b",
                    "priority": 2
                }
            )
            tenant_b_tasks.append(task)

        # Tenant C bursts to 500 requests
        tenant_c_tasks = []
        for i in range(500):
            task = client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "client_c",
                    "tenant_id": "tenant_c",
                    "priority": 2
                }
            )
            tenant_c_tasks.append(task)

        # Execute all requests concurrently
        results_a = await asyncio.gather(*tenant_a_tasks)
        results_b = await asyncio.gather(*tenant_b_tasks)
        results_c = await asyncio.gather(*tenant_c_tasks)

        # Count allowed requests for each tenant
        tenant_a_allowed = sum(1 for r in results_a if r.json()["allowed"])
        tenant_b_allowed = sum(1 for r in results_b if r.json()["allowed"])
        tenant_c_allowed = sum(1 for r in results_c if r.json()["allowed"])

        print(f"Tenant A: {tenant_a_allowed}/2000 allowed (limit: 1000)")
        print(f"Tenant B: {tenant_b_allowed}/50 allowed (limit: 100)")
        print(f"Tenant C: {tenant_c_allowed}/500 allowed (limit: 500)")

        # Tenant B should never be starved (all 50 should be allowed, well under 100 limit)
        assert tenant_b_allowed == 50, f"Tenant B starved: only {tenant_b_allowed}/50 allowed"

        # Tenant A should be throttled (not all 2000 allowed)
        # With refill_rate=1000/sec and test execution taking ~0.3s, some token
        # refills occur during the test, allowing slightly more than the base limit.
        assert tenant_a_allowed <= 1500, f"Tenant A not throttled: {tenant_a_allowed} allowed (limit: 1000 + refill tolerance)"
        assert tenant_a_allowed < 2000, "Tenant A should be throttled, not get all requests"

        # Tenant C should get most of its burst (limit is 500)
        assert tenant_c_allowed >= 400, f"Tenant C under-served: {tenant_c_allowed}/500 allowed"

        # Each tenant should have independent limits
        assert tenant_a_allowed != tenant_b_allowed, "Tenants should have different allowances"


@pytest.mark.asyncio
async def test_tenant_isolation():
    """Test that one tenant cannot exhaust another tenant's quota."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure two tenants with same limits
        for tenant in ["tenant_x", "tenant_y"]:
            await client.put(
                "/v1/tenant/config",
                json={
                    "tenant_id": tenant,
                    "rate_limit": 100,
                    "burst_capacity": 100,
                    "refill_rate": 100
                }
            )

        # Tenant X exhausts its quota using a single client
        for i in range(150):
            await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "client_x",
                    "tenant_id": "tenant_x",
                    "priority": 2
                }
            )

        # Tenant Y should still have full quota
        tenant_y_allowed = 0
        for i in range(100):
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "client_y",
                    "tenant_id": "tenant_y",
                    "priority": 2
                }
            )
            if response.json()["allowed"]:
                tenant_y_allowed += 1

        assert tenant_y_allowed >= 90, f"Tenant Y affected by Tenant X: {tenant_y_allowed}/100"
