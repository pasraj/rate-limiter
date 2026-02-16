import pytest
import asyncio
from main import app_state
from storage.redis_simulator import RedisSimulator
from algorithms import sliding_window, token_bucket


def clear_algorithm_locks():
    """Clear global locks from algorithms to prevent cross-test contamination."""
    sliding_window._locks.clear()
    sliding_window._lock_creation_lock = None
    sliding_window._lock_loop_id = None
    token_bucket._locks.clear()
    token_bucket._lock_creation_lock = None
    token_bucket._lock_loop_id = None


@pytest.fixture(autouse=True)
def reset_app_state():
    """Reset all shared state before each test to prevent cross-test contamination."""
    clear_algorithm_locks()
    app_state["redis"] = RedisSimulator(latency_ms=0)
    app_state["tenant_configs"] = {}
    app_state["tenant_rate_limiters"] = {}
    app_state["tenant_token_buckets"] = {}
    app_state["priority_queues"] = {}
    app_state.pop("processed_items", None)

    # Cancel any lingering processing tasks
    for task in app_state.get("processing_tasks", {}).values():
        task.cancel()
    app_state["processing_tasks"] = {}

    yield

    # Cleanup after test
    for task in app_state.get("processing_tasks", {}).values():
        task.cancel()
    app_state["processing_tasks"] = {}
