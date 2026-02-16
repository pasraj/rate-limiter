import asyncio
import time
import statistics
import tracemalloc
from httpx import AsyncClient, ASGITransport
from main import app, app_state
from storage.redis_simulator import RedisSimulator


class PerformanceBenchmark:
    def __init__(self):
        self.latencies = []
        self.successful_requests = 0
        self.failed_requests = 0

    async def single_request(self, client: AsyncClient, client_id: str, tenant_id: str):
        start_time = time.perf_counter()

        try:
            response = await client.post(
                "/v1/rate-limit/check",
                json={
                    "client_id": client_id,
                    "tenant_id": tenant_id,
                    "priority": 2
                }
            )

            latency = (time.perf_counter() - start_time) * 1000
            self.latencies.append(latency)

            if response.status_code == 200:
                self.successful_requests += 1
            else:
                self.failed_requests += 1

        except Exception:
            self.failed_requests += 1
            latency = (time.perf_counter() - start_time) * 1000
            self.latencies.append(latency)

    def _report_metrics(self, test_name: str, duration: float, num_requests: int):
        throughput = num_requests / duration
        avg_latency = statistics.mean(self.latencies)
        p50 = statistics.median(self.latencies)
        p95 = statistics.quantiles(self.latencies, n=20)[18] if len(self.latencies) >= 20 else max(self.latencies)
        p99 = statistics.quantiles(self.latencies, n=100)[98] if len(self.latencies) >= 100 else max(self.latencies)

        print(f"\n{'=' * 60}")
        print(f"  {test_name}")
        print(f"{'=' * 60}")
        print(f"  Requests:    {num_requests}")
        print(f"  Duration:    {duration:.2f}s")
        print(f"  Throughput:  {throughput:,.0f} req/s")
        print(f"  Allowed:     {self.successful_requests}")
        print(f"  Rejected:    {self.failed_requests}")
        print(f"  Avg Latency: {avg_latency:.2f}ms")
        print(f"  p50 Latency: {p50:.2f}ms")
        print(f"  p95 Latency: {p95:.2f}ms")
        print(f"  p99 Latency: {p99:.2f}ms")
        print(f"{'=' * 60}")

        return {
            "throughput": throughput,
            "avg_latency": avg_latency,
            "p50": p50,
            "p95": p95,
            "p99": p99,
        }

    def _reset(self):
        self.latencies.clear()
        self.successful_requests = 0
        self.failed_requests = 0

    async def run_throughput_test(self, num_requests: int = 10000, concurrency: int = 100):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.put(
                "/v1/tenant/config",
                json={
                    "tenant_id": "benchmark_tenant",
                    "rate_limit": 10000,
                    "burst_capacity": 20000,
                    "refill_rate": 10000
                }
            )

            start_time = time.time()

            semaphore = asyncio.Semaphore(concurrency)

            async def bounded_request(i):
                async with semaphore:
                    await self.single_request(client, f"client_{i}", "benchmark_tenant")

            tasks = [bounded_request(i) for i in range(num_requests)]
            await asyncio.gather(*tasks)

            duration = time.time() - start_time

        return self._report_metrics("Throughput Test", duration, num_requests)

    async def run_burst_test(self):
        self._reset()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.put(
                "/v1/tenant/config",
                json={
                    "tenant_id": "burst_tenant",
                    "rate_limit": 100,
                    "burst_capacity": 200,
                    "refill_rate": 100
                }
            )

            start_time = time.time()
            tasks = [self.single_request(client, f"client_{i}", "burst_tenant") for i in range(500)]
            await asyncio.gather(*tasks)
            duration = time.time() - start_time

        return self._report_metrics("Burst Test (500 reqs, limit 100)", duration, 500)

    async def run_priority_test(self):
        self._reset()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.put(
                "/v1/tenant/config",
                json={
                    "tenant_id": "priority_tenant",
                    "rate_limit": 10,
                    "burst_capacity": 20,
                    "refill_rate": 10
                }
            )

            start_time = time.time()

            for i in range(50):
                await client.post(
                    "/v1/rate-limit/check",
                    json={
                        "client_id": f"normal_{i}",
                        "tenant_id": "priority_tenant",
                        "priority": 2
                    }
                )

            for i in range(10):
                await client.post(
                    "/v1/rate-limit/check",
                    json={
                        "client_id": f"critical_{i}",
                        "tenant_id": "priority_tenant",
                        "priority": 0
                    }
                )

            duration = time.time() - start_time

            response = await client.get("/v1/queue/status?tenant_id=priority_tenant")
            data = response.json()
            print(f"\n  Priority Queue Depth: {data.get('queue_depth')}")
            print(f"  Processing Active:    {data.get('processing')}")

        return self._report_metrics("Priority Test (50 normal + 10 critical)", duration, 60)

    async def run_all_benchmarks(self):
        print("\n" + "#" * 60)
        print("  DISTRIBUTED RATE LIMITER — PERFORMANCE BENCHMARK")
        print("#" * 60)

        # Memory tracking
        tracemalloc.start()
        mem_before = tracemalloc.get_traced_memory()

        results = {}
        results["throughput"] = await self.run_throughput_test(num_requests=10000, concurrency=100)

        self._reset()
        results["burst"] = await self.run_burst_test()
        results["priority"] = await self.run_priority_test()

        mem_after = tracemalloc.get_traced_memory()
        peak_memory = mem_after[1]
        tracemalloc.stop()

        print(f"\n{'=' * 60}")
        print(f"  MEMORY")
        print(f"{'=' * 60}")
        print(f"  Peak Memory Usage: {peak_memory / 1024:.1f} KB ({peak_memory / (1024*1024):.2f} MB)")
        print(f"{'=' * 60}")

        # Summary
        print(f"\n{'#' * 60}")
        print(f"  SUMMARY")
        print(f"{'#' * 60}")
        tp = results["throughput"]
        print(f"  Throughput:  {tp['throughput']:,.0f} req/s {'PASS' if tp['throughput'] > 10000 else 'FAIL'} (target: >10,000)")
        print(f"  p95 Latency: {tp['p95']:.2f}ms {'PASS' if tp['p95'] < 5 else 'FAIL'} (target: <5ms)")
        print(f"  Peak Memory: {peak_memory / 1024:.1f} KB")
        print(f"{'#' * 60}\n")

        return results


async def main():
    app_state["redis"] = RedisSimulator(latency_ms=1)

    benchmark = PerformanceBenchmark()
    await benchmark.run_all_benchmarks()


if __name__ == "__main__":
    asyncio.run(main())
