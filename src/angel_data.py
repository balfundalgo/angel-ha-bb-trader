"""
angel_data.py
=============
Market-data layer over Angel One SmartAPI:

  * download / cache the scrip master
  * capture the reference spot at the configured time (default 09:07,
    pre-open window) for ATM strike selection
  * resolve ATM CE / PE instruments (token, tradingsymbol, exchange,
    lot size) for NIFTY / BANKNIFTY / SENSEX, nearest expiry
  * fetch option OHLC candles for any chosen timeframe (native Angel
    intervals, with 4-hour resampled from 1-hour)

REST call patterns (getMarketData / getCandleData) follow the reference
Angel One code; rate limiting is per-endpoint.
"""

from __future__ import annotations
import os
import time
import math
import json
import random
from datetime import datetime, timedelta

import pandas as pd
import requests

import config
from logger import logger
from api_rate_limiter import api_rate_limiter


# ======================================================================
# Scrip master
# ======================================================================
def download_scrip_master(force=False) -> bool:
    """Download the public Angel scrip master JSON (no auth needed)."""
    try:
        if (not force) and os.path.exists(config.SCRIP_MASTER_FILE):
            age = time.time() - os.path.getmtime(config.SCRIP_MASTER_FILE)
            if age < 12 * 3600:  # reuse if < 12h old
                return True
        logger.info("Downloading Angel scrip master...")
        r = requests.get(config.SCRIP_MASTER_URL, timeout=60)
        r.raise_for_status()
        with open(config.SCRIP_MASTER_FILE, "w", encoding="utf-8") as f:
            f.write(r.text)
        logger.info("Scrip master saved.")
        return True
    except Exception as e:
        logger.error(f"Scrip master download failed: {e}")
        return os.path.exists(config.SCRIP_MASTER_FILE)


