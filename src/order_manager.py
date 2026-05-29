"""
order_manager.py
================
Executes Action events from the strategy engine in either PAPER or LIVE mode.

LIVE order params follow the reference Angel One order_manager:
  * market entry  -> variety NORMAL, ordertype MARKET, BUY (option buying)
  * exit          -> variety NORMAL, ordertype MARKET, SELL
  * protective SL -> variety STOPLOSS, ordertype STOPLOSS_LIMIT, SELL
    (a hard safety net; the engine remains authoritative on HA candle close)

PAPER mode simulates fills with configurable slippage and records the same
trade ledger so the GUI / CSV are identical across modes.
"""

from __future__ import annotations
import csv
import os
import time
from datetime import datetime

import config
from logger import logger
from api_rate_limiter import api_rate_limiter


class OrderManager:
    def __init__(self):
        self.realized = 0.0
        self.ledger = []           # list of dict rows
        self._ensure_csv()

    # ---------------- ledger ----------------
    def _ensure_csv(self):
        path = config.trades_file()
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(
                    ["time", "mode", "leg", "symbol", "side", "qty",
                     "price", "reason", "realized_pnl"])

    def _record(self, leg, symbol, side, qty, price, reason, pnl=0.0):
        row = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "mode": config.TRADING_MODE, "leg": leg, "symbol": symbol,
            "side": side, "qty": qty, "price": round(price, 2),
            "reason": reason, "realized_pnl": round(pnl, 2),
        }
        self.ledger.append(row)
        with open(config.trades_file(), "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([row[k] for k in
                                    ["time", "mode", "leg", "symbol", "side",
                                     "qty", "price", "reason", "realized_pnl"]])
        logger.info(f"[{config.TRADING_MODE}] {side} {qty} {symbol} @ "
                    f"{price:.2f}  ({reason})")

    # ---------------- public API ----------------
    def buy(self, inst: dict, qty: int, ref_price: float, reason: str) -> float:
        px = self._fill_price(ref_price, side="BUY")
        if config.TRADING_MODE == "LIVE":
            self._live_market(inst, qty, "BUY")
        self._record(inst.get("leg", "?"), inst["symbol"], "BUY", qty, px, reason)
        return px

    def sell(self, inst: dict, qty: int, ref_price: float, reason: str,
             entry_price: float) -> float:
        px = self._fill_price(ref_price, side="SELL")
        if config.TRADING_MODE == "LIVE":
            self._live_market(inst, qty, "SELL")
        pnl = (px - entry_price) * qty
        self.realized += pnl
        self._record(inst.get("leg", "?"), inst["symbol"], "SELL", qty, px,
                     reason, pnl)
        return px

    def place_protective_sl(self, inst: dict, qty: int, trigger: float):
        if config.TRADING_MODE != "LIVE" or not config.STRATEGY["place_protective_sl"]:
            return None
        return self._live_stoploss(inst, qty, trigger)

    # ---------------- internals ----------------
    def _fill_price(self, ref_price: float, side: str) -> float:
        slip = config.PAPER["slippage_pct"] / 100.0 if config.TRADING_MODE == "PAPER" else 0.0
        # buying fills a touch higher, selling a touch lower
        return ref_price * (1 + slip) if side == "BUY" else ref_price * (1 - slip)

    def _live_market(self, inst, qty, side):
        params = {
            "variety": "NORMAL",
            "tradingsymbol": inst["symbol"],
            "symboltoken": inst["token"],
            "transactiontype": side,
            "exchange": inst["exchange"],
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0", "squareoff": "0", "stoploss": "0",
            "quantity": str(qty),
        }
        return self._send(params, f"MARKET {side}")

    def _live_stoploss(self, inst, qty, trigger):
        trigger = round(float(trigger), 2)
        limit = round(trigger - 1.0, 2)  # SELL SL limit a touch below trigger
        params = {
            "variety": "STOPLOSS",
            "tradingsymbol": inst["symbol"],
            "symboltoken": inst["token"],
            "transactiontype": "SELL",
            "exchange": inst["exchange"],
            "ordertype": "STOPLOSS_LIMIT",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": str(limit),
            "triggerprice": str(trigger),
            "quantity": str(qty),
        }
        return self._send(params, f"PROTECTIVE SL @ {trigger}")

    def _send(self, params, label):
        for attempt in range(config.RETRY["max_retries"]):
            try:
                api_rate_limiter.wait("placeOrder")
                oid = config.SMART.placeOrder(params)
                if oid and len(str(oid)) > 4:
                    logger.info(f"Order OK [{label}] id={oid}")
                    return oid
                logger.error(f"Order returned bad id [{label}]: {oid}")
            except Exception as e:
                logger.error(f"Order error [{label}] attempt {attempt+1}: {e}")
            time.sleep([2, 5, 10][min(attempt, 2)])
        logger.critical(f"Order FAILED after retries [{label}]")
        return None

    def summary(self):
        return {"realized_pnl": round(self.realized, 2),
                "fills": len(self.ledger)}
