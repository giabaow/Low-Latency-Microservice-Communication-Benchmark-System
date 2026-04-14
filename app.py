"""
Service A - Request Generator / Load Tester
Sends requests to Service B using multiple communication protocols.
Measures latency and throughput for benchmarking.
"""

import time
import json
import statistics
import asyncio
import aiohttp
import requests
import grpc
import sys
import os
import logging
from typing import List, Dict, Any
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add proto path
sys.path.insert(0, '/app/proto')
try:
    import messages_pb2
    import messages_pb2_grpc
except ImportError:
    print("Proto files not found, gRPC benchmarks will be skipped")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [ServiceA] %(message)s')
logger = logging.getLogger(__name__)

SERVICE_B_REST = os.getenv("SERVICE_B_REST", "http://service_b:8001")
SERVICE_B_GRPC = os.getenv("SERVICE_B_GRPC", "service_b:50051")


@dataclass
class BenchmarkResult:
    protocol: str
    message_size: str
    num_requests: int
    concurrency: int
    latencies_ms: List[float]
    errors: int

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies_ms) if self.latencies_ms else 0

    @property
    def p50_latency_ms(self) -> float:
        return statistics.median(self.latencies_ms) if self.latencies_ms else 0

    @property
    def p95_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[idx]

    @property
    def p99_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        sorted_lat = sorted(self.latencies_ms)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[idx]

    @property
    def throughput_rps(self) -> float:
        if not self.latencies_ms:
            return 0
        total_time_s = sum(self.latencies_ms) / 1000
        return len(self.latencies_ms) / total_time_s if total_time_s > 0 else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocol": self.protocol,
            "message_size": self.message_size,
            "num_requests": self.num_requests,
            "concurrency": self.concurrency,
            "avg_latency_ms": round(self.avg_latency_ms, 3),
            "p50_latency_ms": round(self.p50_latency_ms, 3),
            "p95_latency_ms": round(self.p95_latency_ms, 3),
            "p99_latency_ms": round(self.p99_latency_ms, 3),
            "throughput_rps": round(self.throughput_rps, 2),
            "error_rate": round(self.errors / self.num_requests * 100, 2),
            "successful_requests": len(self.latencies_ms),
        }


def generate_payload(size: str) -> Dict:
    """Generate test payloads of different sizes."""
    payloads = {
        "small": {"id": 1, "message": "ping", "timestamp": time.time()},
        "medium": {
            "id": 1,
            "message": "x" * 1024,  # 1KB
            "data": list(range(100)),
            "metadata": {"key": "value", "timestamp": time.time()},
        },
        "large": {
            "id": 1,
            "message": "x" * 65536,  # 64KB
            "data": list(range(1000)),
            "metadata": {f"key_{i}": f"value_{i}" for i in range(100)},
            "timestamp": time.time(),
        },
    }
    return payloads.get(size, payloads["small"])


# ─────────────────────────────────────────────
# REST Benchmark
# ─────────────────────────────────────────────

def benchmark_rest_single(payload: Dict) -> float:
    """Single REST request, returns latency in ms."""
    start = time.perf_counter()
    try:
        response = requests.post(
            f"{SERVICE_B_REST}/process",
            json=payload,
            timeout=10,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        elapsed = (time.perf_counter() - start) * 1000
        return elapsed
    except Exception as e:
        return -1  # Signal error


def run_rest_benchmark(num_requests: int, message_size: str, concurrency: int = 1) -> BenchmarkResult:
    """Run REST benchmark with given parameters."""
    logger.info(f"REST benchmark: {num_requests} reqs, size={message_size}, concurrency={concurrency}")
    payload = generate_payload(message_size)
    latencies = []
    errors = 0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(benchmark_rest_single, payload) for _ in range(num_requests)]
        for future in as_completed(futures):
            result = future.result()
            if result < 0:
                errors += 1
            else:
                latencies.append(result)

    return BenchmarkResult(
        protocol="REST/JSON",
        message_size=message_size,
        num_requests=num_requests,
        concurrency=concurrency,
        latencies_ms=latencies,
        errors=errors,
    )


# ─────────────────────────────────────────────
# gRPC Benchmark
# ─────────────────────────────────────────────

def benchmark_grpc_single(stub, payload: Dict) -> float:
    """Single gRPC request, returns latency in ms."""
    start = time.perf_counter()
    try:
        request = messages_pb2.ProcessRequest(
            id=payload.get("id", 1),
            message=payload.get("message", ""),
            payload=json.dumps(payload).encode(),
        )
        response = stub.Process(request, timeout=10)
        elapsed = (time.perf_counter() - start) * 1000
        return elapsed
    except Exception as e:
        return -1


