"""
angel_websocket.py
==================
Thin wrapper over Angel One SmartWebSocketV2. Subscribes to the option tokens
in quote mode and routes every tick to a user callback:

    on_tick(token: str, ltp: float, volume: float, ts: datetime)

Connection pattern follows the reference Websocket_new.py. Includes
auto-reconnect with resubscription. The engine supplies the callback that
forwards ticks to the right CandleBuilder.
"""

from __future__ import annotations
import threading
import time
from datetime import datetime

import config
from logger import logger

try:
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2
except Exception:
    SmartWebSocketV2 = None


# Angel WebSocket exchangeType codes
WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4, "MCX": 5}
MODE_QUOTE = 2  # 1=LTP, 2=Quote, 3=SnapQuote


class WebSocketFeed:
    def __init__(self, on_tick):
        self.on_tick = on_tick
        self.sws = None
        self.connected = False
        self.tokens_by_exch = {}        # exchangeType -> [token,...]
        self._reconnects = 0
        self._max_reconnects = 10
        self._stop = False

    # ------------------------------------------------------------------
    def add_token(self, token: str, opt_exchange: str):
        ex = WS_EXCHANGE_TYPE.get(opt_exchange, 2)
        self.tokens_by_exch.setdefault(ex, [])
        if token not in self.tokens_by_exch[ex]:
            self.tokens_by_exch[ex].append(str(token))

    def _sub_list(self):
        return [{"exchangeType": ex, "tokens": toks}
                for ex, toks in self.tokens_by_exch.items() if toks]

    # ------------------------------------------------------------------
    def start(self):
        if SmartWebSocketV2 is None:
            logger.error("SmartWebSocketV2 not available (SDK missing).")
            return
        threading.Thread(target=self._connect, daemon=True).start()

    def stop(self):
        self._stop = True
        try:
            if self.sws:
                self.sws.close_connection()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _connect(self):
        if self._stop:
            return
        try:
            auth = getattr(config, "AUTH_TOKEN", None)
            feed = getattr(config, "FEED_TOKEN", None)
            if not auth or not feed:
                logger.error("WebSocket: missing auth/feed token.")
                return
            self.sws = SmartWebSocketV2(
                auth, config.CREDENTIALS["api_key"],
                config.CREDENTIALS["client_id"], feed)
            self.sws.on_open = self._on_open
            self.sws.on_data = self._on_data
            self.sws.on_error = self._on_error
            self.sws.on_close = self._on_close
            logger.info("Connecting WebSocket feed...")
            self.sws.connect()
        except Exception as e:
            logger.error(f"WebSocket connect error: {e}")
            self._reconnect()

    def _on_open(self, wsapp):
        self.connected = True
        self._reconnects = 0
        logger.info("WebSocket feed connected; subscribing tokens...")
        try:
            self.sws.subscribe("habb_feed", MODE_QUOTE, self._sub_list())
            logger.info(f"Subscribed: {self._sub_list()}")
        except Exception as e:
            logger.error(f"Subscribe error: {e}")

    def _on_data(self, wsapp, message):
        try:
            token = message.get("token")
            ltp = message.get("last_traded_price")
            if token is None or ltp is None:
                return
            price = float(ltp) / 100.0  # paise -> rupees
            vol = float(message.get("volume_trade_for_the_day", 0) or 0)
            ts = datetime.now()
            self.on_tick(str(token), price, vol, ts)
        except Exception as e:
            logger.error(f"WS data error: {e}")

    def _on_error(self, wsapp, error):
        logger.error(f"WebSocket error: {error}")
        self.connected = False

    def _on_close(self, wsapp):
        self.connected = False
        if not self._stop:
            logger.warning("WebSocket closed; reconnecting...")
            self._reconnect()

    def _reconnect(self):
        if self._stop or self._reconnects >= self._max_reconnects:
            return
        self._reconnects += 1
        wait = min(30, self._reconnects * 5)
        logger.warning(f"WS reconnect attempt {self._reconnects} in {wait}s")
        time.sleep(wait)
        if self._stop:                 # may have been stopped during the wait
            return
        threading.Thread(target=self._connect, daemon=True).start()
