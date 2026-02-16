# Distributed Rate Limiter with Priority Queue & Concurrency Control

A high-performance distributed rate limiting service with priority-based scheduling, built using FastAPI, Redis simulation, and AsyncIO.

## Project Structure

```
.
├── main.py                  # FastAPI application and API endpoints
├── algorithms/
│   ├── sliding_window.py    # Sliding window counter (rate limiting)
│   ├── token_bucket.py      # Token bucket (burst control)
│   └── priority_heap.py     # Binary min-heap (priority queue scheduler)
├── storage/
│   └── redis_simulator.py   # Async Redis simulator with latency and atomicity
├── tests/
│   ├── conftest.py          # Shared fixtures and state cleanup
│   ├── test_burst_traffic.py
│   ├── test_multi_tenant.py
│   ├── test_priority_inversion.py
│   ├── test_sliding_window_precision.py
│   ├── test_heap_unit.py
│   ├── test_concurrency.py
│   └── test_distributed_nodes.py
├── benchmark.py             # Performance measurement (throughput, latency, memory)
├── redis_utils.py           # Redis helper utilities
├── run.py                   # Uvicorn launcher
├── requirements.txt
└── README.md
```

## Architecture & Design

### System Overview

```
                    ┌─────────────┐
                    │  Client     │
                    └──────┬──────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
    ┌──────────────┐┌──────────────┐┌──────────────┐
    │  API Node 1  ││  API Node 2  ││  API Node 3  │
    │  (FastAPI)   ││  (FastAPI)   ││  (FastAPI)   │
    └──────┬───────┘└──────┬───────┘└──────┬───────┘
           │               │               │
           └───────────────┼───────────────┘
                           ▼
                  ┌─────────────────┐
                  │  Shared Redis   │
                  │  (Atomic Ops)   │
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
     ┌──────────────┐┌───────────┐┌───────────┐
     │Sliding Window││Token      ││Priority   │
     │Counter       ││Bucket     ││Queue      │
     └──────────────┘└───────────┘└───────────┘
```

Each incoming request flows through three layers:
1. **Sliding Window Counter** — Enforces the per-tenant request rate (e.g., 100 req/s)
2. **Token Bucket** — Controls burst capacity (allows short bursts above the sustained rate)
3. **Priority Queue (Min-Heap)** — Schedules rejected requests by priority for deferred processing

### Algorithm Choices & Trade-offs

#### 1. Sliding Window Counter

**Choice:** Weighted sliding window counter with two fixed windows.

**How it works:** Divides time into fixed 1-second windows and maintains a counter per window in Redis. For any given moment, the estimated request count is:

```
estimated = previous_window_count * (1 - elapsed_fraction) + current_window_count
```

**Trade-offs:**
- **O(1) space per client** — Only 2 keys per client/tenant at any time (current + previous window), vs. O(n) for a sorted-set approach that stores every request timestamp.
- **Approximate vs. exact** — The weighted formula is an approximation. It can slightly over- or under-count at window boundaries. A sorted-set approach would be exact but costs O(n) memory and O(log n) per operation.
- **Atomic increment-then-check** — We increment optimistically and rollback on rejection. This avoids a read-then-write race condition where two concurrent requests both read "under limit" and both increment, exceeding the limit.

**Why not other approaches:**
- **Fixed window counter:** Vulnerable to the "boundary burst" problem — a client can send 2x the limit by splitting requests across a window boundary.
- **Sliding window log (sorted set):** Exact but O(n) memory per client. Doesn't scale for millions of clients.
- **Leaky bucket:** Better for traffic shaping than rate limiting. Doesn't allow any burst at all.

#### 2. Token Bucket (Burst Control)

**Choice:** Token bucket using a "consumed tokens" model stored in Redis.

**How it works:** Instead of tracking available tokens (which requires frequent writes on refill), we track consumed tokens and compute refills mathematically on each request:

```
consumed = max(0, stored_consumed - (elapsed_time * refill_rate))
```

**Trade-offs:**
- **Lazy refill** — Tokens are refilled on access, not via a timer. This is O(1) with no background overhead.
- **Allows controlled bursts** — A client under its sustained rate accumulates capacity for short bursts, which the sliding window alone doesn't permit.
- **Two-layer defense** — The sliding window enforces the sustained rate; the token bucket enforces the burst ceiling. A request must pass both checks.

