"""
config.py
=========
Central configuration + runtime state holder.

Credentials are NOT hardcoded. They are entered in the GUI at runtime and
pushed into CREDENTIALS here. (For a client-distributed EXE this is the safe
pattern.)
"""

import os
from datetime import datetime

# ----------------------------------------------------------------------
# Angel One credentials (filled at runtime by the GUI)
# ----------------------------------------------------------------------
CREDENTIALS = {
    "client_id": "",
    "api_key": "",
    "mpin": "",
    "totp_secret": "",
}

# ----------------------------------------------------------------------
# Trading mode: 'PAPER' or 'LIVE'
# ----------------------------------------------------------------------
TRADING_MODE = "PAPER"

# ----------------------------------------------------------------------
# Index specifications
#   token      : Angel symbol token for the spot index
#   spot_exch  : exchange for the spot index quote
#   opt_exch   : exchange for the OPTIONS (NFO for NSE, BFO for BSE/Sensex)
#   scrip_name : 'name' column value in the Angel scrip master
#   step       : ATM strike rounding step
# Lot size is read live from the scrip master (it changes) - not hardcoded.
# ----------------------------------------------------------------------
INDEX_SPECS = {
    "NIFTY": {
        "token": "99926000", "spot_exch": "NSE", "opt_exch": "NFO",
        "scrip_name": "NIFTY", "step": 50,
    },
    "BANKNIFTY": {
        "token": "99926009", "spot_exch": "NSE", "opt_exch": "NFO",
        "scrip_name": "BANKNIFTY", "step": 100,
    },
    "SENSEX": {
        "token": "99919000", "spot_exch": "BSE", "opt_exch": "BFO",
        "scrip_name": "SENSEX", "step": 100,
    },
}

# ----------------------------------------------------------------------
# Angel One getCandleData interval map.
# 4-hour is NOT a native Angel interval -> resampled from 1-hour locally.
# ----------------------------------------------------------------------
TIMEFRAME_TO_ANGEL = {
    1:  "ONE_MINUTE",
    3:  "THREE_MINUTE",
    5:  "FIVE_MINUTE",
    10: "TEN_MINUTE",
    15: "FIFTEEN_MINUTE",
    30: "THIRTY_MINUTE",
    60: "ONE_HOUR",     # "1hr"
    240: "RESAMPLE_4H",  # "4hr" -> fetch 1H and resample
}
TIMEFRAME_LABELS = {
    1: "1 min", 3: "3 min", 5: "5 min", 10: "10 min", 15: "15 min",
    30: "30 min", 60: "1 hour", 240: "4 hour",
}

# ----------------------------------------------------------------------
# Default strategy parameters (overridable from the GUI)
# ----------------------------------------------------------------------
STRATEGY = {
    "index": "NIFTY",
    "timeframe": 5,              # minutes; one of TIMEFRAME_TO_ANGEL keys
    "option_type": "BOTH",      # 'CE', 'PE', or 'BOTH'

    "ref_time": "09:07",        # spot snapshot time for ATM selection
    "start_time": "09:15",      # earliest time to act on signals
    "stop_entry_time": "15:00", # no new entries after this
    "square_off_time": "15:15", # force-close everything

    "bb_period": 20,
    "bb_mult": 2.0,

    "lots": 2,                  # MUST be even
    "entry_pct": 0.05,          # 5% above trigger high
    "sl_buffer": 5.0,           # points
    "trail_step": 5.0,          # points
    "rr_target": 2.0,           # 1:2

    "max_trades": 4,            # total entries per day (CE + PE combined)

    "candle_fetch_delay": 5,    # seconds after candle close before fetching
    "place_protective_sl": True,  # also place a hard SL order on exchange (LIVE)
}

# ----------------------------------------------------------------------
# Paper trading
# ----------------------------------------------------------------------
PAPER = {
    "initial_capital": 200000,
    "slippage_pct": 0.5,
}

RETRY = {"max_retries": 3, "retry_delay": 1, "base_delay": 1}

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
def _base_dir():
    """Writable base dir; works for frozen EXE and source runs."""
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = _base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

SCRIP_MASTER_FILE = os.path.join(DATA_DIR, "scrip_master.json")
SCRIP_MASTER_URL = ("https://margincalculator.angelbroking.com/"
                    "OpenAPI_File/files/OpenAPIScripMaster.json")

def log_file():
    return os.path.join(LOG_DIR, f"trader_{datetime.now():%Y%m%d}.log")

def trades_file():
    return os.path.join(LOG_DIR, f"trades_{datetime.now():%Y%m%d}.csv")

# ----------------------------------------------------------------------
# Runtime objects (set during a session)
# ----------------------------------------------------------------------
SMART = None     # SmartConnect object
AUTH_TOKEN = None  # jwt token (for WebSocket auth)
FEED_TOKEN = None  # feed token (for WebSocket auth)
