# api_rate_limiter.py
"""
Endpoint-aware API rate limiter for Angel One SmartAPI calls.

Why this exists:
- You need *exact* broker candles (OHLC/VWAP) via getCandleData
- You also need *fast* order execution
- A single global limiter makes candle fetches block placeOrder() and adds latency

This module provides:
- Per-endpoint rate buckets (independent locks + timers)
- A small, safe default delay per endpoint
- Backwards-compatible api_rate_limiter.wait("some-name") API used in your code

Notes:
- We intentionally keep order endpoints in a separate bucket so they don't wait
  behind candle refresh calls.
"""

from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime


def _normalize_key(api_name: str) -> str:
    """Map arbitrary call labels to a small set of endpoint buckets."""
    name = (api_name or "").lower()

    # Historical candles
    if "getcandledata" in name or "candle" in name:
        return "getCandleData"

    # Orders
    if "placeorder" in name or "place_" in name and "order" in name:
        return "placeOrder"
    if "modify" in name and "order" in name:
        return "modifyOrder"
    if "cancel" in name and "order" in name:
        return "cancelOrder"

    # Books / positions
    if "orderbook" in name:
        return "orderBook"
    if "tradebook" in name:
        return "tradeBook"
    if "position" in name:
        return "position"

    # LTP
    if "ltp" in name:
        return "ltpData"

    # Fallback
    return "other"


@dataclass
class _Bucket:
    min_interval: float
    lock: threading.Lock
    last_call_ts: float = 0.0
    total_calls: int = 0
    total_wait_s: float = 0.0


class EndpointRateLimiter:
    """
    Thread-safe per-endpoint rate limiter.

    Usage:
        from api_rate_limiter import api_rate_limiter
        api_rate_limiter.wait("getCandleData")
        resp = conn.getCandleData(params)
    """

    def __init__(self, bucket_intervals: Optional[Dict[str, float]] = None):
        # Conservative but not slow. Tune if broker returns rate-limit errors.
        defaults = {
            # Candles: Angel docs often allow higher, but be safe per instrument
            "getCandleData": 0.38,   # ~2.6 calls/sec
            # LTP: keep fast
            "ltpData": 0.12,         # ~8 calls/sec
            # Books / positions are typically stricter
            "orderBook": 1.05,       # ~1 call/sec
            "tradeBook": 1.05,
            "position": 1.05,
            # Orders: keep separate and fast
            "placeOrder": 0.15,
            "modifyOrder": 0.25,
            "cancelOrder": 0.25,
            # Misc
            "other": 0.20,
        }
        if bucket_intervals:
            defaults.update(bucket_intervals)

        self._buckets: Dict[str, _Bucket] = {
            k: _Bucket(min_interval=v, lock=threading.Lock())
            for k, v in defaults.items()
        }

    def wait(self, api_name: str = "other") -> None:
        """Wait if needed for this endpoint bucket."""
        key = _normalize_key(api_name)
        bucket = self._buckets.get(key) or self._buckets["other"]

        with bucket.lock:
            now = time.perf_counter()
            elapsed = now - bucket.last_call_ts
            wait_s = bucket.min_interval - elapsed
            if wait_s > 0:
                time.sleep(wait_s)
                bucket.total_wait_s += wait_s
                now = time.perf_counter()

            bucket.last_call_ts = now
            bucket.total_calls += 1

    def log_stats(self) -> None:
        """Print a small snapshot of limiter usage (safe to call occasionally)."""
        lines = []
        for k, b in self._buckets.items():
            avg_wait = (b.total_wait_s / b.total_calls) if b.total_calls else 0.0
            lines.append(
                f"{k:12s} calls={b.total_calls:6d} total_wait={b.total_wait_s:7.2f}s avg_wait={avg_wait:.4f}s"
            )
        print("=== API Rate Limiter Stats ===")
        print("\n".join(lines))
        print("==============================")

    # Backwards compatibility with earlier code expecting attribute
    @property
    def min_delay(self) -> float:
        # Old global value; return a representative slow endpoint delay
        return float(self._buckets["orderBook"].min_interval)


# Shared instance used by all modules
api_rate_limiter = EndpointRateLimiter()


# Backwards-compatible helper
def wait(api_name: str = "other") -> None:
    api_rate_limiter.wait(api_name)


if __name__ == "__main__":
    print("Testing endpoint-aware API rate limiter...")
    for i in range(3):
        api_rate_limiter.wait("getCandleData")
        print("candle", i, datetime.now().strftime("%H:%M:%S.%f")[:-3])
    for i in range(3):
        api_rate_limiter.wait("placeOrder")
        print("order ", i, datetime.now().strftime("%H:%M:%S.%f")[:-3])
    api_rate_limiter.log_stats()