#### 3. Priority Queue (Binary Min-Heap)

**Choice:** Array-backed binary min-heap with sequence numbers for FIFO tie-breaking.

**How it works:** When a request is rejected by the rate limiter, it's inserted into a per-tenant priority heap. Lower numerical priority = processed first. Items with equal priority are ordered by insertion sequence (FIFO).

**Key operations:**
- `insert(item, priority)` — Add an item while maintaining heap invariants. O(log n).
- `extract_min()` — Retrieve the highest-priority item. O(log n).
- `decrease_key(index, new_priority)` — Boost an item's priority (lower value = higher priority). Used by the aging mechanism to prevent starvation. O(log n).
- `apply_aging()` — Scans the heap and calls `decrease_key` on items waiting longer than 5 seconds, promoting NORMAL→HIGH→CRITICAL.

**Trade-offs:**
- **O(log n) core operations** — Standard heap guarantees for insert, extract, and decrease_key.
- **FIFO within same priority** — Sequence numbers break ties so equal-priority items are processed in insertion order.
- **In-memory, not distributed** — The priority queue is per-node, not shared via Redis. In a real system, you'd use a distributed priority queue (e.g., Redis sorted sets) or route queued items consistently to the same node. We chose in-memory for latency.

### Distributed Consistency

**Approach:** All rate-limiting state (window counters, token bucket state) is stored in the shared Redis simulator. Multiple API nodes connect to the same Redis instance. This is demonstrated in `tests/test_distributed_nodes.py`, which creates 3 independent FastAPI app instances sharing a single `RedisSimulator` and verifies that the global rate limit is enforced across all nodes.

**Atomic operations:** Each Redis operation (get, set, incr, decr) is individually atomic (protected by asyncio.Lock in the simulator). Application-level locks per rate-limit key ensure that read-check-write sequences are atomic across concurrent requests within a node. Cross-node atomicity is provided by Redis's single-threaded model (simulated by the global lock).

**How we handle clock skew:** All nodes use `time.time()` which returns wall-clock time. In production with real Redis:
- Redis's `TIME` command provides a consistent clock.
- Alternatively, use Redis Lua scripts that compute time server-side.
- For our simulator, all nodes are in the same process so clocks are inherently synchronized.

### Fault Tolerance (Redis Failure)

In a production deployment, Redis failure would be handled by:

1. **Local fallback:** Each node maintains a local in-memory rate limiter as a fallback. If Redis is unreachable, the node switches to local-only mode with conservative limits (lower than normal to account for lack of coordination).
2. **Redis Sentinel / Cluster:** Use Redis Sentinel for automatic failover, or Redis Cluster for horizontal scaling with hash-slot partitioning.
3. **Circuit breaker:** After N consecutive Redis failures, stop trying for a cooldown period and use local limits. This prevents cascading latency from Redis timeouts.
4. **Graceful degradation:** When in fallback mode, the system errs on the side of allowing requests (fail-open) rather than blocking everything (fail-closed), since a brief over-limit window is preferable to a full outage.

### Scaling to Millions of Tenants

1. **Key sharding:** Use Redis Cluster with hash tags to ensure all keys for a tenant land on the same shard: `rate:{tenant_a}:{window}` → shard by `{tenant_a}`.
2. **Hot key mitigation:** For tenants with extremely high traffic, replicate their counters across multiple shards and merge counts periodically (trading some precision for throughput).
3. **Lazy expiration:** Window counter keys have TTLs (2x window size). Redis automatically evicts expired keys, keeping memory bounded.
4. **Separate token bucket shards:** Token bucket state is lightweight (2 keys per tenant) and shards naturally by tenant_id.
5. **Priority queue partitioning:** Route queued requests to specific nodes by tenant_id (consistent hashing), keeping each node's heap manageable.

### Concurrency Control

- **Application-level async locks:** Each rate-limit key gets its own `asyncio.Lock`. This serializes concurrent requests for the same key within a node while allowing full parallelism for different keys.
- **Optimistic increment with rollback:** The sliding window increments the counter *before* checking the limit, then decrements (rolls back) on rejection. This ensures that even if two coroutines interleave, the counter is always accurate.
- **Redis-level atomicity:** The `RedisSimulator` protects all operations with a global lock, matching Redis's single-threaded execution model. In production, Lua scripts or `MULTI/EXEC` transactions would provide the same guarantee.

