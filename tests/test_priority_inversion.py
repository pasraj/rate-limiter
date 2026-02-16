"""
SCENARIO 2: Priority Inversion Test
- 100 low-priority background jobs submitted first
- 10 high-priority user requests submitted after
- Expect: High priority jobs queued at front of priority queue
- Expect: Priority ordering is maintained in the queue
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from main import app, app_state
from algorithms.priority_heap import PriorityHeap


@pytest.mark.asyncio
async def test_priority_inversion_prevention():
    """Test that high priority requests are queued ahead of low priority ones."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Configure tenant with limited capacity to force queuing
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "priority_tenant",
                "rate_limit": 10,
                "burst_capacity": 20,
                "refill_rate": 10
            }
        )

        # Submit 100 low-priority background jobs (most will be queued)
        for i in range(100):
            await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": f"bg_job_{i}",
                    "tenant_id": "priority_tenant",
                    "priority": 2  # NORMAL priority
                }
            )

        # Submit 10 high-priority user requests
        high_priority_results = []
        for i in range(10):
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": f"user_req_{i}",
                    "tenant_id": "priority_tenant",
                    "priority": 0  # CRITICAL priority
                }
            )
            high_priority_results.append(response.json())

        # Check queue status
        queue_status = await client.get("/v1/queue/status?tenant_id=priority_tenant")
        assert queue_status.status_code == 200

        queue_data = queue_status.json()
        assert queue_data["queue_depth"] > 0, "Queue should have pending items"

        # Verify the priority queue has CRITICAL items at the front
        queue = app_state["priority_queues"].get("priority_tenant")
        assert queue is not None, "Priority queue should exist"
        assert queue.size() > 0, "Queue should not be empty"

        # Peek at the front of the queue — it should be a CRITICAL item
        front_item = queue.peek()
        assert front_item is not None
        assert front_item["priority"] == 0, (
            f"Front of queue should be CRITICAL (0), got priority {front_item['priority']}"
        )


@pytest.mark.asyncio
async def test_priority_ordering():
    """Test that priority ordering is maintained in queue extraction."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.put(
            "/v1/tenant/config",
            json={
                "tenant_id": "order_tenant",
                "rate_limit": 2,
                "burst_capacity": 2,
                "refill_rate": 2
            }
        )

        # Send a mix of priorities — first 2 will be allowed, rest queued
        priorities_to_send = [
            (2, "normal1"),   # allowed
            (2, "normal2"),   # allowed
            (2, "normal3"),   # queued — NORMAL
            (1, "high1"),     # queued — HIGH
            (2, "normal4"),   # queued — NORMAL
            (0, "critical1"), # queued — CRITICAL
            (1, "high2"),     # queued — HIGH
        ]

        for priority, client_id in priorities_to_send:
            await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": client_id,
                    "tenant_id": "order_tenant",
                    "priority": priority
                }
            )

        # Extract items from the queue and verify ordering
        queue = app_state["priority_queues"].get("order_tenant")
        assert queue is not None
        assert queue.size() >= 3, f"Expected at least 3 queued items, got {queue.size()}"

        # Extract all items and verify priority ordering
        extracted = []
        while not queue.is_empty():
            item = queue.extract_min()
            extracted.append(item)

        # Items should come out in priority order: CRITICAL(0) < HIGH(1) < NORMAL(2)
        priorities = [item["priority"] for item in extracted]
        assert priorities == sorted(priorities), (
            f"Queue extraction not in priority order: {priorities}"
        )

        # CRITICAL items should come first
        assert extracted[0]["priority"] == 0, "First extracted item should be CRITICAL"

        print(f"Extraction order: {[(e['client_id'], e['priority']) for e in extracted]}")


@pytest.mark.asyncio
async def test_priority_aging():
    """Test that items waiting >5s get their priority boosted."""
    import time
    heap = PriorityHeap()

    # Insert items with NORMAL priority but fake old timestamps
    heap.insert({"id": "old_normal"}, priority=2)
    heap.insert({"id": "old_high"}, priority=1)
    heap.insert({"id": "new_normal"}, priority=2)

    # Artificially age the first two items beyond the threshold
    heap.heap[0].timestamp = time.time() - 6.0  # >5s old
    heap.heap[1].timestamp = time.time() - 6.0  # >5s old
    # Rebuild heap after modifying timestamps (heap invariant preserved by sequence)

    # Apply aging
    heap.apply_aging()

    # The aged NORMAL(2) should now be HIGH(1), aged HIGH(1) should now be CRITICAL(0)
    aged_items = {item.data["id"]: item.priority for item in heap.heap}
    assert aged_items["old_normal"] == 1, f"old_normal should be boosted to 1, got {aged_items['old_normal']}"
    assert aged_items["old_high"] == 0, f"old_high should be boosted to 0, got {aged_items['old_high']}"
    assert aged_items["new_normal"] == 2, f"new_normal should stay at 2, got {aged_items['new_normal']}"

    # The CRITICAL item should be extracted first
    first = heap.extract_min()
    assert first["id"] == "old_high", f"Expected old_high (boosted to CRITICAL), got {first['id']}"
