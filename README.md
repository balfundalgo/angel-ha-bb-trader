# Heiken Ashi + Bollinger Band — ATM Options Trader (Angel One)

Buys ATM CE/PE options on a Heiken-Ashi + Bollinger-Band bounce off the lower
band, manages with a 1:2 partial book + breakeven + step-trail, and targets the
upper band. NIFTY / BANKNIFTY / SENSEX, selectable timeframe, paper or live.

> Educational/automation tooling. Test thoroughly in **PAPER** mode first.
> Trading derivatives carries risk; you are responsible for all orders placed.

---

## Strategy (as implemented)

1. **ATM selection** — at the reference time (default **09:07**, NSE pre-open
   window) the spot is captured live and the ATM strike is locked.
2. **Charts** — everything runs on the **option strike's** chart using
   **Heiken Ashi** candles; **Bollinger Bands** are computed on the **HA close**
   (period/multiplier configurable; population std to match the broker).
3. **Entry**
   - A **red** HA candle closes **below** the lower band → *alert*.
   - The first **green** HA candle after it that closes **above** the lower band
     → *trigger*.
   - Entry level = trigger HA high **+ 5%** (configurable). Fires when a later
     candle's HA high reaches it.
   - **Stop loss** = alert(red) candle HA low − buffer (points, configurable).
   - **Cancel** if any candle's HA low hits the SL before entry fills.
4. **Management**
   - **T1 = 1:2** → book **half** the lots, move remaining SL to **breakeven**.
   - **Trail** — reference is the T1 level; for every **+5 points above T1**,
     raise SL by **5 points** from breakeven.
   - **Overall target = upper band** → exit remaining there.
5. **Max trades** — combined CE+PE cap per day (configurable).
6. Both CE and PE legs run **simultaneously** when "Option side = BOTH".

### On the 09:07 price
The historical candle API has **no 09:07 candle** (candles start 09:15), so this
value can only be captured **live** while the app is running. The app polls the
index quote at the reference time, prefers a *moving* (live pre-open) tick, and
falls back to the first available value — logging which source was used. Keep
the app running before 09:07.

### Market data: WebSocket-driven (no candle polling)
To stay clear of Angel's `getCandleData` rate limit (3/sec, 180/min, **shared
across your whole client code**), the app fetches history **once** at startup
(one call per leg, just to seed the Bollinger warmup), then subscribes to the
**WebSocket** and builds candles locally from the live tick stream. After
startup it makes **zero** `getCandleData` calls, so it won't be throttled and
won't compete with your other Angel apps for the candle budget.

`candle_fetch_delay` (default 5s) is the grace period after each candle close
for the first tick of the new candle to arrive and finalise the just-closed
candle before the strategy reads it.

### Notes / assumptions
- **SENSEX** options trade on **BFO** (BSE), strike step 100; NIFTY on NFO
  (step 50); BANKNIFTY on NFO (step 100). Lot size is read live from the scrip
  master, not hardcoded.
- **4-hour** is not a native Angel interval — it is resampled from 1-hour,
  anchored to the 09:15 session start.
- **Lots must be even** (so "half" is exact).
- **Paper mode still needs a live Angel connection** for real market data.

---

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
