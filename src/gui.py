"""
gui.py
======
Modern, light-themed control panel (CustomTkinter) for the HA + Bollinger
options trader. Collects credentials + strategy parameters, starts/stops the
engine, and shows a live status panel and activity log.
"""

import customtkinter as ctk

import config
from logger import logger, set_gui_sink
from engine import TradingEngine

# ---- light theme palette ----
ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")

BG = "#f4f6fb"
CARD = "#ffffff"
ACCENT = "#2563eb"
TXT = "#1f2937"
SUB = "#6b7280"
OK = "#16a34a"
WARN = "#dc2626"


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"Balfund | HA + Bollinger Options Trader  (v{config.APP_VERSION})")
        self.geometry("1080x760")
        self.configure(fg_color=BG)
        self.engine = None
        config.load_settings()   # restore saved credentials + parameters
        self._build()
        set_gui_sink(self._log_line)
        logger.info(f"App version v{config.APP_VERSION} ready.")
        self.after(1000, self._tick_monitor)   # live P&L / trade panel poll

    # ------------------------------------------------------------------
    def _card(self, parent, title):
        frame = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14)
        ctk.CTkLabel(frame, text=title, text_color=TXT,
                     font=("Segoe UI Semibold", 15)).pack(
            anchor="w", padx=16, pady=(12, 4))
        return frame

    def _field(self, parent, label, default="", show=None, width=180):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(row, text=label, text_color=SUB,
                     font=("Segoe UI", 12), width=150, anchor="w").pack(side="left")
        e = ctk.CTkEntry(row, width=width, show=show, fg_color="#f9fafb",
                         text_color=TXT, border_color="#e5e7eb")
        if default:
            e.insert(0, str(default))
        e.pack(side="left")
        return e

    def _option(self, parent, label, values, default, width=180):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=4)
        ctk.CTkLabel(row, text=label, text_color=SUB,
                     font=("Segoe UI", 12), width=150, anchor="w").pack(side="left")
        var = ctk.StringVar(value=str(default))
        ctk.CTkOptionMenu(row, values=[str(v) for v in values], variable=var,
                          width=width, fg_color="#f9fafb", text_color=TXT,
                          button_color=ACCENT, dropdown_fg_color=CARD,
                          dropdown_text_color=TXT).pack(side="left")
        return var

    # ------------------------------------------------------------------
    def _build(self):
        ctk.CTkLabel(self, text="Heiken Ashi + Bollinger Band Options Trader",
                     text_color=TXT, font=("Segoe UI Semibold", 22)).pack(
            anchor="w", padx=24, pady=(18, 2))
        ctk.CTkLabel(self, text=f"ATM CE/PE buying · HA candles · BB on HA close   —   v{config.APP_VERSION}",
                     text_color=SUB, font=("Segoe UI", 13)).pack(
            anchor="w", padx=24, pady=(0, 12))

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=4)

        # left column = scrollable fields on top + FIXED button bar at bottom
        left_container = ctk.CTkFrame(body, fg_color="transparent", width=440)
        left_container.pack(side="left", fill="y", padx=(0, 10))
        left_container.pack_propagate(False)
        footer = ctk.CTkFrame(left_container, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(8, 0))
        left = ctk.CTkScrollableFrame(left_container, fg_color="transparent")
        left.pack(side="top", fill="both", expand=True)

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True)

        # ----- credentials -----
        cred = self._card(left, "Angel One Credentials")
        cred.pack(fill="x", pady=6)
        c = config.CREDENTIALS
        self.e_client = self._field(cred, "Client ID", c.get("client_id", ""))
        self.e_apikey = self._field(cred, "API Key", c.get("api_key", ""))
        self.e_mpin = self._field(cred, "MPIN", c.get("mpin", ""), show="*")
        self.e_totp = self._field(cred, "TOTP Secret", c.get("totp_secret", ""), show="*")
        ctk.CTkLabel(cred, text="Credentials stay on this machine.",
                     text_color=SUB, font=("Segoe UI", 10)).pack(
            anchor="w", padx=16, pady=(0, 10))

        # ----- strategy -----
        st = config.STRATEGY
        strat = self._card(left, "Strategy Parameters")
        strat.pack(fill="x", pady=6)
        self.v_mode = self._option(strat, "Mode", ["PAPER", "LIVE"], config.TRADING_MODE)
        self.v_index = self._option(strat, "Index", ["NIFTY", "BANKNIFTY", "SENSEX"], st["index"])
        self.v_tf = self._option(strat, "Timeframe (min)",
                                 [1, 3, 5, 10, 15, 30, 60, 240], st["timeframe"])
        self.v_opt = self._option(strat, "Option side", ["BOTH", "CE", "PE"], st["option_type"])
        self.e_ref = self._field(strat, "ATM ref time", st["ref_time"])
        self.e_start = self._field(strat, "Start time", st["start_time"])
        self.e_stop = self._field(strat, "Stop entry time", st["stop_entry_time"])
        self.e_sq = self._field(strat, "Square-off time", st["square_off_time"])
        self.e_bbp = self._field(strat, "BB period", st["bb_period"])
        self.e_bbm = self._field(strat, "BB multiplier", st["bb_mult"])
        self.e_lots = self._field(strat, "Lots (even)", st["lots"])
        self.e_entry = self._field(strat, "Entry % above high", st["entry_pct"] * 100)
        self.e_slbuf = self._field(strat, "SL buffer (pts)", st["sl_buffer"])
        self.e_trail = self._field(strat, "Trail step (pts)", st["trail_step"])
        self.e_rr = self._field(strat, "Target R:R", st["rr_target"])
        self.e_max = self._field(strat, "Max trades", st["max_trades"])

        btns = ctk.CTkFrame(footer, fg_color="transparent")
        btns.pack(fill="x", pady=(4, 4), padx=4)
        self.btn_start = ctk.CTkButton(btns, text="Start", fg_color=ACCENT,
                                       hover_color="#1d4ed8", command=self._start)
        self.btn_start.pack(side="left", expand=True, fill="x", padx=4)
        self.btn_stop = ctk.CTkButton(btns, text="Stop", fg_color="#9ca3af",
                                      hover_color=WARN, command=self._stop,
                                      state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=4)

        self.btn_save = ctk.CTkButton(footer, text="💾  Save Settings",
                                      fg_color="#10b981", hover_color="#059669",
                                      command=self._save_settings)
        self.btn_save.pack(fill="x", pady=(0, 6), padx=4)

        # ----- live P&L monitor (websocket-driven, polled 1s) -----
        pnlc = self._card(right, "Live P&L")
        pnlc.pack(fill="x", pady=6)
        self.lbl_pnl_total = ctk.CTkLabel(pnlc, text="\u20b9 0.00",
                                          text_color=OK, font=("Segoe UI Semibold", 30),
                                          anchor="w")
        self.lbl_pnl_total.pack(anchor="w", padx=16, pady=(0, 0))
        self.lbl_pnl_sub = ctk.CTkLabel(pnlc, text="Realized: \u20b90.00    Unrealized: \u20b90.00",
                                        text_color=SUB, font=("Segoe UI", 12), anchor="w")
        self.lbl_pnl_sub.pack(anchor="w", padx=16, pady=(0, 12))

        # ----- active trades panel -----
        trc = self._card(right, "Active Trades")
        trc.pack(fill="x", pady=6)
        self.lbl_trades = ctk.CTkLabel(trc, text="(engine not started)",
                                       text_color=TXT, font=("Consolas", 12),
                                       justify="left", anchor="w")
        self.lbl_trades.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        # ----- status -----
        stat = self._card(right, "Live Status")
        stat.pack(fill="x", pady=6)
        self.lbl_status = ctk.CTkLabel(stat, text="Idle. Configure and press Start.",
                                       text_color=TXT, font=("Consolas", 13),
                                       justify="left", anchor="w")
        self.lbl_status.pack(anchor="w", padx=16, pady=(0, 12), fill="x")

        logc = self._card(right, "Activity Log")
        logc.pack(fill="both", expand=True, pady=6)
        self.txt = ctk.CTkTextbox(logc, fg_color="#0f172a", text_color="#e2e8f0",
                                  font=("Consolas", 12), corner_radius=10)
        self.txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))

    # ------------------------------------------------------------------
    def _collect(self):
        config.CREDENTIALS.update({
            "client_id": self.e_client.get().strip(),
            "api_key": self.e_apikey.get().strip(),
            "mpin": self.e_mpin.get().strip(),
            "totp_secret": self.e_totp.get().strip(),
        })
        config.TRADING_MODE = self.v_mode.get()
        s = config.STRATEGY
        s["index"] = self.v_index.get()
        s["timeframe"] = int(self.v_tf.get())
        s["option_type"] = self.v_opt.get()
        s["ref_time"] = self.e_ref.get().strip()
        s["start_time"] = self.e_start.get().strip()
        s["stop_entry_time"] = self.e_stop.get().strip()
        s["square_off_time"] = self.e_sq.get().strip()
        s["bb_period"] = int(self.e_bbp.get())
        s["bb_mult"] = float(self.e_bbm.get())
        s["lots"] = int(self.e_lots.get())
        s["entry_pct"] = float(self.e_entry.get()) / 100.0
        s["sl_buffer"] = float(self.e_slbuf.get())
        s["trail_step"] = float(self.e_trail.get())
        s["rr_target"] = float(self.e_rr.get())
        s["max_trades"] = int(self.e_max.get())

    def _save_settings(self):
        try:
            self._collect()
        except ValueError as e:
            logger.error(f"Invalid input, not saved: {e}")
            return
        if config.save_settings():
            logger.info(f"Settings saved to {config.SETTINGS_FILE}")
        else:
            logger.error("Failed to save settings.")

    def _start(self):
        try:
            self._collect()
        except ValueError as e:
            logger.error(f"Invalid input: {e}")
            return
        if config.STRATEGY["lots"] % 2 != 0:
            logger.error("Lots must be an even number.")
            return
        config.save_settings()   # auto-save so a good config persists
        logger.info("Starting engine...")
        self.engine = TradingEngine(status_cb=self._status)
        self.engine.start()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal", fg_color=WARN)

    def _stop(self):
        if self.engine:
            self.engine.stop()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled", fg_color="#9ca3af")

    # callbacks (engine threads -> marshal to UI thread)
    def _status(self, st):
        self.after(0, lambda: self._render_status(st))

    def _render_status(self, st):
        lines = [f"Mode: {st['mode']}   Index ATM: {st.get('atm')}   "
                 f"Ref spot: {st.get('ref_spot')}   Realized P&L: {st.get('pnl')}"]
        for leg, info in st.get("legs", {}).items():
            lines.append(
                f"  {leg}: {info['state']:11s} {info['symbol']}  "
                f"entry={info['entry']} SL={info['sl']} T1={info['t1']} "
                f"trades={info['trades']}")
        self.lbl_status.configure(text="\n".join(lines))

    def _log_line(self, line):
        self.after(0, lambda: (self.txt.insert("end", line + "\n"),
                               self.txt.see("end")))

    # ---------------- live P&L + trade panel (read-only, 1s poll) --------
    def _tick_monitor(self):
        try:
            if self.engine is not None:
                self._render_live(self.engine.live_snapshot())
        except Exception:
            pass
        self.after(1000, self._tick_monitor)

    def _render_live(self, snap):
        total = snap["total"]
        color = OK if total >= 0 else WARN
        self.lbl_pnl_total.configure(text=f"\u20b9 {total:,.2f}", text_color=color)
        self.lbl_pnl_sub.configure(
            text=f"Realized: \u20b9{snap['realized']:,.2f}    "
                 f"Unrealized: \u20b9{snap['unrealized']:,.2f}")

        header = (f"{'Leg':<4}{'Symbol':<22}{'State':<11}"
                  f"{'Entry':>9}{'LTP':>9}{'Qty':>6}{'SL':>9}{'P&L':>11}")
        rows = [header, "-" * len(header)]
        if not snap["legs"]:
            rows.append("(engine not started)")
        for lg in snap["legs"]:
            e = f"{lg['entry']:.2f}" if lg["entry"] else "-"
            l = f"{lg['ltp']:.2f}" if lg["ltp"] else "-"
            sl = f"{lg['sl']:.2f}" if lg["sl"] else "-"
            rows.append(
                f"{lg['leg']:<4}{lg['symbol']:<22}{lg['state']:<11}"
                f"{e:>9}{l:>9}{lg['qty']:>6}{sl:>9}{lg['unrealized']:>+11.2f}")
        self.lbl_trades.configure(text="\n".join(rows))


def run():
    App().mainloop()


if __name__ == "__main__":
    run()