def _load_options(index: str) -> pd.DataFrame:
    """Load OPTIDX rows for the given index from the scrip master."""
    spec = config.INDEX_SPECS[index]
    with open(config.SCRIP_MASTER_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    df = pd.DataFrame(data)
    df = df[(df["exch_seg"] == spec["opt_exch"])
            & (df["name"] == spec["scrip_name"])
            & (df["instrumenttype"] == "OPTIDX")].copy()
    # strike stored * 100 in the master (e.g. 25500 -> 2550000)
    df["strike_val"] = pd.to_numeric(df["strike"], errors="coerce") / 100.0
    df["lotsize"] = pd.to_numeric(df["lotsize"], errors="coerce")
    df["opt"] = df["symbol"].str.extract(r"(CE|PE)$")
    df["expiry_dt"] = pd.to_datetime(df["expiry"], format="%d%b%Y",
                                     errors="coerce")
    return df.dropna(subset=["expiry_dt", "strike_val", "opt"])


def nearest_expiry(df: pd.DataFrame) -> pd.Timestamp:
    """Nearest expiry on/after today (includes today on expiry day)."""
    today = pd.Timestamp(datetime.now().date())
    fut = df[df["expiry_dt"].dt.normalize() >= today]
    if fut.empty:
        raise RuntimeError("No future expiry found in scrip master.")
    return fut["expiry_dt"].min()


# ======================================================================
# Reference spot capture (the 09:07 problem)
# ======================================================================
def _quote_ltp(index: str):
    """Single live LTP read for the spot index via getMarketData."""
    spec = config.INDEX_SPECS[index]
    obj = config.SMART
    api_rate_limiter.wait("ltpData")
    try:
        resp = obj.getMarketData(
            mode="LTP",
            exchangeTokens={spec["spot_exch"]: [spec["token"]]},
        )
        fetched = (resp or {}).get("data", {}).get("fetched", [])
        if fetched:
            return float(fetched[0].get("ltp") or 0) or None
    except Exception as e:
        logger.debug(f"getMarketData failed ({e}); trying ltpData...")
    # Fallback to ltpData
    try:
        api_rate_limiter.wait("ltpData")
        r = obj.ltpData(spec["spot_exch"], spec["scrip_name"], spec["token"])
        return float(r["data"]["ltp"])
    except Exception as e:
        logger.error(f"LTP read failed for {index}: {e}")
        return None


def capture_reference_spot(index: str, ref_time: str,
                           poll_seconds: int = 40) -> tuple[float | None, str]:
    """
    Capture the spot at `ref_time` (e.g. '09:07', the pre-open window).

    Strategy:
      * Wait until ref_time, then poll the live quote for up to
        `poll_seconds`, watching for the value to MOVE (a moving value =
        a live pre-open tick rather than a stale previous close).
      * Returns (spot, source) where source is 'live_tick',
        'first_value', or 'unavailable'.

    NOTE: pre-open index dissemination is not guaranteed on the broker
    feed; if no fresh tick arrives we return the first value we saw and
    log the source so you always know what fed the ATM selection.
    """
    hh, mm = map(int, ref_time.split(":"))
    target = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    now = datetime.now()
    if now < target:
        wait = (target - now).total_seconds()
        logger.info(f"Waiting {wait:.0f}s until {ref_time} for spot snapshot...")
        time.sleep(max(0, wait))

    first_val = None
    last_val = None
    deadline = time.time() + poll_seconds
    while time.time() < deadline:
        v = _quote_ltp(index)
        if v:
            if first_val is None:
                first_val = v
                logger.info(f"{index} first quote at ~{ref_time}: {v:.2f}")
            if last_val is not None and abs(v - last_val) > 0.001:
                logger.info(f"{index} live tick detected at ~{ref_time}: {v:.2f}")
                return v, "live_tick"
            last_val = v
        time.sleep(1.5)

    if first_val is not None:
        logger.warning(f"{index}: no fresh pre-open tick; using first value "
                       f"{first_val:.2f}. Verify against your terminal.")
        return first_val, "first_value"
    logger.error(f"{index}: reference spot unavailable at {ref_time}.")
    return None, "unavailable"


def round_to_atm(spot: float, index: str) -> int:
    step = config.INDEX_SPECS[index]["step"]
    return int(round(spot / step) * step)


# ======================================================================
# ATM instrument resolution
# ======================================================================
def resolve_atm_instruments(index: str, spot: float) -> dict:
    """
    Return {'CE': {...}, 'PE': {...}} dicts each with:
        symbol, token, exchange, strike, lotsize, expiry
    for the ATM strike of the nearest expiry.
    """
    df = _load_options(index)
    exp = nearest_expiry(df)
    df = df[df["expiry_dt"] == exp]
    atm = round_to_atm(spot, index)

    spec = config.INDEX_SPECS[index]
    out = {}
    for opt in ("CE", "PE"):
        leg = df[(df["opt"] == opt)]
        if leg.empty:
            raise RuntimeError(f"No {opt} rows for {index} {exp.date()}")
        # choose strike closest to ATM (exact match expected)
        leg = leg.assign(dist=(leg["strike_val"] - atm).abs())
        row = leg.sort_values("dist").iloc[0]
        out[opt] = {
            "symbol": str(row["symbol"]),
            "token": str(row["token"]),
            "exchange": spec["opt_exch"],
            "strike": int(row["strike_val"]),
            "lotsize": int(row["lotsize"]),
            "expiry": exp.strftime("%d%b%Y").upper(),
        }
        logger.info(f"{index} ATM {opt}: {out[opt]['symbol']} "
                    f"(strike {out[opt]['strike']}, token {out[opt]['token']}, "
                    f"lot {out[opt]['lotsize']})")
    return out


# ======================================================================
# Candle fetching + resampling
# ======================================================================
_ANGEL_COLS = ["datetime", "open", "high", "low", "close", "volume"]


def _fetch_native(token: str, exchange: str, interval: str,
                  from_dt: datetime, to_dt: datetime) -> pd.DataFrame:
    obj = config.SMART
    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }
    # Backoff schedule (seconds). Angel's getCandleData has a per-second limit
    # AND a 180/minute cap that is shared across the whole client code, so a
    # rate-limit denial may need a multi-second wait to clear. Jitter avoids
    # two legs retrying in lockstep.
    backoff = [2, 5, 10, 20, 30]
    max_tries = len(backoff) + 1

    def _is_rate_limit(text: str) -> bool:
        t = text.lower()
        return ("exceeding access rate" in t or "access denied" in t
                or "access rate" in t or "rate" in t and "exceed" in t)

    for attempt in range(max_tries):
        try:
            api_rate_limiter.wait("getCandleData")
            resp = obj.getCandleData(params)
            if not (isinstance(resp, dict) and resp.get("status")):
                msg = str(resp)
                wait = backoff[min(attempt, len(backoff) - 1)]
                if _is_rate_limit(msg):
                    logger.warning(f"Rate limited on candles ({token}); "
                                   f"backing off {wait}s "
                                   f"(try {attempt+1}/{max_tries})")
                else:
                    logger.warning(f"Candle API error ({token}): {resp}")
                time.sleep(wait + random.uniform(0, 1.0))
                continue
            rows = resp.get("data") or []
            if not rows:
                return pd.DataFrame(columns=_ANGEL_COLS)
            df = pd.DataFrame(rows, columns=_ANGEL_COLS)
            df["datetime"] = pd.to_datetime(df["datetime"])
            for c in ("open", "high", "low", "close", "volume"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            return df
        except Exception as e:
            wait = backoff[min(attempt, len(backoff) - 1)]
            if _is_rate_limit(str(e)):
                logger.warning(f"Rate limited on candles ({token}); backing "
                               f"off {wait}s (try {attempt+1}/{max_tries})")
            else:
                logger.error(f"getCandleData error ({token}) attempt "
                             f"{attempt+1}: {e}")
            time.sleep(wait + random.uniform(0, 1.0))
    logger.error(f"getCandleData gave up for {token} after {max_tries} tries.")
    return pd.DataFrame(columns=_ANGEL_COLS)


def _resample(df: pd.DataFrame, minutes: int,
              session_start="09:15") -> pd.DataFrame:
    """Resample 1-min/1-hour OHLC to `minutes` buckets, anchored to session."""
    if df.empty:
        return df
    d = df.copy().set_index("datetime").sort_index()
    rule = f"{minutes}min"
    # origin at the session start of the first day keeps buckets aligned
    first_day = d.index[0].normalize()
    sh, sm = map(int, session_start.split(":"))
    origin = first_day + pd.Timedelta(hours=sh, minutes=sm)
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    out = d.resample(rule, origin=origin, label="left", closed="left").agg(agg)
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    return out


def get_option_candles(token: str, exchange: str, timeframe: int,
                       lookback_candles: int = 60) -> pd.DataFrame:
    """
    Return a clean OHLC dataframe (datetime, open, high, low, close, volume)
    for the chosen timeframe, with enough history for the BB warmup.

    timeframe in {1,3,5,10,15,30,60,240}.
    """
    angel = config.TIMEFRAME_TO_ANGEL.get(timeframe)
    if angel is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    now = datetime.now()
    # lookback in calendar terms: candles * minutes, plus slack for off-hours
    minutes_needed = lookback_candles * timeframe
    days_back = max(1, math.ceil(minutes_needed / (60 * 6)) + 1)
    from_dt = (now - timedelta(days=days_back)).replace(
        hour=9, minute=15, second=0, microsecond=0)

    if angel == "RESAMPLE_4H":
        base = _fetch_native(token, exchange, "ONE_HOUR", from_dt, now)
        return _resample(base, 240)
    return _fetch_native(token, exchange, angel, from_dt, now)
