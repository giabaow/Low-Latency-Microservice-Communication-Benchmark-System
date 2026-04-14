"""
Service B - Processing Service
Exposes both REST (HTTP/JSON) and gRPC (Protobuf) endpoints.
Receives requests from Service A, processes them, and forwards to Service C.
"""

import time
import json
import asyncio
import hashlib
import logging
import os
import sys
import threading
from concurrent import futures

import grpc
from flask import Flask, request, jsonify
import requests

# Add proto path
sys.path.insert(0, '/app/proto')
try:
    import messages_pb2
    import messages_pb2_grpc
except ImportError:
    print("Proto files not available")

logging.basicConfig(level=logging.INFO, format='%(asctime)s [ServiceB] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

SERVICE_C_URL = os.getenv("SERVICE_C_URL", "http://service_c:8002")
GRPC_PORT = int(os.getenv("GRPC_PORT", "50051"))
REST_PORT = int(os.getenv("REST_PORT", "8001"))

# Request counter for metrics
request_count = {"rest": 0, "grpc": 0}
request_lock = threading.Lock()


def process_payload(payload: dict) -> dict:
    """
    Simulate processing: compute a checksum and echo back with metadata.
    In production this could be CPU-bound work, DB queries, etc.
    """
    payload_str = json.dumps(payload, sort_keys=True)
    checksum = hashlib.sha256(payload_str.encode()).hexdigest()[:16]
    return {
        "status": "processed",
        "checksum": checksum,
        "processed_at": time.time(),
        "payload_size_bytes": len(payload_str),
        "service": "service_b",
    }


def forward_to_service_c(data: dict):
    """Async fire-and-forget log to Service C."""
    try:
        requests.post(
            f"{SERVICE_C_URL}/log",
            json=data,
            timeout=2,
        )
    except Exception:
        pass  # Non-critical: don't fail if service C is down


# ─────────────────────────────────────────────
# REST Endpoints (Flask)
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "service_b", "timestamp": time.time()})


@app.route("/metrics", methods=["GET"])
def metrics():
    return jsonify({
        "service": "service_b",
        "request_counts": dict(request_count),
        "uptime": time.time(),
    })


@app.route("/process", methods=["POST"])
def process():
    recv_time = time.perf_counter()
    data = request.get_json(force=True)

    with request_lock:
        request_count["rest"] += 1

    result = process_payload(data)
    result["protocol"] = "REST"
    result["request_id"] = request_count["rest"]

    # Fire-and-forget log to Service C (don't wait for response)
    log_thread = threading.Thread(target=forward_to_service_c, args=(result,), daemon=True)
    log_thread.start()

    elapsed = (time.perf_counter() - recv_time) * 1000
    result["processing_time_ms"] = round(elapsed, 3)

    return jsonify(result)


@app.route("/process/batch", methods=["POST"])
def process_batch():
    """Process a batch of messages at once."""
    recv_time = time.perf_counter()
    items = request.get_json(force=True)

    if not isinstance(items, list):
        return jsonify({"error": "Expected a list"}), 400

    results = [process_payload(item) for item in items]
    elapsed = (time.perf_counter() - recv_time) * 1000

    return jsonify({
        "results": results,
        "batch_size": len(items),
        "total_processing_time_ms": round(elapsed, 3),
    })


# ─────────────────────────────────────────────
# gRPC Service Implementation
# ─────────────────────────────────────────────

class ProcessorServicer(messages_pb2_grpc.ProcessorServicer):
    def Process(self, request, context):
        recv_time = time.perf_counter()

        with request_lock:
            request_count["grpc"] += 1

        # Decode payload
        try:
            payload = json.loads(request.payload.decode())
        except Exception:
            payload = {"message": request.message, "id": request.id}

        result = process_payload(payload)

        # Fire-and-forget log
        log_thread = threading.Thread(
            target=forward_to_service_c, args=(result,), daemon=True
        )
        log_thread.start()

        elapsed = (time.perf_counter() - recv_time) * 1000

        return messages_pb2.ProcessResponse(
            status="processed",
            checksum=result["checksum"],
            processed_at=result["processed_at"],
            payload_size_bytes=result["payload_size_bytes"],
            processing_time_ms=elapsed,
        )

    def ProcessStream(self, request_iterator, context):
        """Bidirectional streaming RPC for high-throughput scenarios."""
        for req in request_iterator:
            try:
                payload = json.loads(req.payload.decode())
            except Exception:
                payload = {"message": req.message}

            result = process_payload(payload)

            yield messages_pb2.ProcessResponse(
                status="processed",
                checksum=result["checksum"],
                processed_at=result["processed_at"],
                payload_size_bytes=result["payload_size_bytes"],
                processing_time_ms=0.0,
            )


def serve_grpc():
    """Start the gRPC server."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    try:
        messages_pb2_grpc.add_ProcessorServicer_to_server(ProcessorServicer(), server)
    except Exception as e:
        logger.warning(f"Could not register gRPC servicer (proto not available): {e}")
        return

    server.add_insecure_port(f"[::]:{GRPC_PORT}")
    server.start()
    logger.info(f"gRPC server started on port {GRPC_PORT}")
    server.wait_for_termination()


if __name__ == "__main__":
    # Start gRPC server in background thread
    grpc_thread = threading.Thread(target=serve_grpc, daemon=True)
    grpc_thread.start()

    logger.info(f"REST server starting on port {REST_PORT}")
    app.run(host="0.0.0.0", port=REST_PORT, threaded=True)
