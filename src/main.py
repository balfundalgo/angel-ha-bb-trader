"""
main.py
=======
Entry point for the Heiken Ashi + Bollinger Band ATM options trader.
Launches the GUI.
"""

import multiprocessing


def main():
    multiprocessing.freeze_support()  # safe for PyInstaller --onefile
    from gui import run
    run()


if __name__ == "__main__":
    main()
