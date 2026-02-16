"""
SCENARIO 4: Sliding Window Precision
- Tests precise sliding window behavior at window boundaries
- Verifies that the weighted formula prevents burst-at-boundary exploits
- A naive counter that resets at T=1 would incorrectly allow requests
  that a sliding window correctly rejects
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_sliding_window_precision():
    """Test precise sliding window behavior with timed requests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure tenant with 2 req/sec limit and 1 second window
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "precision_tenant",
                "rate_limit": 2,
                "burst_capacity": 10,
                "refill_rate": 2
            }
        )

        # Two requests immediately at T=0 (same window, same client)
        r1 = await client.post(
            "/v1/rate-limit/check",
            json={
                "client_id": "precision_client",
                "tenant_id": "precision_tenant",
                "priority": 2
            }
        )
        assert r1.json()["allowed"] is True, "First request should be allowed"

        r2 = await client.post(
            "/v1/rate-limit/check",
            json={
                "client_id": "precision_client",
                "tenant_id": "precision_tenant",
                "priority": 2
            }
        )
        assert r2.json()["allowed"] is True, "Second request should be allowed"

        # Third request at T=0.5 (still within first window, sliding window has 2 requests)
        await asyncio.sleep(0.5)
        r3 = await client.post(
            "/v1/rate-limit/check",
            json={
                "client_id": "precision_client",
                "tenant_id": "precision_tenant",
                "priority": 2
            }
        )
        result3 = r3.json()
        # Should be rejected or queued because window contains 2 requests already
        assert result3["allowed"] is False or result3.get("queued") is True, \
            "Third request should be rejected (sliding window has 2 requests)"

        # Request after 2+ seconds (previous requests have expired from sliding window)
        await asyncio.sleep(2.0)
        r4 = await client.post(
            "/v1/rate-limit/check",
            json={
                "client_id": "precision_client",
                "tenant_id": "precision_tenant",
                "priority": 2
            }
        )
        assert r4.json()["allowed"] is True, "Fourth request should be allowed after window slides"


@pytest.mark.asyncio
async def test_naive_counter_vs_sliding_window():
    """Test that sliding window correctly limits across window boundaries.

    Sends 3 requests at T~0.9 using the same client_id, then 3 more at T~1.1.
    A naive counter that resets at T=1 would allow all 3 in the second batch.
    The sliding window should reject some because the window [0.1, 1.1] still
    contains the first batch.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "counter_test",
                "rate_limit": 3,
                "burst_capacity": 5,
                "refill_rate": 3
            }
        )

        # Make 3 requests at T=0.9 — same client so sliding window counts them together
        await asyncio.sleep(0.9)
        results_first_batch = []
        for i in range(3):
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "same_client",
                    "tenant_id": "counter_test",
                    "priority": 2
                }
            )
            results_first_batch.append(response.json()["allowed"])

        # Make 3 more requests at T=1.1 (just after 1 second boundary)
        await asyncio.sleep(0.2)
        results_second_batch = []
        for i in range(3):
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "same_client",
                    "tenant_id": "counter_test",
                    "priority": 2
                }
            )
            results_second_batch.append(response.json()["allowed"])

        first_allowed = sum(results_first_batch)
        second_allowed = sum(results_second_batch)

        print(f"First batch (T=0.9): {first_allowed}/3 allowed")
        print(f"Second batch (T=1.1): {second_allowed}/3 allowed")

        # With sliding window, second batch should be limited
        # because window [0.1, 1.1] contains requests from first batch
        assert second_allowed < 3, "Sliding window should limit second batch"


@pytest.mark.asyncio
async def test_window_boundary_edge_cases():
    """Test edge cases at window boundaries."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "boundary_tenant",
                "rate_limit": 2,
                "burst_capacity": 5,
                "refill_rate": 2
            }
        )

        # Two requests immediately — same client
        r1 = await client.post(
            "/v1/rate-limit/check",
            json={"client_id": "edge_client", "tenant_id": "boundary_tenant", "priority": 2}
        )
        r2 = await client.post(
            "/v1/rate-limit/check",
            json={"client_id": "edge_client", "tenant_id": "boundary_tenant", "priority": 2}
        )

        assert r1.json()["allowed"] is True
        assert r2.json()["allowed"] is True

        # Third should be rejected
        r3 = await client.post(
            "/v1/rate-limit/check",
            json={"client_id": "edge_client", "tenant_id": "boundary_tenant", "priority": 2}
        )
        assert r3.json()["allowed"] is False

        # Wait 2.5 seconds for sliding window to move past the first requests
        await asyncio.sleep(2.5)
        r4 = await client.post(
            "/v1/rate-limit/check",
            json={"client_id": "edge_client", "tenant_id": "boundary_tenant", "priority": 2}
        )
        assert r4.json()["allowed"] is True