---

## How to Run

### Prerequisites

- Python 3.9 or higher
- pip (Python package manager)

### Installation

```bash
pip install -r requirements.txt
```

### Running the Service

#### Using uvicorn directly
```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

#### Using the run script
```bash
python run.py
```

The API will be available at `http://localhost:8000`

### API Documentation

Once the service is running, visit:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

### API Specification

#### `POST /v1/rate-limit/check` — Rate Check

Validates if a request should proceed or be queued.

| Field | Type | Description |
|---|---|---|
| **Request** | | |
| `client_id` | string | Client identifier |
| `tenant_id` | string | Tenant identifier |
| `priority` | int | 0 = CRITICAL, 1 = HIGH, 2 = NORMAL (default) |
| **Response** | | |
| `allowed` | bool | Whether the request is permitted |
| `retry_after` | float/null | Seconds to wait before retrying (if rejected) |
| `queued` | bool | Whether the request was added to the priority queue |

#### `GET /v1/queue/status?tenant_id={id}` — Queue Status

Returns the current depth and processing state of the tenant's priority queue.

| Field | Type | Description |
|---|---|---|
| `tenant_id` | string | Tenant identifier |
| `queue_depth` | int | Number of items waiting in the queue |
| `processing` | bool | Whether a background processor is active |

#### `PUT /v1/tenant/config` — Tenant Configuration

Dynamically updates rate limits and burst settings. Resets existing limiters for the tenant.

| Field | Type | Description |
|---|---|---|
| `tenant_id` | string | Tenant identifier |
| `rate_limit` | int | Max requests per second (sliding window) |
| `burst_capacity` | int | Max burst size (token bucket capacity) |
| `refill_rate` | int | Tokens refilled per second |

### Example API Usage

Configure a tenant:
```bash
curl -X PUT http://localhost:8000/v1/tenant/config \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "my_tenant",
    "rate_limit": 100,
    "burst_capacity": 200,
    "refill_rate": 100
  }'
```

Check rate limit:
```bash
curl -X POST http://localhost:8000/v1/rate-limit/check \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "client_123",
    "tenant_id": "my_tenant",
    "priority": 2
  }'
```

Get queue status:
```bash
curl http://localhost:8000/v1/queue/status?tenant_id=my_tenant
```

---

## Testing

### Running Tests

Run all tests:
```bash
pytest tests/ -v
```

Run specific test suites:
```bash
pytest tests/test_burst_traffic.py -v          # Burst traffic patterns
pytest tests/test_multi_tenant.py -v           # Multi-tenant fairness
pytest tests/test_priority_inversion.py -v     # Priority inversion & aging
pytest tests/test_sliding_window_precision.py -v  # Sliding window precision
pytest tests/test_heap_unit.py -v              # Heap algorithmic correctness
pytest tests/test_concurrency.py -v            # Race condition tests
pytest tests/test_distributed_nodes.py -v      # Multi-node distributed tests
```

### Running Benchmarks

```bash
python benchmark.py
```

Runs three benchmark scenarios:
1. **Throughput test** — 10,000 requests with 100 concurrency. Measures sustained req/s.
2. **Burst test** — 500 concurrent requests against a 100 req/s limit. Measures rejection behavior under load.
3. **Priority test** — 50 normal + 10 critical requests against a 10 req/s limit. Measures queue depth and priority handling.

Reports:
- Throughput (requests/second) — target: >10,000 req/s
- Latency (p50, p95, p99) — target: p95 < 5ms
- Peak memory usage (via `tracemalloc`)
- PASS/FAIL summary against targets

### Test Coverage

| Test Suite | What It Validates |
|---|---|
| `test_burst_traffic` | Burst capacity limits, retry-after headers |
| `test_multi_tenant` | Tenant isolation, noisy-neighbor prevention, fairness |
| `test_priority_inversion` | Queue ordering, priority aging mechanism |
| `test_sliding_window_precision` | Window boundary precision, weighted formula correctness |
| `test_heap_unit` | Heap invariants, insert/extract ordering, decrease_key, FIFO, aging |
| `test_concurrency` | Race conditions under concurrent access, cross-tenant independence |
| `test_distributed_nodes` | 3 API nodes sharing Redis, global rate limit enforcement |
