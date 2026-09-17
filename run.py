#!/usr/bin/env python3
"""Entry point: python run.py [--live] [--allow-heavy-sizing] [...]

See README.md for the full command reference and milestone gates.
"""
import sys

from bot.cli import main

if __name__ == "__main__":
    sys.exit(main())
