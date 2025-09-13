from dataclasses import dataclass
from typing import List, Optional
import argparse
import asyncio
import aiohttp
import json
import time
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

@dataclass
class EndpointConfig:
    method: str
    path: str
    body: Optional[dict] = None


@dataclass
class Result:
    status: int
    latency_ms: float
    ok: bool
    endpoint: str = ""


async def call_endpoint(session: aiohttp.ClientSession, base_url: str, ep: EndpointConfig, token: Optional[str]) -> Result:
    url = base_url.rstrip("/") + ep.path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = json.dumps(ep.body) if ep.body is not None else None

    start = time.perf_counter()
    try:
        async with session.request(ep.method.upper(), url, data=data, headers=headers) as resp:
            # read response to completion (without storing) to measure full latency
            await resp.read()
            latency_ms = (time.perf_counter() - start) * 1000
            is_ok = (200 <= resp.status < 400)
            return Result(status=resp.status, latency_ms=latency_ms, ok=is_ok, endpoint=ep.path)
    except Exception:
        latency_ms = (time.perf_counter() - start) * 1000
        return Result(status=0, latency_ms=latency_ms, ok=False, endpoint=ep.path)


async def worker(name: int, queue: asyncio.Queue, session: aiohttp.ClientSession, base_url: str, token: Optional[str], results: List[Result], delay: float = 0.1):
    while True:
        item = await queue.get()
        if item is None:
            queue.task_done()
            return
        ep = item
        res = await call_endpoint(session, base_url, ep, token)
        results.append(res)
        queue.task_done()
        
        # Pequeno delay para evitar rate limiting
        if delay > 0:
            await asyncio.sleep(delay)


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


async def run_test(base_url: str, token: Optional[str], concurrency: int, requests: int, endpoints: List[EndpointConfig], delay: float = 0.1):
    queue: asyncio.Queue = asyncio.Queue()

    # Round-robin enqueue endpoints
    for i in range(requests):
        queue.put_nowait(endpoints[i % len(endpoints)])

    # Poison pills
    for _ in range(concurrency):
        queue.put_nowait(None)

    results: List[Result] = []

    timeout = aiohttp.ClientTimeout(total=60)
    connector = aiohttp.TCPConnector(ssl=False, limit=0)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        workers = [asyncio.create_task(worker(i, queue, session, base_url, token, results, delay)) for i in range(concurrency)]
        start = time.perf_counter()
        await queue.join()
        duration_s = time.perf_counter() - start
        for w in workers:
            w.cancel()
        # Wait for cancellations
        await asyncio.gather(*workers, return_exceptions=True)

    # Metrics
    latencies = [r.latency_ms for r in results]
    total = len(results)
    success = sum(1 for r in results if r.ok)
    errors = total - success
    rps = total / duration_s if duration_s > 0 else 0

    print("\n=== Performance Summary ===")
    print(f"Requests: {total} in {duration_s:.2f}s  |  Concurrency: {concurrency}  |  RPS: {rps:.2f}")
    print(f"Success: {success}  Errors: {errors}")
    if latencies:
        print("Latency (ms): min={:.2f} p50={:.2f} p90={:.2f} p95={:.2f} p99={:.2f} max={:.2f}".format(
            min(latencies), percentile(latencies, 50), percentile(latencies, 90), percentile(latencies, 95), percentile(latencies, 99), max(latencies)
        ))

    # Status breakdown
    from collections import Counter
    codes = Counter(r.status for r in results)
    print("Status codes:", dict(sorted(codes.items())))
    
    # Breakdown por endpoint
    print("\n=== Breakdown por Endpoint ===")
    endpoint_stats = {}
    for r in results:
        if r.endpoint not in endpoint_stats:
            endpoint_stats[r.endpoint] = {"total": 0, "success": 0, "latencies": []}
        endpoint_stats[r.endpoint]["total"] += 1
        if r.ok:
            endpoint_stats[r.endpoint]["success"] += 1
        endpoint_stats[r.endpoint]["latencies"].append(r.latency_ms)
    
    for endpoint, stats in endpoint_stats.items():
        success_rate = (stats["success"] / stats["total"]) * 100
        avg_latency = sum(stats["latencies"]) / len(stats["latencies"])
        print(f"{endpoint}: {stats['success']}/{stats['total']} ({success_rate:.1f}%) - avg: {avg_latency:.1f}ms")


def default_endpoints() -> List[EndpointConfig]:
    return [
        EndpointConfig("GET", "/v2/status"),
        EndpointConfig("GET", "/v2/logs_automaticos/status/"),
        EndpointConfig("GET", "/v2/auth/test"),
    ]


def main():
    parser = argparse.ArgumentParser(description="API performance test")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL, ex: http://localhost:8000")
    parser.add_argument("--token", default=None, help="Bearer token (opcional)")
    parser.add_argument("--concurrency", type=int, default=10, help="Número de workers simultâneos")
    parser.add_argument("--requests", type=int, default=100, help="Total de requisições")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay entre requisições (segundos)")
    parser.add_argument("--endpoints-json", default=None, help="Caminho para JSON com endpoints personalizados")

    args = parser.parse_args()

    if args.endpoints_json:
        with open(args.endpoints_json, "r", encoding="utf-8") as f:
            raw = json.load(f)
            endpoints = [EndpointConfig(**e) for e in raw]
    else:
        endpoints = default_endpoints()

    asyncio.run(run_test(args.base_url, args.token, args.concurrency, args.requests, endpoints, args.delay))


if __name__ == "__main__":
    main()

# python tools/perf_test.py --base-url https://sua-api.com

# python tools/perf_test.py --base-url https://sua-api.com --requests 500 --concurrency 50

# python tools/perf_test.py --base-url https://sua-api.com --token SEU_TOKEN