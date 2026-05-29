"""
angel_connection.py
===================
Angel One SmartAPI connection with TOTP login, health check and reconnect.
Connection pattern adapted from the user's reference connectapi_new.py.
"""

import time
import config
from logger import logger

try:
    from SmartApi import SmartConnect
    import pyotp
except Exception:  # allows import on machines without the SDK (e.g. CI lint)
    SmartConnect = None
    pyotp = None


class ConnectionManager:
    def __init__(self):
        self.obj = None
        self.connected = False
        self.feed_token = None

    def connect(self):
        if SmartConnect is None or pyotp is None:
            logger.error("SmartApi / pyotp not installed.")
            return None

        c = config.CREDENTIALS
        if not all([c["client_id"], c["api_key"], c["mpin"], c["totp_secret"]]):
            logger.error("Missing Angel One credentials.")
            return None

        logger.info("Connecting to Angel One SmartAPI...")
        for attempt in range(config.RETRY["max_retries"]):
            try:
                totp = pyotp.TOTP(c["totp_secret"]).now()
                obj = SmartConnect(api_key=c["api_key"])
                data = obj.generateSession(c["client_id"], c["mpin"], totp)
                if data and data.get("data"):
                    self.obj = obj
                    self.connected = True
                    self.feed_token = obj.getfeedToken()
                    config.SMART = obj
                    logger.info("=" * 50)
                    logger.info(f"CONNECTED  |  client {c['client_id']}")
                    logger.info("=" * 50)
                    return obj
                logger.error(f"Login attempt {attempt+1}: bad response {data}")
            except Exception as e:
                logger.error(f"Login attempt {attempt+1} failed: {e}")
            if attempt < config.RETRY["max_retries"] - 1:
                time.sleep(config.RETRY["retry_delay"] * (attempt + 1))
        logger.critical("Failed to connect after all retries.")
        return None

    def health_ok(self):
        if not self.connected or not self.obj:
            return False
        try:
            r = self.obj.ltpData("NSE", "Nifty 50", "99926000")
            return bool(r and r.get("data"))
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self.connected = False
            return False

    def get(self):
        if not self.health_ok():
            logger.warning("Connection unhealthy - reconnecting...")
            return self.connect()
        return self.obj


connection_manager = ConnectionManager()