def run_grpc_benchmark(num_requests: int, message_size: str, concurrency: int = 1) -> BenchmarkResult:
    """Run gRPC benchmark."""
    logger.info(f"gRPC benchmark: {num_requests} reqs, size={message_size}, concurrency={concurrency}")
    payload = generate_payload(message_size)
    latencies = []
    errors = 0

    try:
        channel = grpc.insecure_channel(SERVICE_B_GRPC)
        stub = messages_pb2_grpc.ProcessorStub(channel)

        def single_request(_):
            return benchmark_grpc_single(stub, payload)

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(single_request, i) for i in range(num_requests)]
            for future in as_completed(futures):
                result = future.result()
                if result < 0:
                    errors += 1
                else:
                    latencies.append(result)

        channel.close()
    except Exception as e:
        logger.error(f"gRPC benchmark failed: {e}")
        errors = num_requests

    return BenchmarkResult(
        protocol="gRPC/Protobuf",
        message_size=message_size,
        num_requests=num_requests,
        concurrency=concurrency,
        latencies_ms=latencies,
        errors=errors,
    )


# ─────────────────────────────────────────────
# Main Benchmark Runner
# ─────────────────────────────────────────────

def wait_for_services(max_retries: int = 30) -> bool:
    """Wait until Service B is ready."""
    logger.info("Waiting for Service B to be ready...")
    for i in range(max_retries):
        try:
            r = requests.get(f"{SERVICE_B_REST}/health", timeout=3)
            if r.status_code == 200:
                logger.info("Service B is ready!")
                return True
        except Exception:
            pass
        time.sleep(2)
        logger.info(f"Retry {i+1}/{max_retries}...")
    return False


def run_all_benchmarks() -> List[Dict]:
    """Run the full benchmark suite."""
    results = []

    scenarios = [
        # (num_requests, message_size, concurrency)
        (100, "small", 1),
        (100, "medium", 1),
        (100, "large", 1),
        (500, "small", 10),
        (500, "medium", 10),
        (500, "small", 50),
    ]

    for num_req, size, conc in scenarios:
        # REST benchmark
        try:
            rest_result = run_rest_benchmark(num_req, size, conc)
            results.append(rest_result.to_dict())
            logger.info(f"REST {size} c={conc}: avg={rest_result.avg_latency_ms:.2f}ms "
                        f"p99={rest_result.p99_latency_ms:.2f}ms "
                        f"throughput={rest_result.throughput_rps:.1f}rps")
        except Exception as e:
            logger.error(f"REST benchmark failed: {e}")

        # gRPC benchmark
        try:
            grpc_result = run_grpc_benchmark(num_req, size, conc)
            results.append(grpc_result.to_dict())
            logger.info(f"gRPC {size} c={conc}: avg={grpc_result.avg_latency_ms:.2f}ms "
                        f"p99={grpc_result.p99_latency_ms:.2f}ms "
                        f"throughput={grpc_result.throughput_rps:.1f}rps")
        except Exception as e:
            logger.error(f"gRPC benchmark failed: {e}")

        time.sleep(1)  # Cool-down between scenarios

    return results


def save_results(results: List[Dict]):
    """Save benchmark results to file."""
    output_path = "/app/results/benchmark_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump({"timestamp": time.time(), "results": results}, f, indent=2)
    logger.info(f"Results saved to {output_path}")


if __name__ == "__main__":
    time.sleep(5)  # Initial startup delay

    if not wait_for_services():
        logger.error("Service B did not become ready. Exiting.")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("Starting Low-Latency Microservice Communication Benchmark")
    logger.info("=" * 60)

    results = run_all_benchmarks()
    save_results(results)

    logger.info("=" * 60)
    logger.info("Benchmark complete! Results saved.")
    logger.info("=" * 60)

    # Print summary table
    print("\n{'='*60}")
    print("BENCHMARK SUMMARY")
    print("=" * 60)
    for r in results:
        print(f"[{r['protocol']:15s}] size={r['message_size']:6s} "
              f"c={r['concurrency']:3d} | "
              f"avg={r['avg_latency_ms']:7.2f}ms "
              f"p99={r['p99_latency_ms']:7.2f}ms "
              f"rps={r['throughput_rps']:8.1f}")
