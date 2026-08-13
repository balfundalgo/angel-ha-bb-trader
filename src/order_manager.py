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
        if config.TRADING_MODE == "LIVE":
            # cancel the resting SL first so this reducing sell nets cleanly
            self._cancel_protective_sl(inst)
            oid = self._live_market(inst, qty, "SELL")
            ok = self._verify_or_warn(oid, f"SELL {qty} {inst['symbol']} ({reason})")
            if not ok:
                # DO NOT record a phantom exit. Keep books honest; the position
                # is still open at the broker and the reconciler will handle it.
                logger.critical(f"!!! EXIT REJECTED for {inst['symbol']} "
                                f"({reason}) — NOT recorded. Position still OPEN; "
                                f"reconciler/exchange-SL will resolve. !!!")
                return None
            px = self._fill_price(ref_price, side="SELL")
        else:
            px = self._fill_price(ref_price, side="SELL")
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
        except Exception as e:
            logger.error(f"Modify SL {oid} raised: {e}; cancel-and-replace")
            return self._replace_stoploss(inst, qty, trigger)

        # VERIFY the modify actually took at the exchange (do not trust that
        # the SDK call merely returned). Read the order back.
        if self._verify_sl_trigger(oid, trigger):
            logger.info(f"Modified SL {oid} -> trigger {trigger} qty {qty} (verified)")
            return oid
        logger.warning(f"Modify SL {oid} did NOT take at exchange "
                       f"(wanted {trigger}); cancel-and-replace")
        return self._replace_stoploss(inst, qty, trigger)

    def _replace_stoploss(self, inst, qty, trigger):
        """Cancel the tracked SL and place a fresh one at `trigger`; verify it
        rests. Guarantees the exchange trigger is where we intend."""
        self._cancel_protective_sl(inst)
        oid = self._live_stoploss(inst, qty, trigger)
        if oid and self._verify_sl_trigger(oid, trigger):
            self.sl_orders[inst["symbol"]] = oid
            logger.info(f"Replaced SL -> new id {oid} trigger {trigger} (verified)")
            return oid
        if oid:
            self.sl_orders[inst["symbol"]] = oid
            logger.critical(f"Replacement SL {oid} placed but NOT verified at "
                            f"trigger {trigger} — CHECK TERMINAL.")
        else:
            logger.critical(f"FAILED to place replacement SL at {trigger} for "
                            f"{inst['symbol']} — position may be UNPROTECTED.")
        return oid

    def _verify_sl_trigger(self, oid, want_trigger, tries=3):
        """Confirm order `oid` is a live resting SL whose exchange trigger
        matches want_trigger. Returns False if it's gone or mismatched."""
        for _ in range(tries):
            book = self.fetch_order_book()
            row = book.get(str(oid))
            if row:
                status = str(row.get("status", "")).lower()
                if status in ("cancelled", "rejected", "complete", "filled"):
                    return False
                try:
                    got = float(row.get("triggerprice") or 0)
                except (TypeError, ValueError):
                    got = 0.0
                if abs(got - round(float(want_trigger), 2)) < 0.06:
                    return True
            time.sleep(0.5)
        return False

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

    def fetch_order_book(self):
        """Return {orderid: row} from Angel, or {} on failure. One API call."""
        try:
            api_rate_limiter.wait("orderBook")
            book = config.SMART.orderBook()
            out = {}
            for row in (book or {}).get("data", []) or []:
                oid = str(row.get("orderid"))
                out[oid] = row
            return out
        except Exception as e:
            logger.error(f"orderBook fetch failed: {e}")
            return {}

    def fetch_positions(self):
        """Return {symbol: netqty} from Angel (best-effort across SDK method
        names), or None if positions can't be read."""
        for meth in ("position", "getPosition", "positionData"):
            fn = getattr(config.SMART, meth, None)
            if not callable(fn):
                continue
            try:
                api_rate_limiter.wait("position")
                resp = fn()
                data = (resp or {}).get("data") or []
                out = {}
                for row in data:
                    sym = str(row.get("tradingsymbol") or row.get("symbolname") or "")
                    try:
                        net = int(float(row.get("netqty", 0) or 0))
                    except (TypeError, ValueError):
                        net = 0
                    if sym:
                        out[sym] = net
                return out
            except Exception as e:
                logger.error(f"positions fetch via {meth} failed: {e}")
                return None
        return None

    def order_fill(self, order_id):
        """Return (status_lower, avg_price) for an order from the order book."""
        status, avg, _ = self.order_fill_detail(order_id)
        return status, avg

    def order_fill_detail(self, order_id, book=None):
        """Return (status_lower, avg_price, filled_qty) for an order."""
        if book is None:
            book = self.fetch_order_book()
        row = book.get(str(order_id))
        if not row:
            return None, 0.0, 0
        status = str(row.get("status", "")).lower()
        try:
            avg = float(row.get("averageprice") or row.get("price") or 0) or 0.0
        except (TypeError, ValueError):
            avg = 0.0
        try:
            fq = int(float(row.get("filledshares") or row.get("quantity") or 0) or 0)
        except (TypeError, ValueError):
            fq = 0
        return status, avg, fq

    def reconcile_close(self, inst, qty, exit_price, entry_price, reason):
        """Record an exit the BROKER already executed (no order is placed).
        Cancels any lingering resting SL so it can't later open a short."""
        self._cancel_protective_sl(inst)          # kill any naked resting SL
        pnl = (exit_price - entry_price) * qty
        self.realized += pnl
        self._record(inst.get("leg", "?"), inst["symbol"], "SELL", qty,
                     exit_price, reason, pnl)
        logger.critical(f"RECONCILED {inst['symbol']}: broker closed {qty} @ "
                        f"{exit_price:.2f} (app had it open). P&L {pnl:+.2f} "
                        f"corrected. [{reason}]")

    def summary(self):
        return {"realized_pnl": round(self.realized, 2),
                "fills": len(self.ledger)}
