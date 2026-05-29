"""
logger.py
=========
Lightweight logger that writes to a dated file and, optionally, forwards
each line to a GUI callback so the user sees live activity in the app.
"""

import logging
import config

_gui_sink = None  # callable(str) set by the GUI


def set_gui_sink(fn):
    global _gui_sink
    _gui_sink = fn


class _GuiHandler(logging.Handler):
    def emit(self, record):
        if _gui_sink:
            try:
                _gui_sink(self.format(record))
            except Exception:
                pass


def _build():
    lg = logging.getLogger("ha_bb_trader")
    lg.setLevel(logging.INFO)
    if lg.handlers:
        return lg
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%H:%M:%S")

    fh = logging.FileHandler(config.log_file(), encoding="utf-8")
    fh.setFormatter(fmt)
    lg.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    lg.addHandler(sh)

    gh = _GuiHandler()
    gh.setFormatter(fmt)
    lg.addHandler(gh)
    return lg


logger = _build()
