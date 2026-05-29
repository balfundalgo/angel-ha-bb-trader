"""
engine.py
=========
Orchestrates the whole session in a background thread:

  connect -> scrip master -> capture reference spot (09:07) -> resolve ATM
  CE/PE -> per-timeframe candle loop -> HA+BB -> feed strategy -> execute.

Both CE and PE run as independent LegStrategy instances; `max_trades` is the
combined cap across both legs. Times (start / stop-entry / square-off) are
enforced here.
"""

from __future__ import annotations
import threading
import time
from datetime import datetime

import pandas as pd

import config
from logger import logger
from angel_connection import connection_manager
import angel_data as data
from indicators import add_ha_bollinger
from strategy import LegStrategy, LegConfig, State, ActionType
from order_manager import OrderManager


class TradingEngine:
    def __init__(self, status_cb=None):
        self.status_cb = status_cb or (lambda s: None)
        self._stop = threading.Event()
        self.thread = None
        self.om = OrderManager()
        self.legs = {}                 # 'CE'/'PE' -> LegStrategy
        self.instruments = {}          # 'CE'/'PE' -> instrument dict
        self.last_dt = {}              # 'CE'/'PE' -> last processed candle dt
        self.entry_px = {}             # 'CE'/'PE' -> current entry price
        self.ref_spot = None
        self.atm = None

    # ---------------- lifecycle ----------------
    def start(self):
        if self.thread and self.thread.is_alive():
            logger.warning("Engine already running.")
            return
        self._stop.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        logger.info("Stop requested.")

    # ---------------- main run ----------------
    def _run(self):
        try:
            s = config.STRATEGY
            if config.TRADING_MODE == "LIVE":
                if connection_manager.connect() is None:
                    logger.critical("Cannot start: connection failed.")
                    return
            else:
                # PAPER still needs a live connection for market data
                if connection_manager.connect() is None:
                    logger.critical("Cannot start: market-data connection failed.")
                    return

            if not data.download_scrip_master():
                logger.critical("Scrip master unavailable.")
                return

            index = s["index"]
            # 1) reference spot at 09:07
            self.ref_spot, src = data.capture_reference_spot(index, s["ref_time"])
            if self.ref_spot is None:
                logger.critical("Reference spot unavailable - aborting.")
                return
            self.atm = data.round_to_atm(self.ref_spot, index)
            logger.info(f"{index} reference spot {self.ref_spot:.2f} "
                        f"({src}) -> ATM {self.atm}")

            # 2) resolve ATM instruments
            inst = data.resolve_atm_instruments(index, self.ref_spot)

            legs_wanted = (["CE", "PE"] if s["option_type"] == "BOTH"
                           else [s["option_type"]])
            for leg in legs_wanted:
                inst[leg]["leg"] = leg
                self.instruments[leg] = inst[leg]
                cfg = LegConfig(
                    leg=leg, lots=s["lots"], lot_size=inst[leg]["lotsize"],
                    entry_pct=s["entry_pct"], sl_buffer=s["sl_buffer"],
                    trail_step=s["trail_step"], rr_target=s["rr_target"],
                )
                if cfg.lots % 2 != 0:
                    logger.warning(f"{leg}: lots must be even; got {cfg.lots}.")
                self.legs[leg] = LegStrategy(cfg)
                self.last_dt[leg] = None

            # 3) wait for start time, then candle loop
            self._wait_until(s["start_time"])
            self._candle_loop()

        except Exception as e:
            logger.exception(f"Engine crashed: {e}")
        finally:
            self._push_status()
            logger.info("Engine stopped. Summary: %s", self.om.summary())

    # ---------------- candle loop ----------------
    def _candle_loop(self):
        s = config.STRATEGY
        tf = s["timeframe"]
        delay = s["candle_fetch_delay"]

        while not self._stop.is_set():
            now = datetime.now()

            # square-off
            if self._past(s["square_off_time"]):
                self._square_off_all("EOD square-off")
                break

            self._sleep_to_next_candle(tf, delay)
            if self._stop.is_set():
                break

            combined_trades = sum(l.trades_taken for l in self.legs.values())
            block_entries = (combined_trades >= s["max_trades"]
                             or self._past(s["stop_entry_time"]))

            for leg, strat in self.legs.items():
                inst = self.instruments[leg]
                df = data.get_option_candles(
                    inst["token"], inst["exchange"], tf,
                    lookback_candles=s["bb_period"] + 40)
                if df is None or df.empty or len(df) < 2:
                    continue
                ha = add_ha_bollinger(df, s["bb_period"], s["bb_mult"])
                # last CLOSED candle = second to last row (last row may be forming)
                closed = ha.iloc[-2]
                cdt = closed.get("datetime")
                if self.last_dt[leg] is not None and cdt == self.last_dt[leg]:
                    continue  # already processed
                self.last_dt[leg] = cdt

                row = {
                    "ha_open": float(closed["ha_open"]),
                    "ha_high": float(closed["ha_high"]),
                    "ha_low": float(closed["ha_low"]),
                    "ha_close": float(closed["ha_close"]),
                    "ha_green": bool(closed["ha_green"]),
                    "bb_upper": float(closed["bb_upper"]) if pd.notna(closed["bb_upper"]) else None,
                    "bb_lower": float(closed["bb_lower"]) if pd.notna(closed["bb_lower"]) else None,
                }
                actions = strat.process_candle(row, block_entries)
                self._execute(leg, actions)

            self._push_status()

    # ---------------- action execution ----------------
    def _execute(self, leg, actions):
        inst = self.instruments[leg]
        strat = self.legs[leg]
        for a in actions:
            if a.type == ActionType.ENTER:
                px = self.om.buy(inst, a.qty, a.price, a.reason)
                self.entry_px[leg] = px
                self.om.place_protective_sl(inst, a.qty, strat.stop_loss)
            elif a.type == ActionType.BOOK_HALF:
                self.om.sell(inst, a.qty, a.price, a.reason,
                             self.entry_px.get(leg, a.price))
            elif a.type == ActionType.MODIFY_SL:
                self.om.place_protective_sl(inst, a.qty, a.price)
            elif a.type == ActionType.EXIT_ALL:
                self.om.sell(inst, a.qty, a.price, a.reason,
                             self.entry_px.get(leg, a.price))
                self.entry_px.pop(leg, None)
            elif a.type == ActionType.CANCEL:
                logger.info(f"[{leg}] {a.reason}")
            elif a.type == ActionType.INFO:
                logger.info(f"[{leg}] {a.reason}")

    def _square_off_all(self, reason):
        for leg, strat in self.legs.items():
            if strat.state in (State.IN_FULL, State.IN_RUNNER):
                qty = (strat.cfg.half_qty if strat.state == State.IN_RUNNER
                       else strat.cfg.total_qty)
                inst = self.instruments[leg]
                df = data.get_option_candles(inst["token"], inst["exchange"],
                                             config.STRATEGY["timeframe"], 5)
                px = float(df.iloc[-1]["close"]) if df is not None and not df.empty else 0.0
                self.om.sell(inst, qty, px, reason, self.entry_px.get(leg, px))
                strat._close_trade()

    # ---------------- timing helpers ----------------
    def _now_hm(self):
        return datetime.now().strftime("%H:%M")

    def _past(self, hhmm):
        return self._now_hm() >= hhmm

    def _wait_until(self, hhmm):
        hh, mm = map(int, hhmm.split(":"))
        tgt = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        while datetime.now() < tgt and not self._stop.is_set():
            time.sleep(min(5, (tgt - datetime.now()).total_seconds()))

    def _sleep_to_next_candle(self, tf_min, delay):
        """Sleep until the next candle boundary + delay seconds."""
        now = datetime.now()
        # minutes since session start 09:15
        anchor = now.replace(hour=9, minute=15, second=0, microsecond=0)
        elapsed = (now - anchor).total_seconds()
        period = tf_min * 60
        next_boundary = anchor.timestamp() + (int(elapsed // period) + 1) * period
        wake = next_boundary + delay
        while time.time() < wake and not self._stop.is_set():
            time.sleep(min(2, wake - time.time()))

    # ---------------- status push ----------------
    def _push_status(self):
        legs_info = {}
        for leg, strat in self.legs.items():
            legs_info[leg] = {
                "state": strat.state.value,
                "symbol": self.instruments[leg]["symbol"],
                "sl": strat.stop_loss,
                "entry": strat.entry_price,
                "t1": strat.t1_level,
                "trades": strat.trades_taken,
            }
        self.status_cb({
            "mode": config.TRADING_MODE,
            "ref_spot": self.ref_spot,
            "atm": self.atm,
            "legs": legs_info,
            "pnl": self.om.summary()["realized_pnl"],
        })
