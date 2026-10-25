"""DupeClean mit Fensteroberfläche starten (Doppelklick oder: pyw dupeclean_gui.pyw)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dupeclean.gui import main  # noqa: E402

main()
