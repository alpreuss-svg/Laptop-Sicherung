"""Startskript: python dupeclean.py <befehl> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dupeclean.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
