"""
offline_test.py
===============
Test the Heiken Ashi + Bollinger Band strategy in VSCode WITHOUT any broker
connection. Runs the exact same indicators.py + strategy.py logic the live
engine uses, on either synthetic option-premium candles or your own CSV.

This lets you verify the entry/SL/target/trail behaviour any time of day.

USAGE
-----
1) Synthetic demo (default):
       python src/offline_test.py

2) Your own CSV of option candles:
       python src/offline_test.py path/to/option_candles.csv
   CSV must have columns (case-insensitive):
       datetime, open, high, low, close   (volume optional)
   One row per candle of the timeframe you want to test.

It prints every signal/order event and a final summary. Tweak the CONFIG
block below to match the parameters you set in the GUI.
"""

from __future__ import annotations
import sys
import os
import math

import numpy as np
import pandas as pd

# allow running both as `python src/offline_test.py` and from inside src/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from indicators import add_ha_bollinger
from strategy import LegStrategy, LegConfig, State, ActionType


# ======================================================================
# CONFIG  — match these to your GUI settings
# ======================================================================
CONFIG = {
    "bb_period": 20,
    "bb_mult": 2.0,
    "lots": 2,          # must be even
    "lot_size": 75,     # NIFTY=75, BANKNIFTY=15/30, SENSEX=20 (check scrip master)
    "entry_pct": 0.05,  # 5% above trigger HA high
    "sl_buffer": 5.0,   # points below red HA low
    "trail_step": 5.0,  # points per trail step
    "rr_target": 2.0,   # 1:2
    "max_trades": 4,    # cap for this leg in the test
}


# ======================================================================
# Sample data generator (a clean red-below-band -> green bounce -> rally)
# ======================================================================
def make_sample_candles(n_warmup: int = 22) -> pd.DataFrame:
    """
    Build a deterministic option-premium series that contains a textbook
    setup so you can see every state transition fire.
    """
    # flat-ish warmup so the bands are tight
    warm = []
    base = 100.0
    flips = [0, 1, -1, 0, 1, -1, 0, 0, 1, -1, 0, 1, -1, 0, 1, -1, 0, 0, 1, -1, 0, 1]
    for k in range(n_warmup):
        warm.append(base + flips[k % len(flips)])
    # drop below band, green recovery, then rally through the upper band
    move = [90, 96, 110, 130, 160, 200, 210]
    close = np.array(warm + move, dtype=float)

    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) + 1.0
    lo = np.minimum(op, close) - 1.0
    dt = pd.date_range("2026-05-29 09:15", periods=len(close), freq="5min")
    return pd.DataFrame(
        {"datetime": dt, "open": op, "high": hi, "low": lo,
         "close": close, "volume": 100}
    )


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    need = {"open", "high", "low", "close"}
    if not need.issubset(df.columns):
        raise SystemExit(f"CSV must contain columns {need}; got {set(df.columns)}")
    if "datetime" not in df.columns:
        df["datetime"] = pd.date_range("2026-05-29 09:15", periods=len(df),
                                       freq="5min")
    return df


# ======================================================================
# Runner
# ======================================================================
def run(df: pd.DataFrame):
    if CONFIG["lots"] % 2 != 0:
        raise SystemExit("lots must be even.")

    ha = add_ha_bollinger(df, CONFIG["bb_period"], CONFIG["bb_mult"])

    cfg = LegConfig(
        leg="TEST", lots=CONFIG["lots"], lot_size=CONFIG["lot_size"],
        entry_pct=CONFIG["entry_pct"], sl_buffer=CONFIG["sl_buffer"],
        trail_step=CONFIG["trail_step"], rr_target=CONFIG["rr_target"],
    )
    strat = LegStrategy(cfg)

    realized = 0.0
    entry_px = None
    fills = 0

    print("=" * 92)
    print(f"Candles: {len(ha)} | BB({CONFIG['bb_period']},{CONFIG['bb_mult']}) "
          f"on HA close | lots={CONFIG['lots']} x {CONFIG['lot_size']} "
          f"| entry +{CONFIG['entry_pct']*100:g}% | SL buf {CONFIG['sl_buffer']} "
          f"| trail {CONFIG['trail_step']} | RR 1:{CONFIG['rr_target']:g}")
    print("=" * 92)

    for i in range(len(ha)):
        r = ha.iloc[i]
        row = {
            "ha_open": float(r["ha_open"]), "ha_high": float(r["ha_high"]),
            "ha_low": float(r["ha_low"]), "ha_close": float(r["ha_close"]),
            "ha_green": bool(r["ha_green"]),
            "bb_upper": float(r["bb_upper"]) if pd.notna(r["bb_upper"]) else None,
            "bb_lower": float(r["bb_lower"]) if pd.notna(r["bb_lower"]) else None,
        }
        max_reached = strat.trades_taken >= CONFIG["max_trades"]
        for a in strat.process_candle(row, max_reached):
            ts = str(r["datetime"])[:16]
            if a.type == ActionType.ENTER:
                entry_px = a.price
                fills += 1
                print(f"[{ts}] c{i:>3} ENTER     {a.qty:>4} @ {a.price:>8.2f}  | {a.reason}")
            elif a.type == ActionType.BOOK_HALF:
                pnl = (a.price - entry_px) * a.qty
                realized += pnl
                print(f"[{ts}] c{i:>3} BOOK_HALF {a.qty:>4} @ {a.price:>8.2f}  "
                      f"| pnl {pnl:+.2f} | {a.reason}")
            elif a.type == ActionType.EXIT_ALL:
                pnl = (a.price - entry_px) * a.qty if entry_px else 0.0
                realized += pnl
                print(f"[{ts}] c{i:>3} EXIT_ALL  {a.qty:>4} @ {a.price:>8.2f}  "
                      f"| pnl {pnl:+.2f} | {a.reason}")
                entry_px = None
            elif a.type == ActionType.MODIFY_SL:
                print(f"[{ts}] c{i:>3} MODIFY_SL      @ {a.price:>8.2f}  | {a.reason}")
            elif a.type == ActionType.CANCEL:
                print(f"[{ts}] c{i:>3} CANCEL                       | {a.reason}")
            else:  # INFO
                print(f"[{ts}] c{i:>3} info                          | {a.reason}")

    print("=" * 92)
    print(f"Entries taken: {strat.trades_taken} | fills: {fills} "
          f"| final state: {strat.state.value} "
          f"| realized P&L (points*qty): {realized:+.2f}")
    print("=" * 92)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        data = load_csv(sys.argv[1])
        print(f"Loaded {len(data)} candles from {sys.argv[1]}")
    else:
        data = make_sample_candles()
        print("Using built-in synthetic sample (pass a CSV path to use your own).")
    run(data)
