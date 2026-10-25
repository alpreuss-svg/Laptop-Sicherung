"""Einfache Fortschrittsanzeige auf der Konsole (stderr)."""
from __future__ import annotations

import os
import sys
import time

from .fsutil import console_safe, fmt_duration, fmt_int, fmt_size

# Optionaler Empfänger für Fortschrittsmeldungen (z. B. die Fensteroberfläche).
# Wird mit einem dict aufgerufen: label, items, total_items, bytes, total_bytes, rate, eta, text, done
LISTENER = None


class Progress:
    def __init__(self, label: str, total_items: int = 0, total_bytes: int = 0, enabled: bool = True):
        self.label = label
        self.total_items = total_items
        self.total_bytes = total_bytes
        self.items = 0
        self.bytes = 0
        self.start = time.monotonic()
        self._last = 0.0
        self.enabled = enabled and os.environ.get("DUPECLEAN_QUIET") != "1"
        self._tty = bool(sys.stderr) and sys.stderr.isatty()
        self._interval = 0.25 if (self._tty or LISTENER) else 10.0
        self._width = 0

    def add(self, items: int = 0, nbytes: int = 0, extra: str = "") -> None:
        self.items += items
        self.bytes += nbytes
        now = time.monotonic()
        if now - self._last >= self._interval:
            self._last = now
            self._show(extra)

    def _rate(self) -> float:
        dt = max(time.monotonic() - self.start, 1e-6)
        return self.bytes / dt

    def _line(self, extra: str = "") -> str:
        parts = [self.label + ":"]
        if self.total_items:
            parts.append(f"{fmt_int(self.items)}/{fmt_int(self.total_items)} Dateien")
        else:
            parts.append(f"{fmt_int(self.items)} Dateien")
        if self.total_bytes:
            parts.append(f"{fmt_size(self.bytes)}/{fmt_size(self.total_bytes)}")
        elif self.bytes:
            parts.append(fmt_size(self.bytes))
        rate = self._rate()
        if self.bytes:
            parts.append(f"{fmt_size(rate)}/s")
        if self.total_bytes and rate > 0:
            parts.append("Rest ~" + fmt_duration((self.total_bytes - self.bytes) / rate))
        elif self.total_items and self.items:
            dt = time.monotonic() - self.start
            parts.append("Rest ~" + fmt_duration(dt / self.items * (self.total_items - self.items)))
        line = "  ".join(parts)
        if extra:
            line += "  " + extra
        return line

    def _notify(self, extra: str = "", done: bool = False) -> None:
        if LISTENER is None:
            return
        rate = self._rate()
        eta = None
        if not done:
            if self.total_bytes and rate > 0:
                eta = (self.total_bytes - self.bytes) / rate
            elif self.total_items and self.items:
                eta = (time.monotonic() - self.start) / self.items * (self.total_items - self.items)
        try:
            LISTENER({"label": self.label, "items": self.items, "total_items": self.total_items,
                      "bytes": self.bytes, "total_bytes": self.total_bytes, "rate": rate, "eta": eta,
                      "text": self._line(extra), "done": done})
        except Exception:
            pass

    def _show(self, extra: str = "") -> None:
        self._notify(extra)
        if not self.enabled or not sys.stderr:
            return
        line = console_safe(self._line(extra))
        if self._tty:
            cols = 120
            try:
                cols = os.get_terminal_size(sys.stderr.fileno()).columns
            except OSError:
                pass
            line = line[: cols - 1]
            pad = max(0, self._width - len(line))
            sys.stderr.write("\r" + line + " " * pad)
            self._width = len(line)
        else:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()

    def done(self, extra: str = "") -> None:
        self._notify(extra, done=True)
        if not self.enabled or not sys.stderr:
            return
        dt = time.monotonic() - self.start
        line = self._line(extra).split("  Rest ~")[0] + f"  fertig in {fmt_duration(dt)}"
        if self._tty:
            sys.stderr.write("\r" + console_safe(line) + " " * max(0, self._width - len(line)) + "\n")
        else:
            sys.stderr.write(console_safe(line) + "\n")
        sys.stderr.flush()
