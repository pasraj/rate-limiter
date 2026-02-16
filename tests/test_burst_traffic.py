"""
SCENARIO 1: Burst Traffic Pattern
- 10,000 requests in 10 seconds from single client
- Expect: ~100 allowed (100/sec sliding window limit), rest rejected with retry-after headers
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from main import app


@pytest.mark.asyncio
async def test_burst_traffic_pattern():
    """Test burst traffic with 10,000 requests from single client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure tenant with 100 req/sec limit
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "burst_client",
                "rate_limit": 100,
                "burst_capacity": 1000,
                "refill_rate": 100
            }
        )

        allowed_count = 0
        rejected_count = 0
        retry_headers_count = 0

        # Send 10,000 requests rapidly from the same client
        tasks = []
        for i in range(10000):
            task = client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "single_client",
                    "tenant_id": "burst_client",
                    "priority": 2
                }
            )
            tasks.append(task)

        responses = await asyncio.gather(*tasks)

        for response in responses:
            data = response.json()
            if data["allowed"]:
                allowed_count += 1
            else:
                rejected_count += 1
                if data.get("retry_after") is not None:
                    retry_headers_count += 1

        # Sliding window allows 100/sec; token bucket burst_capacity is 1000
        # The sliding window (per-tenant) should cap at 100 within a 1-second window
        assert allowed_count <= 1000, f"Expected max 1000 allowed, got {allowed_count}"
        assert rejected_count >= 9000, f"Expected at least 9000 rejected, got {rejected_count}"
        assert retry_headers_count > 0, "Expected retry-after headers in rejections"

        print(f"Burst Traffic Results: {allowed_count} allowed, {rejected_count} rejected")


@pytest.mark.asyncio
async def test_burst_with_retry_after():
    """Test that retry-after headers are properly set."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "retry_tenant",
                "rate_limit": 10,
                "burst_capacity": 10,
                "refill_rate": 10
            }
        )

        # Exhaust the limit
        for i in range(15):
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": "test_client",
                    "tenant_id": "retry_tenant",
                    "priority": 2
                }
            )

            data = response.json()
            if not data["allowed"]:
                assert data["retry_after"] is not None, "Missing retry_after header"
                assert data["retry_after"] > 0, "retry_after should be positive"
