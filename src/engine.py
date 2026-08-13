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
import traceback
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
from candle_builder import CandleBuilder
from angel_websocket import WebSocketFeed


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
        self.open_qty = {}             # 'CE'/'PE' -> qty the ENGINE believes open
        self.builders = {}             # 'CE'/'PE' -> CandleBuilder
        self.token_to_leg = {}         # token -> 'CE'/'PE'
        self.locks = {}                # 'CE'/'PE' -> threading.Lock (candle vs tick)
        self.feed = None               # WebSocketFeed
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
            self.feed = WebSocketFeed(on_tick=self._on_tick)
            for leg in legs_wanted:
                inst[leg]["leg"] = leg
                self.instruments[leg] = inst[leg]
                cfg = LegConfig(
                    leg=leg, lots=s["lots"], lot_size=inst[leg]["lotsize"],
                    entry_pct=s["entry_pct"], sl_buffer=s["sl_buffer"],
                    trail_step=s["trail_step"], book_half_points=s["book_half_points"],
                )
                self.legs[leg] = LegStrategy(cfg)
                self.locks[leg] = threading.Lock()
                self.last_dt[leg] = None
                logger.info(f"{leg}: {cfg.lots} lot(s) x2 x {cfg.lot_size} = "
                            f"{cfg.total_qty} qty (half {cfg.half_qty}); "
                            f"book-half at entry+{cfg.book_half_points:g}pts, "
                            f"then open-ended trail ({cfg.trail_step:g}pt steps)")

                # one-time historical fetch to SEED the BB warmup
                builder = CandleBuilder(s["timeframe"], s["start_time"])
                hist = data.get_option_candles(
                    inst[leg]["token"], inst[leg]["exchange"], s["timeframe"],
                    lookback_candles=s["bb_period"] + 40)
                if hist is not None and not hist.empty:
                    # exclude the still-forming last candle from the seed
                    builder.seed(hist.iloc[:-1] if len(hist) > 1 else hist)
                    logger.info(f"{leg}: seeded {builder.n_completed()} "
                                f"historical candles for BB warmup.")
                else:
                    logger.warning(f"{leg}: no historical seed (will warm up "
                                   f"from live ticks).")
                self.builders[leg] = builder
                self.token_to_leg[str(inst[leg]["token"])] = leg
                self.feed.add_token(inst[leg]["token"], inst[leg]["exchange"])

            # start the live feed (no more getCandleData after this)
            self.feed.start()

            # Broker reconciler: auto-correct app state if the broker closes a
            # position the app still thinks is open (e.g. an exchange SL fires
            # while the feed is starved). LIVE only; strategy logic untouched.
            if config.TRADING_MODE == "LIVE":
                threading.Thread(target=self._reconcile_loop, daemon=True).start()

            # 3) wait for start time, then candle loop
            self._wait_until(s["start_time"])
            self._candle_loop()

        except Exception as e:
            logger.exception(f"Engine crashed: {e}")
        finally:
            if self.feed:
                self.feed.stop()
            self._push_status()
            logger.info("Engine stopped. Summary: %s", self.om.summary())

    # ---------------- tick routing (all execution happens here) ----------
    def _on_tick(self, token, ltp, volume, ts):
        leg = self.token_to_leg.get(str(token))
        if not leg or leg not in self.builders:
            return
        self.builders[leg].update(ltp, volume, ts)

        strat = self.legs.get(leg)
        if strat is None:
            return
        # only the execution states care about ticks
        from strategy import State as _St
        if strat.state not in (_St.ARMED, _St.IN_FULL, _St.IN_RUNNER):
            return
        lock = self.locks.get(leg)
        if lock is None or not lock.acquire(blocking=False):
            return  # candle thread holds it for this leg; next tick will retry
        try:
            actions = strat.process_tick(ltp, self._entries_blocked())
            if actions:
                self._execute(leg, actions)
                self._push_status()
        finally:
            lock.release()

    def _entries_blocked(self):
        s = config.STRATEGY
        combined = sum(l.trades_taken for l in self.legs.values())
        return combined >= s["max_trades"] or self._past(s["stop_entry_time"])

    # ---------------- broker reconciler (read-only auto-correct) ----------
    def _reconcile_loop(self):
        interval = getattr(config, "RECONCILE_INTERVAL", 20)
        while not self._stop.is_set():
            # sleep first so positions have time to exist after entry
            for _ in range(int(interval)):
                if self._stop.is_set():
                    return
                time.sleep(1)
            try:
                self._reconcile_once()
            except Exception as e:
                logger.error(f"Reconciler cycle error: {e}")

    def _reconcile_once(self):
        book = self.om.fetch_order_book()          # 1 API call
        positions = self.om.fetch_positions()      # 1 API call (best-effort)

        for leg, strat in self.legs.items():
            inst = self.instruments.get(leg)
            if not inst:
                continue
            sym = inst["symbol"]
            app_qty = self.open_qty.get(leg, 0)     # what the ENGINE believes open
            sl_id = self.om.sl_orders.get(sym)

            # (1) Did the resting exchange SL TRIGGER and fill? That is our
            #     stop exit. Record it and flatten the app's view.
            if sl_id:
                status, avg, fq = self.om.order_fill_detail(sl_id, book)
                if status in ("complete", "filled"):
                    lock = self.locks.get(leg)
                    if lock and lock.acquire(timeout=5):
                        try:
                            qty = fq or app_qty or strat.cfg.total_qty
                            entry_px = self.entry_px.get(leg, avg)
                            self.om.sl_orders.pop(sym, None)   # it's done
                            self.om.reconcile_close(inst, qty, avg or entry_px,
                                                    entry_px,
                                                    "exchange SL triggered")
                            self.open_qty[leg] = 0
                            self.entry_px.pop(leg, None)
                            if strat.state in (State.IN_FULL, State.IN_RUNNER):
                                strat._close_trade()
                            self._push_status()
                        finally:
                            lock.release()
                    continue

            # Determine broker net qty on this symbol (best-effort).
            broker_qty = positions.get(sym) if positions is not None else None

            # (2) App believes OPEN but broker is FLAT (closed elsewhere:
            #     manual square-off, or an SL we lost track of) -> book it.
            if app_qty > 0 and broker_qty == 0:
                lock = self.locks.get(leg)
                if lock and lock.acquire(timeout=5):
                    try:
                        exit_px = self._reconcile_exit_price(sym, book)
                        entry_px = self.entry_px.get(leg, exit_px)
                        self.om.reconcile_close(inst, app_qty, exit_px, entry_px,
                                                "auto-reconcile (broker flat)")
                        self.open_qty[leg] = 0
                        self.entry_px.pop(leg, None)
                        if strat.state in (State.IN_FULL, State.IN_RUNNER):
                            strat._close_trade()
                        self._push_status()
                    finally:
                        lock.release()
                continue

            # (3) App believes FLAT but broker HOLDS -> orphan. Try to protect
            #     it with a fresh closing SL (nets cleanly, no short margin),
            #     and alert loudly. We do NOT fire a naked market sell (that is
            #     what Angel rejects for margin).
            if app_qty == 0 and broker_qty and broker_qty > 0:
                if not sl_id:
                    logger.critical(f"ORPHAN: broker holds {broker_qty} {sym} with "
                                    f"NO resting SL. Placing a protective SL and "
                                    f"alerting — CHECK TERMINAL.")
                    # re-protect the orphan with a closing stop a bit below LTP
                    lp = self._reconcile_exit_price(sym, book)
                    if lp:
                        self.om.place_protective_sl(inst, broker_qty,
                                                    round(lp * 0.98, 2))
                else:
                    logger.critical(f"ORPHAN: broker holds {broker_qty} {sym} "
                                    f"(a protective SL is resting). CHECK TERMINAL.")

    def _reconcile_exit_price(self, sym, book):
        """Best exit price for a broker-closed position: the triggered SL's
        fill, else the latest completed SELL on the symbol, else last tick."""
        # tracked SL fill
        sl_id = self.om.sl_orders.get(sym)
        row = book.get(str(sl_id)) if sl_id else None
        if row and str(row.get("status", "")).lower() in ("complete", "filled"):
            px = float(row.get("averageprice") or row.get("price") or 0) or 0.0
            if px:
                return px
        # latest completed SELL on this symbol
        best = None
        for r in book.values():
            if (str(r.get("tradingsymbol")) == sym
                    and str(r.get("transactiontype")).upper() == "SELL"
                    and str(r.get("status", "")).lower() in ("complete", "filled")):
                px = float(r.get("averageprice") or r.get("price") or 0) or 0.0
                if px:
                    best = px
        if best:
            return best
        # last resort: last websocket tick
        for lg, i in self.instruments.items():
            if i["symbol"] == sym:
                lp = self.builders[lg].last_price
                if lp:
                    return lp
        return 0.0

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

            for leg, strat in self.legs.items():
                try:
                    # candles come from the LOCAL builder (WebSocket-driven),
                    # so no getCandleData calls happen inside the loop.
                    df = self.builders[leg].completed_df()
                    if df is None or df.empty or len(df) < 2:
                        continue
                    ha = add_ha_bollinger(df, s["bb_period"], s["bb_mult"])
                    # last row here is the most recently CLOSED candle (the
                    # forming candle is not included in completed_df()).
                    closed = ha.iloc[-1]
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
                    # candle close only detects signals / arms the setup;
                    # all entries and exits run on ticks (process_tick).
                    with self.locks[leg]:
                        actions = strat.process_candle(row)
                        self._execute(leg, actions)
                except Exception as e:
                    logger.error(f"[{leg}] cycle error (continuing): {e}\n"
                                 f"{traceback.format_exc()}")

            self._push_status()

    # ---------------- action execution ----------------
    def _execute(self, leg, actions):
        inst = self.instruments[leg]
        strat = self.legs[leg]
        for a in actions:
            if a.type == ActionType.ENTER:
                px = self.om.buy(inst, a.qty, a.price, a.reason)
                self.entry_px[leg] = px
                self.open_qty[leg] = a.qty          # engine believes this open
                self.om.place_protective_sl(inst, a.qty, strat.stop_loss)
            elif a.type == ActionType.BOOK_HALF:
                # partial PROFIT exit (sell half, stay long half) = reducing order.
                px = self.om.sell(inst, a.qty, a.price, a.reason,
                                  self.entry_px.get(leg, a.price))
                if px is not None:                  # only reduce if it filled
                    self.open_qty[leg] = max(0, self.open_qty.get(leg, a.qty * 2) - a.qty)
            elif a.type == ActionType.MODIFY_SL:
                # move the single resting exchange SL up the trail
                self.om.place_protective_sl(inst, a.qty, a.price)
            elif a.type == ActionType.EXIT_ALL:
                # Every strategy-driven full exit is a STOP hit (initial /
                # breakeven / trailing). We DO NOT fire a naked market sell for
                # it — that is what Angel priced as a fresh short (~2L margin)
                # and rejected. Instead we let the RESTING exchange SL trigger:
                # it already sits at this level and is recognised as a closing
                # order. The reconciler records the fill when it triggers.
                logger.info(f"[{leg}] stop exit -> delegated to resting exchange "
                            f"SL ({a.reason}); awaiting exchange fill")
                # keep entry_px[leg] until the reconciler books the real fill
            elif a.type == ActionType.CANCEL:
                logger.info(f"[{leg}] {a.reason}")
            elif a.type == ActionType.INFO:
                logger.info(f"[{leg}] {a.reason}")

    def _square_off_all(self, reason):
        for leg, strat in self.legs.items():
            with self.locks[leg]:
                qty = self.open_qty.get(leg, 0)
                if qty <= 0:
                    continue
                inst = self.instruments[leg]
                last = self.builders[leg].last_price or self.entry_px.get(leg, 0.0)
                sym = inst["symbol"]
                if self.om.sl_orders.get(sym) and last:
                    # flatten NOW: pull the SL trigger just ABOVE market so the
                    # exchange fires it immediately (a sell-stop triggers when
                    # LTP <= trigger). Below-market would wait for a downtick.
                    self.om.place_protective_sl(inst, qty, round(last * 1.001, 2))
                    logger.info(f"[{leg}] square-off: resting SL pulled above market "
                                f"to flatten {qty} {sym} now")
                else:
                    self.om.sell(inst, qty, last, reason, self.entry_px.get(leg, last))
                # engine view / strategy view left for the reconciler to confirm
                if strat.state in (State.IN_FULL, State.IN_RUNNER):
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
                "book_half": strat.book_half_level,
                "trades": strat.trades_taken,
            }
        self.status_cb({
            "mode": config.TRADING_MODE,
            "ref_spot": self.ref_spot,
            "atm": self.atm,
            "legs": legs_info,
            "pnl": self.om.summary()["realized_pnl"],
        })

    # ---------------- read-only live snapshot (for GUI monitor) ----------
    # Pure read of existing values; does NOT alter any trading logic.
    def live_snapshot(self):
        realized = self.om.realized if self.om else 0.0
        unrealized_total = 0.0
        legs = []
        for leg, strat in self.legs.items():
            ltp = self.builders[leg].last_price if leg in self.builders else None
            # entry: actual fill price once in a trade; planned entry level
            # while ARMED/waiting (so the panel is never blank mid-setup).
            entry = strat.entry_price if strat.entry_price is not None else strat.entry_level
            if strat.state == State.IN_FULL:
                qty = strat.cfg.total_qty
            elif strat.state == State.IN_RUNNER:
                qty = strat.cfg.half_qty
            else:
                qty = 0
            unreal = 0.0
            if qty and strat.entry_price and ltp:
                unreal = (ltp - strat.entry_price) * qty   # long option position
                unrealized_total += unreal
            legs.append({
                "leg": leg,
                "symbol": self.instruments[leg]["symbol"] if leg in self.instruments else "",
                "state": strat.state.value,
                "entry": entry,
                "ltp": ltp,
                "qty": qty,
                "sl": strat.stop_loss,
                "book_half": strat.book_half_level,
                "unrealized": unreal,
            })
        return {
            "mode": config.TRADING_MODE,
            "atm": self.atm,
            "ref_spot": self.ref_spot,
            "realized": realized,
            "unrealized": unrealized_total,
            "total": realized + unrealized_total,
            "legs": legs,
            "running": bool(self.thread and self.thread.is_alive()),
        }
