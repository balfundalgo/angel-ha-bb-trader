"""
strategy.py
===========
Per-leg Heiken Ashi + Bollinger Band strategy state machine.

ONE instance handles ONE option leg (ATM CE or ATM PE). The live engine
runs two instances simultaneously (per spec: "both can run").

The engine is CANDLE-CLOSE driven on Heiken Ashi values, because HA highs
and lows only exist at candle granularity. Each closed candle is fed in via
process_candle(); the engine returns a list of Action events that the order
layer executes (market entry, book-half, modify-SL, exit). This keeps the
trading logic broker-agnostic and fully unit-testable.

STRATEGY RULES (as specified)
-----------------------------
Entry:
  1. A RED HA candle closes BELOW the lower BB  -> "alert".
  2. The first GREEN HA candle AFTER it that closes ABOVE the lower BB
     -> "trigger" candle.
  3. Entry level = trigger candle HA_high * (1 + entry_pct)   (default 5%).
     Entry fires on a later candle whose HA_high >= entry level.
  4. Stop loss = alert(red) candle HA_low - sl_buffer (points, configurable).
  5. Cancellation: if, after the trigger and BEFORE entry fills, any candle's
     HA_low <= SL  -> setup voided, wait for a fresh alert.

Management once filled:
  * risk = entry - SL ; target T1 = entry + 2 * risk   (1:2).
  * On T1 hit (HA_high >= T1): book HALF the lots; move remaining SL to
    entry (breakeven / cost).
  * Trailing: reference = T1 level. For every +5 points above T1, raise SL
    by 5 points from breakeven  ->  SL = entry + floor((peak - T1)/5)*5.
  * Final target for runners = upper BB. On HA_high >= bb_upper -> exit all
    remaining.
  * SL exit any time HA_low <= current SL.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional
import math


class State(Enum):
    IDLE = "IDLE"                 # waiting for red-below-band alert
    WAIT_TRIGGER = "WAIT_TRIGGER" # alert seen, waiting for green trigger
    ARMED = "ARMED"               # trigger set, waiting for entry fill / cancel
    IN_FULL = "IN_FULL"           # filled, full qty, watching T1
    IN_RUNNER = "IN_RUNNER"       # half booked, trailing runners
    CLOSED = "CLOSED"             # this trade finished (will reset to IDLE)


class ActionType(Enum):
    ENTER = "ENTER"             # market buy full qty
    BOOK_HALF = "BOOK_HALF"     # sell half qty at/around T1
    MODIFY_SL = "MODIFY_SL"     # move protective SL
    EXIT_ALL = "EXIT_ALL"       # close remaining qty
    CANCEL = "CANCEL"           # setup cancelled before entry
    INFO = "INFO"               # log-only


@dataclass
class Action:
    type: ActionType
    price: float = 0.0
    qty: int = 0
    reason: str = ""


@dataclass
class LegConfig:
    leg: str                      # "CE" or "PE"
    lots: int = 2                 # MUST be even
    lot_size: int = 75            # contracts per lot (read from scrip master)
    entry_pct: float = 0.05       # 5% above trigger HA high
    sl_buffer: float = 5.0        # points subtracted from red HA low
    trail_step: float = 5.0       # points per trail step (and trigger gap)
    rr_target: float = 2.0        # 1:2 reward-to-risk for first target

    @property
    def total_qty(self) -> int:
        return self.lots * self.lot_size

    @property
    def half_qty(self) -> int:
        return (self.lots // 2) * self.lot_size


@dataclass
class LegStrategy:
    cfg: LegConfig
    state: State = State.IDLE
    trades_taken: int = 0

    # working setup values
    alert_low: Optional[float] = None     # red candle HA low
    trigger_high: Optional[float] = None  # green trigger HA high
    entry_level: Optional[float] = None
    stop_loss: Optional[float] = None
    entry_price: Optional[float] = None
    t1_level: Optional[float] = None
    peak: float = field(default=0.0)      # highest HA high since fill
    half_booked: bool = False

    def _reset_setup(self):
        self.alert_low = None
        self.trigger_high = None
        self.entry_level = None
        self.stop_loss = None
        self.entry_price = None
        self.t1_level = None
        self.peak = 0.0
        self.half_booked = False

    def process_candle(self, row: dict, max_trades_reached: bool) -> List[Action]:
        """
        Feed ONE closed HA candle. `row` keys required:
            ha_open, ha_high, ha_low, ha_close, ha_green,
            bb_upper, bb_lower (bb_lower may be NaN during warmup -> skip)
        Returns list of Actions to execute.
        """
        actions: List[Action] = []

        ha_open = row["ha_open"]
        ha_high = row["ha_high"]
        ha_low = row["ha_low"]
        ha_close = row["ha_close"]
        green = bool(row["ha_green"])
        bb_lower = row.get("bb_lower")
        bb_upper = row.get("bb_upper")

        warmup = (bb_lower is None or bb_upper is None
                  or (isinstance(bb_lower, float) and math.isnan(bb_lower)))

        # ---------------- IN-TRADE MANAGEMENT (highest priority) ----------
        if self.state in (State.IN_FULL, State.IN_RUNNER):
            self.peak = max(self.peak, ha_high)

            # 1) Stop loss check (HA low breaches SL)
            if ha_low <= self.stop_loss:
                qty = self.cfg.half_qty if self.state == State.IN_RUNNER else self.cfg.total_qty
                actions.append(Action(ActionType.EXIT_ALL, self.stop_loss, qty,
                                       f"SL hit @ {self.stop_loss:.2f}"))
                self._close_trade()
                return actions

            # 2) Final target = upper band (runners or even full)
            if (not warmup) and ha_high >= bb_upper:
                qty = self.cfg.half_qty if self.state == State.IN_RUNNER else self.cfg.total_qty
                actions.append(Action(ActionType.EXIT_ALL, bb_upper, qty,
                                       f"Upper-band target @ {bb_upper:.2f}"))
                self._close_trade()
                return actions

            if self.state == State.IN_FULL:
                # 3) First target 1:2 -> book half, SL to breakeven
                if ha_high >= self.t1_level:
                    actions.append(Action(ActionType.BOOK_HALF, self.t1_level,
                                           self.cfg.half_qty,
                                           f"T1 1:{self.cfg.rr_target:g} @ {self.t1_level:.2f}"))
                    self.half_booked = True
                    new_sl = self.entry_price  # breakeven
                    if new_sl != self.stop_loss:
                        self.stop_loss = new_sl
                        actions.append(Action(ActionType.MODIFY_SL, self.stop_loss,
                                               self.cfg.half_qty, "SL -> breakeven"))
                    self.state = State.IN_RUNNER
                return actions

            if self.state == State.IN_RUNNER:
                # 4) Trail: reference = T1. +trail_step pts above T1 -> SL up by trail_step
                if self.peak > self.t1_level:
                    steps = math.floor((self.peak - self.t1_level) / self.cfg.trail_step)
                    if steps >= 1:
                        target_sl = round(self.entry_price + steps * self.cfg.trail_step, 2)
                        if target_sl > self.stop_loss:
                            self.stop_loss = target_sl
                            actions.append(Action(ActionType.MODIFY_SL, self.stop_loss,
                                                   self.cfg.half_qty,
                                                   f"Trail SL -> {self.stop_loss:.2f}"))
                return actions

        # ---------------- ARMED: waiting for entry fill / cancel ----------
        if self.state == State.ARMED:
            # Cancellation: HA low dips to/below SL before entry
            if ha_low <= self.stop_loss:
                actions.append(Action(ActionType.CANCEL, 0, 0,
                                       f"Setup cancelled (low {ha_low:.2f} <= SL {self.stop_loss:.2f})"))
                self._reset_setup()
                self.state = State.IDLE
                return actions
            # Entry fill: HA high reaches entry level
            if ha_high >= self.entry_level:
                if max_trades_reached:
                    actions.append(Action(ActionType.INFO, 0, 0,
                                           "Entry signal but max trades reached - skipped"))
                    self._reset_setup()
                    self.state = State.IDLE
                    return actions
                self.entry_price = self.entry_level
                self.peak = ha_high
                risk = self.entry_price - self.stop_loss
                self.t1_level = round(self.entry_price + self.cfg.rr_target * risk, 2)
                self.trades_taken += 1
                actions.append(Action(ActionType.ENTER, self.entry_price,
                                       self.cfg.total_qty,
                                       f"Entry @ {self.entry_price:.2f} | SL {self.stop_loss:.2f} "
                                       f"| T1 {self.t1_level:.2f}"))
                self.state = State.IN_FULL
            return actions

        # ---------------- Signal search (no warmup bands -> wait) ----------
        if warmup:
            return actions

        # IDLE: look for red candle closing below lower band (alert)
        if self.state == State.IDLE:
            if (not green) and ha_close < bb_lower:
                self.alert_low = ha_low
                self.state = State.WAIT_TRIGGER
                actions.append(Action(ActionType.INFO, 0, 0,
                                       f"Alert: red HA closed below lower band "
                                       f"({ha_close:.2f} < {bb_lower:.2f})"))
            return actions

        # WAIT_TRIGGER: first green candle closing above lower band -> arm
        if self.state == State.WAIT_TRIGGER:
            # update alert low if another red candle prints below band (most recent red)
            if (not green) and ha_close < bb_lower:
                self.alert_low = ha_low
                return actions
            if green and ha_close > bb_lower:
                self.trigger_high = ha_high
                self.entry_level = round(self.trigger_high * (1 + self.cfg.entry_pct), 2)
                self.stop_loss = round(self.alert_low - self.cfg.sl_buffer, 2)
                self.state = State.ARMED
                actions.append(Action(ActionType.INFO, 0, 0,
                                       f"Trigger green HA. Entry {self.entry_level:.2f}, "
                                       f"SL {self.stop_loss:.2f}"))
            return actions

        return actions

    def _close_trade(self):
        self._reset_setup()
        self.state = State.IDLE


# ----------------------------------------------------------------------
# Self-test: drive a synthetic candle sequence through the state machine
# ----------------------------------------------------------------------
if __name__ == "__main__":

    def candle(o, h, l, c, up, lo):
        return {"ha_open": o, "ha_high": h, "ha_low": l, "ha_close": c,
                "ha_green": c >= o, "bb_upper": up, "bb_lower": lo}

    cfg = LegConfig(leg="CE", lots=2, lot_size=75, entry_pct=0.05,
                    sl_buffer=5.0, trail_step=5.0, rr_target=2.0)
    s = LegStrategy(cfg)

    seq = [
        # red candle closes below lower band (alert). low=90
        candle(100, 101, 90, 92, up=130, lo=95),
        # green candle closes above lower band -> trigger. high=100
        # entry = 100*1.05 = 105 ; SL = 90 - 5 = 85 ; risk=20 ; T1=105+40=145
        candle(93, 100, 92, 99, up=130, lo=95),
        # candle reaches entry (high>=105) -> ENTER @105
        candle(100, 106, 99, 104, up=135, lo=96),
        # rises but below T1
        candle(104, 130, 103, 128, up=150, lo=100),
        # hits T1 145 -> BOOK_HALF, SL->breakeven 105
        candle(128, 146, 127, 144, up=170, lo=110),
        # peak 158 -> 158-145=13 -> 2 steps -> SL=105+10=115
        candle(144, 158, 143, 150, up=175, lo=115),
        # touches upper band 175 -> EXIT_ALL runners
        candle(150, 176, 149, 172, up=175, lo=120),
    ]

    for i, row in enumerate(seq):
        acts = s.process_candle(row, max_trades_reached=False)
        for a in acts:
            print(f"candle#{i} [{s.state.value:11s}] {a.type.value:9s} "
                  f"px={a.price:7.2f} qty={a.qty:4d}  {a.reason}")

    print("\nFinal state:", s.state.value, "| trades_taken:", s.trades_taken)
    assert s.trades_taken == 1
    print("State-machine self-test passed.")
