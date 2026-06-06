#!/usr/bin/env python3
"""Top-level `ade` command. Symlinked into ~/bin/ade by setup.sh."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ade.cli import main

if __name__ == "__main__":
    main()
