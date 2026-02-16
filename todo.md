# Distributed Rate Limiter — Audit & Fix Tracker

## Critical Issues

- [x] **No multi-node simulation** — Added `tests/test_distributed_nodes.py` with 3 API nodes sharing a single RedisSimulator. Tests global rate limit enforcement and atomic consistency.
- [x] **`decrease_key()` not implemented** — Added `decrease_key(index, new_priority)` to `PriorityHeap` with O(log n) bubble-up. `apply_aging()` now uses it instead of full heap rebuild.
- [x] **`process_queue()` is a no-op** — Dequeued items are now stored in `app_state["processed_items"]` for tracking. Loop terminates after 1s of idle (100 cycles) and cleans up its entry in `processing_tasks`.
- [x] **Sliding window doesn't enforce per-tenant limits** — `is_allowed()` and `rollback()` now accept `tenant_id` parameter. Rate keys use `{tenant_id}:{client_id}` for per-tenant enforcement. `main.py` updated to pass `tenant_id`.
- [x] **README has no design discussion** — Rewritten with: algorithm trade-offs (sliding window vs fixed window vs sorted set), fault tolerance strategy (local fallback, Sentinel, circuit breaker), scaling plan (key sharding, hot key mitigation, lazy TTL), concurrency reasoning (per-key async locks, optimistic increment with rollback).
- [x] **Benchmark outputs nothing** — Added `_report_metrics()` method that prints throughput, p50/p95/p99 latency, allowed/rejected counts. Added `tracemalloc` for peak memory measurement. Summary section prints PASS/FAIL against targets.

## Medium Issues

- [x] **`TokenBucket.get_status()` reads wrong keys** — Fixed to read `bucket:{id}:consumed` (matching `consume()`). Logic now correctly computes `available = capacity - consumed_after_refill`. Added app-level lock for consistency.
- [x] **Priority tests have no assertions** — `test_priority_inversion_prevention` now asserts queue front is CRITICAL. `test_priority_ordering` asserts extraction order is sorted by priority and first item is CRITICAL. Added `test_priority_aging` for aging mechanism.
- [x] **Tests use unique client_ids defeating sliding window** — `test_multi_tenant_fairness` now uses `"client_a"` for all Tenant A requests. `test_naive_counter_vs_sliding_window` now uses `"same_client"` for all requests.
- [x] **No heap unit tests** — Added `tests/test_heap_unit.py` with 13 tests: insert/extract ordering, FIFO within priority, decrease_key, empty heap, peek, heap invariant verification, aging, performance (10k items).
- [x] **No concurrency/race-condition tests** — Added `tests/test_concurrency.py` with: 200 concurrent requests same client (race condition test), 5 tenants concurrent independence test.
- [x] **Background tasks leak** — Added `tests/conftest.py` with `autouse` fixture that cancels all processing tasks before and after each test. No more "Task was destroyed but it is pending!" warnings.

## Low Issues

- [x] **Deprecated `_loop` attribute access** — Replaced `_lock._loop` check with `id(asyncio.get_running_loop())` tracking in both `token_bucket.py` and `sliding_window.py`. Works on Python 3.10+.
- [x] **Missing state cleanup in test fixtures** — `conftest.py` autouse fixture resets all `app_state` dicts, clears algorithm module locks, and cancels background tasks before every test.

## Final Status

**27 tests passing** (was 9). All assignment requirements addressed.

| File | Changes |
|---|---|
| `algorithms/priority_heap.py` | Added `decrease_key()`, refactored `apply_aging()` to use it |
| `algorithms/sliding_window.py` | Added `tenant_id` param, fixed lock helper |
| `algorithms/token_bucket.py` | Fixed `get_status()` keys/logic, fixed lock helper |
| `main.py` | Fixed `process_queue()` (processes items, terminates), passes `tenant_id` to sliding window |
| `benchmark.py` | Added output, memory tracking, PASS/FAIL summary |
| `README.md` | Full design documentation |
| `tests/conftest.py` | **New** — autouse state cleanup fixture |
| `tests/test_heap_unit.py` | **New** — 13 heap algorithm tests |
| `tests/test_concurrency.py` | **New** — race condition tests |
| `tests/test_distributed_nodes.py` | **New** — 3-node distributed tests |
| `tests/test_burst_traffic.py` | Cleaned up (conftest handles state) |
| `tests/test_multi_tenant.py` | Fixed client_ids, assertions |
| `tests/test_priority_inversion.py` | Added real assertions, aging test |
| `tests/test_sliding_window_precision.py` | Fixed client_ids, descriptions |
