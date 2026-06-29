"""
offline_test.py
===============
Test the NEW tick-based Heiken Ashi + Bollinger strategy in VSCode without a
broker. Signal detection runs on HA candle close; execution is simulated by
replaying intra-candle "ticks" (open -> high -> low -> close) through the same
process_tick() the live engine uses.

USAGE
-----
    python src/offline_test.py                 # built-in synthetic sample
    python src/offline_test.py candles.csv     # your own CSV (datetime,open,high,low,close)

Tune CONFIG below to match your GUI settings. Lots are pairs (1 lot = 2 units),
and the target is in PREMIUM POINTS (half booked at half the target).
"""

from __future__ import annotations
import sys
import os
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators import add_ha_bollinger
from strategy import LegStrategy, LegConfig, State, ActionType

CONFIG = {
    "bb_period": 20, "bb_mult": 2.0,
    "lots": 1, "lot_size": 65,
    "entry_pct": 0.05, "sl_buffer": 5.0,
    "trail_step": 5.0, "book_half_points": 20.0,
    "max_trades": 4,
}


def make_sample_candles(n_warmup: int = 22) -> pd.DataFrame:
    flips = [0,1,-1,0,1,-1,0,0,1,-1,0,1,-1,0,1,-1,0,0,1,-1,0,1]
    warm = [100.0 + flips[k % len(flips)] for k in range(n_warmup)]
    move = [90, 96, 110, 130, 160, 200, 210]
    close = np.array(warm + move, dtype=float)
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) + 1.0
    lo = np.minimum(op, close) - 1.0
    dt = pd.date_range("2026-06-23 09:15", periods=len(close), freq="5min")
    return pd.DataFrame({"datetime": dt, "open": op, "high": hi, "low": lo,
                         "close": close, "volume": 100})


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    if not {"open","high","low","close"}.issubset(df.columns):
        raise SystemExit("CSV needs columns: open, high, low, close (datetime optional)")
    if "datetime" not in df.columns:
        df["datetime"] = pd.date_range("2026-06-23 09:15", periods=len(df), freq="5min")
    return df


def run(df: pd.DataFrame):
    ha = add_ha_bollinger(df, CONFIG["bb_period"], CONFIG["bb_mult"])
    cfg = LegConfig(leg="TEST", lots=CONFIG["lots"], lot_size=CONFIG["lot_size"],
                    entry_pct=CONFIG["entry_pct"], sl_buffer=CONFIG["sl_buffer"],
                    trail_step=CONFIG["trail_step"], book_half_points=CONFIG["book_half_points"])
    s = LegStrategy(cfg)
    realized = 0.0
    entry_px = None

    print("=" * 90)
    print(f"qty/trade = {cfg.total_qty} (half {cfg.half_qty}) | entry +{cfg.entry_pct*100:g}% "
          f"| SL buf {cfg.sl_buffer} | book-half +{cfg.book_half_points:g}pts "
          f"(book half at entry+{cfg.book_half_points:g}, open trail) | trail {cfg.trail_step}")
    print("Execution replays intra-candle ticks: open -> high -> low -> close")
    print("=" * 90)

    def emit(a, ts):
        nonlocal realized, entry_px
        if a.type == ActionType.ENTER:
            entry_px = a.price
            print(f"[{ts}] ENTER     {a.qty:>4} @ {a.price:>8.2f} | {a.reason}")
        elif a.type == ActionType.BOOK_HALF:
            realized += (a.price - entry_px) * a.qty
            print(f"[{ts}] BOOK_HALF {a.qty:>4} @ {a.price:>8.2f} | pnl {(a.price-entry_px)*a.qty:+.0f} | {a.reason}")
        elif a.type == ActionType.EXIT_ALL:
            realized += (a.price - entry_px) * a.qty if entry_px else 0.0
            print(f"[{ts}] EXIT      {a.qty:>4} @ {a.price:>8.2f} | pnl {(a.price-entry_px)*a.qty:+.0f} | {a.reason}")
            entry_px = None
        elif a.type == ActionType.MODIFY_SL:
            print(f"[{ts}] SL ->          {a.price:>8.2f} | {a.reason}")
        elif a.type in (ActionType.CANCEL, ActionType.INFO):
            print(f"[{ts}] {a.type.value:9s}             | {a.reason}")

    for i in range(len(ha)):
        r = ha.iloc[i]
        ts = str(r["datetime"])[:16]
        row = {"ha_open": float(r.ha_open), "ha_high": float(r.ha_high),
               "ha_low": float(r.ha_low), "ha_close": float(r.ha_close),
               "ha_green": bool(r.ha_green),
               "bb_lower": float(r.bb_lower) if pd.notna(r.bb_lower) else None,
               "bb_upper": float(r.bb_upper) if pd.notna(r.bb_upper) else None}
        for a in s.process_candle(row):
            emit(a, ts)
        blocked = s.trades_taken >= CONFIG["max_trades"]
        for px in (row["ha_open"], row["ha_high"], row["ha_low"], row["ha_close"]):
            for a in s.process_tick(px, entries_blocked=blocked):
                emit(a, ts)
            blocked = s.trades_taken >= CONFIG["max_trades"]

    print("=" * 90)
    print(f"Entries: {s.trades_taken} | final state: {s.state.value} "
          f"| realized P&L (pts*qty): {realized:+.2f}")
    print("=" * 90)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        data = load_csv(sys.argv[1]); print(f"Loaded {len(data)} candles")
    else:
        data = make_sample_candles(); print("Built-in synthetic sample.")
    run(data)
