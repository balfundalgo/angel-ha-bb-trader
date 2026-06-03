"""
candle_builder.py
=================
Builds OHLC candles locally from a live tick stream (WebSocket LTP), so the
app does NOT need to poll getCandleData during the session.

One builder per option token. It is seeded once with historical candles (for
the Bollinger warmup), then each incoming tick updates the current forming
candle; when a tick crosses into a new time bucket the previous candle is
finalised.

Thread-safe: ticks arrive on the WebSocket thread while the engine loop reads
completed candles on its own thread.

Bucket alignment matches Angel's native candles: buckets are anchored to the
session start (09:15) in steps of `timeframe` minutes, so seeded historical
candles and live-built candles line up on the same boundaries. This also
handles 4-hour directly (timeframe=240) with no separate resampling step.
"""

from __future__ import annotations
import threading
from datetime import datetime, timedelta

import pandas as pd


class CandleBuilder:
    def __init__(self, timeframe_min: int, session_start: str = "09:15"):
        self.tf = int(timeframe_min)
        sh, sm = map(int, session_start.split(":"))
        self.sess_h, self.sess_m = sh, sm
        self._lock = threading.RLock()
        self._seed: list[dict] = []      # historical completed candles
        self._completed: list[dict] = [] # live completed candles
        self._cur: dict | None = None    # forming candle
        self.last_price: float | None = None
        self.last_tick_ts: datetime | None = None

    # ------------------------------------------------------------------
    def _bucket_start(self, ts: datetime) -> datetime:
        """Start datetime of the timeframe bucket that `ts` falls in."""
        anchor = ts.replace(hour=self.sess_h, minute=self.sess_m,
                            second=0, microsecond=0)
        if ts < anchor:
            # before session start (e.g. pre-open) -> snap to anchor
            return anchor
        delta_min = (ts - anchor).total_seconds() / 60.0
        k = int(delta_min // self.tf)
        return anchor + timedelta(minutes=k * self.tf)

    # ------------------------------------------------------------------
    def seed(self, df: pd.DataFrame):
        """Seed with historical candles (datetime, open, high, low, close)."""
        if df is None or df.empty:
            return
        with self._lock:
            self._seed = []
            for _, r in df.iterrows():
                self._seed.append({
                    "datetime": pd.to_datetime(r["datetime"]).to_pydatetime()
                    if "datetime" in r else None,
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume", 0) or 0),
                })

    # ------------------------------------------------------------------
    def update(self, price: float, volume: float = 0.0, ts: datetime | None = None):
        """Feed one tick."""
        if price is None or price <= 0:
            return
        ts = ts or datetime.now()
        with self._lock:
            self.last_price = price
            self.last_tick_ts = ts
            bstart = self._bucket_start(ts)

            if self._cur is None:
                self._cur = self._new_candle(bstart, price, volume)
                return

            if bstart > self._cur["datetime"]:
                # close out current candle(s) and open a new one
                self._completed.append(self._cur)
                self._cur = self._new_candle(bstart, price, volume)
            else:
                c = self._cur
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["volume"] += volume

    def _new_candle(self, bstart, price, volume):
        return {"datetime": bstart, "open": price, "high": price,
                "low": price, "close": price, "volume": volume}

    # ------------------------------------------------------------------
    def completed_df(self) -> pd.DataFrame:
        """Seed + live completed candles (excludes the forming candle)."""
        with self._lock:
            rows = self._seed + self._completed
        if not rows:
            return pd.DataFrame(columns=["datetime", "open", "high", "low",
                                         "close", "volume"])
        df = pd.DataFrame(rows)
        # drop any seed/live overlap on identical timestamps (keep last)
        df = df.drop_duplicates(subset="datetime", keep="last")
        df = df.sort_values("datetime").reset_index(drop=True)
        return df

    def n_completed(self) -> int:
        with self._lock:
            return len(self._seed) + len(self._completed)


# ----------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------
if __name__ == "__main__":
    cb = CandleBuilder(timeframe_min=5)
    seed = pd.DataFrame({
        "datetime": pd.date_range("2026-06-01 09:15", periods=3, freq="5min"),
        "open": [100, 101, 102], "high": [101, 102, 103],
        "low": [99, 100, 101], "close": [101, 102, 102], "volume": [0, 0, 0],
    })
    cb.seed(seed)

    base = datetime(2026, 6, 1, 9, 30, 0)
    # ticks within 09:30 bucket
    for i, p in enumerate([102, 104, 103, 105]):
        cb.update(p, ts=base + timedelta(seconds=i * 20))
    # tick crossing into 09:35 bucket -> finalises 09:30 candle
    cb.update(106, ts=datetime(2026, 6, 1, 9, 35, 10))

    df = cb.completed_df()
    print(df.to_string())
    last = df.iloc[-1]  # the 09:30 candle just completed
    assert last["datetime"] == datetime(2026, 6, 1, 9, 30)
    assert last["open"] == 102 and last["high"] == 105 and last["low"] == 102
    assert last["close"] == 105
    assert cb.last_price == 106
    print("\nCandleBuilder self-test passed.")
