"""
Service C - Log Aggregator / Metrics Collector
Receives processed events from Service B, stores metrics,
and exposes aggregated statistics for the benchmark dashboard.
"""

import time
import json
import logging
import os
import threading
from collections import defaultdict, deque
from typing import Dict, List

from flask import Flask, request, jsonify

logging.basicConfig(level=logging.INFO, format='%(asctime)s [ServiceC] %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
REST_PORT = int(os.getenv("REST_PORT", "8002"))

# In-memory event store (ring buffer, max 10000 events)
MAX_EVENTS = 10000
events: deque = deque(maxlen=MAX_EVENTS)
events_lock = threading.Lock()

# Aggregated counters
stats: Dict = defaultdict(lambda: {"count": 0, "total_bytes": 0})
stats_lock = threading.Lock()


# ─────────────────────────────────────────────
# REST Endpoints
# ─────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "service_c", "timestamp": time.time()})


@app.route("/log", methods=["POST"])
def log_event():
    """Receive and store a processed event from Service B."""
    data = request.get_json(force=True)
    event = {
        "received_at": time.time(),
        "protocol": data.get("protocol", "unknown"),
        "checksum": data.get("checksum"),
        "payload_size_bytes": data.get("payload_size_bytes", 0),
        "processing_time_ms": data.get("processing_time_ms", 0),
        "service": data.get("service", "unknown"),
    }

    with events_lock:
        events.append(event)

    with stats_lock:
        key = event["protocol"]
        stats[key]["count"] += 1
        stats[key]["total_bytes"] += event["payload_size_bytes"]

    return jsonify({"status": "logged", "total_events": len(events)})


@app.route("/stats", methods=["GET"])
def get_stats():
    """Return aggregated statistics."""
    with stats_lock:
        summary = dict(stats)

    return jsonify({
        "service": "service_c",
        "total_logged_events": len(events),
        "by_protocol": summary,
        "timestamp": time.time(),
    })


@app.route("/events/recent", methods=["GET"])
def recent_events():
    """Return the N most recent events."""
    n = int(request.args.get("n", 50))
    with events_lock:
        recent = list(events)[-n:]
    return jsonify({"events": recent, "count": len(recent)})


@app.route("/events/clear", methods=["POST"])
def clear_events():
    """Clear all stored events (useful between benchmark runs)."""
    with events_lock:
        events.clear()
    with stats_lock:
        stats.clear()
    return jsonify({"status": "cleared"})


@app.route("/report", methods=["GET"])
def generate_report():
    """Generate a summary report of all logged events."""
    with events_lock:
        all_events = list(events)

    if not all_events:
        return jsonify({"message": "No events logged yet"})

    # Group by protocol
    by_protocol: Dict[str, List] = defaultdict(list)
    for ev in all_events:
        by_protocol[ev["protocol"]].append(ev)

    report = {}
    for protocol, evs in by_protocol.items():
        sizes = [e["payload_size_bytes"] for e in evs]
        report[protocol] = {
            "count": len(evs),
            "avg_payload_bytes": sum(sizes) / len(sizes) if sizes else 0,
            "total_bytes_processed": sum(sizes),
        }

    return jsonify({
        "report": report,
        "total_events": len(all_events),
        "generated_at": time.time(),
    })


if __name__ == "__main__":
    logger.info(f"Service C (Log Aggregator) starting on port {REST_PORT}")
    app.run(host="0.0.0.0", port=REST_PORT, threaded=True)
