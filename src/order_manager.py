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
        self.sl_orders = {}        # symbol -> resting protective-SL order_id (LIVE)
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
            oid = self._live_market(inst, qty, "BUY")
            self._verify_or_warn(oid, f"BUY {qty} {inst['symbol']}")
        self._record(inst.get("leg", "?"), inst["symbol"], "BUY", qty, px, reason)
        return px

    def sell(self, inst: dict, qty: int, ref_price: float, reason: str,
             entry_price: float) -> float:
        px = self._fill_price(ref_price, side="SELL")
        if config.TRADING_MODE == "LIVE":
            # CRITICAL: cancel the resting protective SL BEFORE any market sell,
            # so total sell qty can never exceed the position (the cause of the
            # mass rejections). Then verify the exit actually filled.
            self._cancel_protective_sl(inst)
            oid = self._live_market(inst, qty, "SELL")
            ok = self._verify_or_warn(oid, f"SELL {qty} {inst['symbol']} ({reason})")
            if not ok:
                logger.critical(f"!!! EXIT MAY HAVE FAILED for {inst['symbol']} "
                                f"({reason}). CHECK THE TERMINAL — position may "
                                f"still be OPEN. !!!")
        pnl = (px - entry_price) * qty
        self.realized += pnl
        self._record(inst.get("leg", "?"), inst["symbol"], "SELL", qty, px,
                     reason, pnl)
        return px

    def place_protective_sl(self, inst: dict, qty: int, trigger: float):
        """Ensure exactly ONE protective SL per symbol: modify if one exists,
        otherwise place a new one. Never stacks."""
        if config.TRADING_MODE != "LIVE" or not config.STRATEGY["place_protective_sl"]:
            return None
        sym = inst["symbol"]
        if self.sl_orders.get(sym):
            return self._modify_stoploss(inst, qty, trigger, self.sl_orders[sym])
        oid = self._live_stoploss(inst, qty, trigger)
        if oid:
            self.sl_orders[sym] = oid
        return oid

    # ---------------- internals ----------------
    def _fill_price(self, ref_price: float, side: str) -> float:
        slip = config.PAPER["slippage_pct"] / 100.0 if config.TRADING_MODE == "PAPER" else 0.0
        # buying fills a touch higher, selling a touch lower
        return ref_price * (1 + slip) if side == "BUY" else ref_price * (1 - slip)

    def _cancel_protective_sl(self, inst):
        sym = inst["symbol"]
        oid = self.sl_orders.get(sym)
        if not oid:
            return
        try:
            api_rate_limiter.wait("cancelOrder")
            config.SMART.cancelOrder(oid, "STOPLOSS")
            logger.info(f"Cancelled resting SL {oid} for {sym}")
        except Exception as e:
            logger.error(f"Cancel SL {oid} failed for {sym}: {e}")
        finally:
            self.sl_orders.pop(sym, None)

    def _modify_stoploss(self, inst, qty, trigger, oid):
        trigger = round(float(trigger), 2)
        limit = round(trigger - 1.0, 2)
        params = {
            "variety": "STOPLOSS", "orderid": str(oid),
            "tradingsymbol": inst["symbol"], "symboltoken": inst["token"],
            "transactiontype": "SELL", "exchange": inst["exchange"],
            "ordertype": "STOPLOSS_LIMIT", "producttype": "INTRADAY",
            "duration": "DAY", "price": str(limit),
            "triggerprice": str(trigger), "quantity": str(qty),
        }
        try:
            api_rate_limiter.wait("modifyOrder")
            config.SMART.modifyOrder(params)
            logger.info(f"Modified SL {oid} -> trigger {trigger} qty {qty}")
            return oid
        except Exception as e:
            logger.error(f"Modify SL {oid} failed: {e}; replacing instead")
            # fallback: cancel + place fresh so we never lose protection silently
            self._cancel_protective_sl(inst)
            new = self._live_stoploss(inst, qty, trigger)
            if new:
                self.sl_orders[inst["symbol"]] = new
            return new

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
                    logger.info(f"Order submitted [{label}] id={oid}")
                    return oid
                logger.error(f"Order returned bad id [{label}]: {oid}")
            except Exception as e:
                logger.error(f"Order error [{label}] attempt {attempt+1}: {e}")
            time.sleep([2, 5, 10][min(attempt, 2)])
        logger.critical(f"Order FAILED after retries [{label}]")
        return None

    def _order_status(self, order_id):
        """Return the Angel order status string (lowercased) or None."""
        try:
            api_rate_limiter.wait("getOrderBook")
            book = config.SMART.orderBook()
            for row in (book or {}).get("data", []) or []:
                if str(row.get("orderid")) == str(order_id):
                    return str(row.get("status", "")).lower(), str(row.get("text", ""))
        except Exception as e:
            logger.error(f"orderBook lookup failed for {order_id}: {e}")
        return None, ""

    def _verify_or_warn(self, order_id, label):
        """Poll the order book to confirm a market order actually filled.
        Returns True if complete; logs the real reason if rejected."""
        if not order_id:
            logger.critical(f"No order id to verify [{label}] — treat as FAILED")
            return False
        for _ in range(6):                      # ~6s of polling
            status, text = self._order_status(order_id)
            if status is None:
                time.sleep(1.0)
                continue
            if status in ("complete", "filled"):
                logger.info(f"FILL CONFIRMED [{label}] id={order_id}")
                return True
            if status in ("rejected", "cancelled"):
                logger.critical(f"ORDER {status.upper()} [{label}] id={order_id} "
                                f"reason: {text}")
                return False
            time.sleep(1.0)                      # open / trigger pending -> wait
        logger.warning(f"Order not confirmed filled within timeout [{label}] "
                       f"id={order_id} — verify in terminal")
        return False

    def summary(self):
        return {"realized_pnl": round(self.realized, 2),
                "fills": len(self.ledger)}
