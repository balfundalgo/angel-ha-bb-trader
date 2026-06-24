# Heiken Ashi + Bollinger Band — ATM Options Trader (Angel One)

Buys ATM CE/PE options on a Heiken-Ashi + Bollinger-Band bounce off the lower
band, manages with a 1:2 partial book + breakeven + step-trail, and targets the
upper band. NIFTY / BANKNIFTY / SENSEX, selectable timeframe, paper or live.

> Educational/automation tooling. Test thoroughly in **PAPER** mode first.
> Trading derivatives carries risk; you are responsible for all orders placed.

---

## Strategy (as implemented)

Everything runs on the **option strike's** chart using **Heiken Ashi** candles;
**Bollinger Bands** are computed on the **HA close**.

1. **ATM selection** - at the reference time (default 09:07, pre-open) the spot
   is captured live and the ATM strike is locked. CE and PE run simultaneously.
2. **Signal detection (on HA candle close):**
   - **Alert**: a RED HA candle closes BELOW the lower band.
   - **Trigger**: the FIRST GREEN HA candle after the alert (no band condition).
     On trigger, the setup is armed: entry level = trigger HA high + 5%,
     stop loss = alert(red) HA low - buffer.
3. **Execution (on live ticks / LTP):**
   - **Cancel**: LTP hits the SL before entry -> void the setup.
   - **Entry**: LTP crosses the entry level -> buy immediately (no candle wait).
     Quantity = lots x 2 x lot_size (1 lot = 2 units; "half" is always exact).
   - **Book half**: LTP reaches entry + (target/2) -> sell half, move remaining
     SL to breakeven.
   - **Trail**: from the book-half level, raise SL by `trail_step` for every
     `trail_step` points of favourable move.
   - **Full target**: LTP reaches entry + target points -> exit remaining half.
   - The remaining half exits on whichever comes first: trailing stop or target.
   - **SL exit** any time LTP hits the current SL.
4. **Max trades** (combined CE+PE), time gates, and square-off as configured.

Target is set in **premium points** (GUI field "Target (points)"). The old 1:2
R:R and the upper-Bollinger-band target have been removed.

> NOTE: entries and exits are **tick-driven** with no candle-close backstop. If
> the tick feed drops while a position is open, a stop/target may not fire until
> ticks resume.


## Run from source
```bash
pip install -r requirements.txt
python src/main.py
```

## Build the Windows EXE (GitHub Actions)
1. Push this repo to GitHub (branch `main`).
2. The **Build Windows EXE** workflow runs automatically (or trigger it from the
   **Actions** tab → *Run workflow*).
3. Download `HA_BB_Trader.exe` from the run's **Artifacts**.

The workflow uses `windows-latest` + PyInstaller `--onefile --windowed` and
collects CustomTkinter and SmartApi data files.

## Layout
```
src/
  config.py            settings, index specs, timeframe map
  logger.py            file + GUI logging
  api_rate_limiter.py  per-endpoint Angel rate limiter
  angel_connection.py  SmartAPI login / reconnect
  angel_data.py        scrip master, 09:07 capture, ATM resolve, candles
  candle_builder.py    builds timeframe candles locally from WS ticks
  angel_websocket.py   SmartWebSocketV2 feed -> tick callback
  indicators.py        Heiken Ashi + Bollinger (on HA close)
  strategy.py          per-leg state machine (unit-tested)
  order_manager.py     live + paper order execution + trade ledger
  engine.py            session orchestration (candle loop, both legs)
  gui.py               CustomTkinter light-themed control panel
  main.py              entry point
.github/workflows/build.yml
requirements.txt
```

Credentials are entered in the GUI at runtime and never committed.
```
