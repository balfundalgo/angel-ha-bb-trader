"""
indicators.py
=============
Heiken Ashi candle construction + Bollinger Bands computed ON the
Heiken Ashi candles.

Design notes
------------
* The Bollinger Band calculation reuses the exact logic verified earlier
  against Dhan / TradingView:
      middle = N-period SMA
      std    = population standard deviation (ddof=0)   <-- matches broker
      upper  = middle + mult * std
      lower  = middle - mult * std
  Here the *source* of the bands is the Heiken Ashi close (per spec), not
  the regular close.

* All downstream high / low / close references in the strategy use the
  Heiken Ashi values (HA_open / HA_high / HA_low / HA_close), per spec.

Input dataframe must contain real OHLC columns: open, high, low, close
(case-insensitive accepted). A 'datetime' column is preserved if present.
"""

from __future__ import annotations
import pandas as pd
import numpy as np


def _normalise_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with lower-case open/high/low/close columns."""
    out = df.copy()
    rename = {}
    for c in out.columns:
        lc = str(c).lower()
        if lc in ("open", "high", "low", "close", "volume", "datetime"):
            rename[c] = lc
    out = out.rename(columns=rename)
    for col in ("open", "high", "low", "close"):
        if col not in out.columns:
            raise ValueError(f"OHLC column '{col}' missing from candle data")
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def heiken_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """
    Build Heiken Ashi candles from regular OHLC.

    HA_close = (O + H + L + C) / 4
    HA_open  = (prev HA_open + prev HA_close) / 2   (seed = (O+C)/2 of bar 0)
    HA_high  = max(High, HA_open, HA_close)
    HA_low   = min(Low,  HA_open, HA_close)

    Returns a dataframe with columns:
        datetime (if present), ha_open, ha_high, ha_low, ha_close,
        and original open/high/low/close kept as real_open ... real_close.
    """
    d = _normalise_ohlc(df)
    n = len(d)
    if n == 0:
        return pd.DataFrame()

    o = d["open"].to_numpy(dtype=float)
    h = d["high"].to_numpy(dtype=float)
    l = d["low"].to_numpy(dtype=float)
    c = d["close"].to_numpy(dtype=float)

    ha_close = (o + h + l + c) / 4.0
    ha_open = np.empty(n, dtype=float)
    ha_open[0] = (o[0] + c[0]) / 2.0
    for i in range(1, n):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0

    ha_high = np.maximum.reduce([h, ha_open, ha_close])
    ha_low = np.minimum.reduce([l, ha_open, ha_close])

    out = pd.DataFrame(
        {
            "ha_open": ha_open,
            "ha_high": ha_high,
            "ha_low": ha_low,
            "ha_close": ha_close,
            "real_open": o,
            "real_high": h,
            "real_low": l,
            "real_close": c,
        },
        index=d.index,
    )
    if "datetime" in d.columns:
        out.insert(0, "datetime", d["datetime"].values)
    if "volume" in d.columns:
        out["volume"] = d["volume"].values

    # candle colour helper: True = green (bullish HA), False = red (bearish HA)
    out["ha_green"] = out["ha_close"] >= out["ha_open"]
    return out


def bollinger_on_series(
    src: pd.Series, period: int = 20, mult: float = 2.0
) -> pd.DataFrame:
    """
    Bollinger Bands on an arbitrary source series.
    Uses POPULATION std (ddof=0) to match the broker (Dhan / TradingView).
    """
    src = pd.to_numeric(src, errors="coerce")
    middle = src.rolling(period).mean()
    std = src.rolling(period).std(ddof=0)  # population std -> broker match
    upper = middle + mult * std
    lower = middle - mult * std
    return pd.DataFrame(
        {"bb_middle": middle, "bb_upper": upper, "bb_lower": lower}
    )


def add_ha_bollinger(
    df: pd.DataFrame, bb_period: int = 20, bb_mult: float = 2.0
) -> pd.DataFrame:
    """
    Full pipeline: real OHLC -> Heiken Ashi -> Bollinger Bands on HA close.

    Returns the HA dataframe with bb_middle / bb_upper / bb_lower appended.
    Bands are computed on ha_close (per strategy spec).
    """
    ha = heiken_ashi(df)
    if ha.empty:
        return ha
    bands = bollinger_on_series(ha["ha_close"], period=bb_period, mult=bb_mult)
    ha = pd.concat([ha, bands], axis=1)
    return ha


if __name__ == "__main__":
    # Quick self-test with synthetic data
    import numpy as np

    rng = np.random.default_rng(42)
    base = 100 + np.cumsum(rng.normal(0, 1, 60))
    opens = base + rng.normal(0, 0.3, 60)
    highs = np.maximum(opens, base) + np.abs(rng.normal(0, 0.5, 60))
    lows = np.minimum(opens, base) - np.abs(rng.normal(0, 0.5, 60))
    closes = base
    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes}
    )
    res = add_ha_bollinger(df, bb_period=20, bb_mult=2.0)
    print(res.tail(5).round(2).to_string())
    # Sanity checks
    last = res.iloc[-1]
    assert last["ha_high"] >= last["ha_low"]
    assert last["bb_upper"] >= last["bb_middle"] >= last["bb_lower"]
    print("\nHA + BB self-test passed.")
