"""
strategy.py
===========
Per-leg Heiken Ashi + Bollinger Band strategy (long-only option buying).

SPLIT BETWEEN CANDLE CLOSE AND TICKS
------------------------------------
Signal detection runs on HA candle CLOSE (process_candle):
  * Alert   : a RED HA candle closes BELOW the lower BB.
  * Trigger : the FIRST GREEN HA candle after the alert (no band condition).
              On trigger we ARM the setup and lock:
                  entry_level = trigger HA high * (1 + entry_pct)   (default +5%)
                  stop_loss   = alert(red) HA low - sl_buffer

Execution runs on every TICK / live LTP (process_tick):
  * Cancel   : LTP hits SL before entry  -> void the setup.
  * Entry    : LTP crosses entry_level   -> buy immediately.
               qty = lots * 2 * lot_size  (1 lot = 2 units; "half" is exact).
  * Book half: LTP reaches entry + target_points/2 -> sell half,
               move remaining SL to breakeven (entry).
  * Trail    : after book-half, measured FROM the book-half level: for every
               +trail_step points, raise SL by trail_step from breakeven.
  * Target   : LTP reaches entry + target_points -> exit the remaining half.
  * SL exit  : LTP hits the current SL at any time.
  The remaining half exits on whichever comes first: trailing stop or target.

REMOVED vs the old version: the 1:2 R:R target and the upper-Bollinger-band
target. Entries/exits are no longer evaluated on candle close.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import math


class State(Enum):
    IDLE = "IDLE"                 # waiting for red-below-band alert
    WAIT_TRIGGER = "WAIT_TRIGGER" # alert seen, waiting for first green candle
    ARMED = "ARMED"               # trigger set, waiting (ticks) for entry/cancel
    IN_FULL = "IN_FULL"           # filled, full qty, watching book-half/target/SL
    IN_RUNNER = "IN_RUNNER"       # half booked, trailing remaining half


class ActionType(Enum):
    ENTER = "ENTER"
    BOOK_HALF = "BOOK_HALF"
    MODIFY_SL = "MODIFY_SL"
    EXIT_ALL = "EXIT_ALL"
    CANCEL = "CANCEL"
    INFO = "INFO"


@dataclass
class Action:
    type: ActionType
    price: float = 0.0
    qty: int = 0
    reason: str = ""


@dataclass
class LegConfig:
    leg: str                       # "CE" or "PE"
    lots: int = 1                  # 1 lot = 2 units (pairs); any whole number ok
    lot_size: int = 65             # contracts per unit (read from scrip master)
    entry_pct: float = 0.05        # +5% above trigger HA high
    sl_buffer: float = 5.0         # points subtracted from red HA low
    trail_step: float = 5.0        # points per trail step
    target_points: float = 30.0    # FULL target in premium points

    @property
    def total_qty(self) -> int:
        return self.lots * 2 * self.lot_size

    @property
    def half_qty(self) -> int:
        return self.lots * self.lot_size      # exactly half of total_qty


@dataclass
class LegStrategy:
    cfg: LegConfig
    state: State = State.IDLE
    trades_taken: int = 0

    alert_low: Optional[float] = None
    trigger_high: Optional[float] = None
    entry_level: Optional[float] = None
    stop_loss: Optional[float] = None
    entry_price: Optional[float] = None
    book_half_level: Optional[float] = None
    full_target: Optional[float] = None
    peak: float = field(default=0.0)
    half_booked: bool = False

    def _reset_setup(self):
        self.alert_low = None
        self.trigger_high = None
        self.entry_level = None
        self.stop_loss = None
        self.entry_price = None
        self.book_half_level = None
        self.full_target = None
        self.peak = 0.0
        self.half_booked = False

    def _close_trade(self):
        self._reset_setup()
        self.state = State.IDLE

    # ------------------------------------------------------------------
    # CANDLE CLOSE: signal detection / arming only (no entries or exits)
    # ------------------------------------------------------------------
    def process_candle(self, row: dict) -> List[Action]:
        actions: List[Action] = []

        # Only the signal-search states react to candle close.
        if self.state not in (State.IDLE, State.WAIT_TRIGGER):
            return actions

        ha_open = row["ha_open"]
        ha_high = row["ha_high"]
        ha_low = row["ha_low"]
        ha_close = row["ha_close"]
        green = bool(row["ha_green"])
        bb_lower = row.get("bb_lower")

        warmup = (bb_lower is None
                  or (isinstance(bb_lower, float) and math.isnan(bb_lower)))
        if warmup:
            return actions

        if self.state == State.IDLE:
            # alert: red HA candle closes below the lower band
            if (not green) and ha_close < bb_lower:
                self.alert_low = ha_low
                self.state = State.WAIT_TRIGGER
                actions.append(Action(ActionType.INFO, 0, 0,
                                       f"Alert: red HA closed below lower band "
                                       f"({ha_close:.2f} < {bb_lower:.2f})"))
            return actions

        if self.state == State.WAIT_TRIGGER:
            # keep the most recent red-below-band low as the SL reference
            if (not green) and ha_close < bb_lower:
                self.alert_low = ha_low
                return actions
            # trigger: FIRST green HA candle after the alert (no band condition)
            if green:
                self.trigger_high = ha_high
                self.entry_level = round(self.trigger_high * (1 + self.cfg.entry_pct), 2)
                self.stop_loss = round(self.alert_low - self.cfg.sl_buffer, 2)
                self.state = State.ARMED
                actions.append(Action(ActionType.INFO, 0, 0,
                                       f"Trigger green HA. Entry {self.entry_level:.2f}, "
                                       f"SL {self.stop_loss:.2f} (waiting for tick cross)"))
            return actions

        return actions

    # ------------------------------------------------------------------
    # TICK: all execution (entry, cancel, book-half, target, trail, SL)
    # ------------------------------------------------------------------
    def process_tick(self, ltp: float, entries_blocked: bool) -> List[Action]:
        actions: List[Action] = []
        if ltp is None or ltp <= 0:
            return actions

        # ---- ARMED: wait for entry cross or cancel ----
        if self.state == State.ARMED:
            if ltp <= self.stop_loss:
                actions.append(Action(ActionType.CANCEL, 0, 0,
                                       f"Setup cancelled (LTP {ltp:.2f} <= SL "
                                       f"{self.stop_loss:.2f} before entry)"))
                self._reset_setup()
                self.state = State.IDLE
                return actions
            if ltp >= self.entry_level:
                if entries_blocked:
                    actions.append(Action(ActionType.INFO, 0, 0,
                                           "Entry cross but entries blocked - skipped"))
                    self._reset_setup()
                    self.state = State.IDLE
                    return actions
                self.entry_price = self.entry_level
                self.peak = ltp
                self.book_half_level = round(self.entry_price + self.cfg.target_points / 2.0, 2)
                self.full_target = round(self.entry_price + self.cfg.target_points, 2)
                self.trades_taken += 1
                self.state = State.IN_FULL
                actions.append(Action(ActionType.ENTER, self.entry_price,
                                       self.cfg.total_qty,
                                       f"Entry @ {self.entry_price:.2f} | SL {self.stop_loss:.2f} "
                                       f"| book-half {self.book_half_level:.2f} "
                                       f"| target {self.full_target:.2f}"))
            return actions

        # ---- IN_FULL: SL / book-half (then fall through to runner) ----
        if self.state == State.IN_FULL:
            self.peak = max(self.peak, ltp)
            if ltp <= self.stop_loss:
                actions.append(Action(ActionType.EXIT_ALL, self.stop_loss,
                                       self.cfg.total_qty,
                                       f"SL hit @ {self.stop_loss:.2f}"))
                self._close_trade()
                return actions
            if ltp >= self.book_half_level:
                actions.append(Action(ActionType.BOOK_HALF, self.book_half_level,
                                       self.cfg.half_qty,
                                       f"Book half @ {self.book_half_level:.2f} (target/2)"))
                self.half_booked = True
                self.stop_loss = self.entry_price          # SL -> breakeven
                actions.append(Action(ActionType.MODIFY_SL, self.stop_loss,
                                       self.cfg.half_qty, "SL -> breakeven"))
                self.state = State.IN_RUNNER
                # fall through to runner checks on this same tick
            else:
                return actions

        # ---- IN_RUNNER: target / SL / trail on remaining half ----
        if self.state == State.IN_RUNNER:
            self.peak = max(self.peak, ltp)

            if ltp <= self.stop_loss:
                actions.append(Action(ActionType.EXIT_ALL, self.stop_loss,
                                       self.cfg.half_qty,
                                       f"Trailing/breakeven SL hit @ {self.stop_loss:.2f}"))
                self._close_trade()
                return actions

            if ltp >= self.full_target:
                actions.append(Action(ActionType.EXIT_ALL, self.full_target,
                                       self.cfg.half_qty,
                                       f"Target hit @ {self.full_target:.2f}"))
                self._close_trade()
                return actions

            # trail: steps measured FROM the book-half level
            if self.peak > self.book_half_level:
                steps = math.floor((self.peak - self.book_half_level) / self.cfg.trail_step)
                if steps >= 1:
                    new_sl = round(self.entry_price + steps * self.cfg.trail_step, 2)
                    if new_sl > self.stop_loss:
                        self.stop_loss = new_sl
                        actions.append(Action(ActionType.MODIFY_SL, self.stop_loss,
                                               self.cfg.half_qty,
                                               f"Trail SL -> {self.stop_loss:.2f}"))
            return actions

        return actions


# ----------------------------------------------------------------------
# Self-test: candle close arms the setup, then a tick stream executes it
# ----------------------------------------------------------------------
if __name__ == "__main__":

    def candle(o, h, l, c, lo):
        return {"ha_open": o, "ha_high": h, "ha_low": l, "ha_close": c,
                "ha_green": c >= o, "bb_lower": lo}

    cfg = LegConfig(leg="CE", lots=2, lot_size=65, entry_pct=0.05,
                    sl_buffer=5.0, trail_step=5.0, target_points=40.0)
    s = LegStrategy(cfg)

    # 1) red HA closes below band, low=170.55 -> alert
    for a in s.process_candle(candle(180, 182, 170.55, 172, lo=208)):
        print("candle:", a.reason)
    # 2) green HA candle (any) -> trigger. entry=188.10*1.05=197.51, SL=170.55-5=165.55
    for a in s.process_candle(candle(185, 188.10, 184, 187, lo=208)):
        print("candle:", a.reason)
    print("state:", s.state.value, "| entry_level:", s.entry_level, "| SL:", s.stop_loss)
    assert s.state == State.ARMED

    def tick(p):
        for a in s.process_tick(p, entries_blocked=False):
            print(f"  tick {p:7.2f} -> {a.type.value:9s} px={a.price:7.2f} q={a.qty:4d}  {a.reason}")

    print("\n-- tick stream --")
    tick(195.00)     # below entry, nothing
    tick(197.60)     # >= entry 197.51 -> ENTER 260 @197.51; book-half=217.51 target=237.51
    tick(210.00)     # rising, below book-half
    tick(217.60)     # >= book-half -> BOOK_HALF 130 @217.51, SL->breakeven 197.51
    tick(223.00)     # peak 223; steps=floor((223-217.51)/5)=1 -> trail SL=197.51+5=202.51
    tick(229.00)     # peak 229; steps=floor((229-217.51)/5)=2 -> trail SL=207.51
    tick(238.00)     # >= target 237.51 -> EXIT remaining 130 @237.51
    print("\nfinal state:", s.state.value, "| trades_taken:", s.trades_taken)
    assert s.state == State.IDLE and s.trades_taken == 1
    print("Tick-based strategy self-test passed.")
