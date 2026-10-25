"""Gemeinsame Hilfen für die Tests: erzeugte Testbäume im Temp-Ordner, nie auf echten Daten."""
from __future__ import annotations

import contextlib
import csv
import glob
import hashlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from dupeclean import fsutil  # noqa: E402
from dupeclean.cli import main  # noqa: E402
from dupeclean.fsutil import lp  # noqa: E402

T0 = 1_600_000_000  # feste Zeitstempel für reproduzierbare Regeln


def can_symlink() -> bool:
    d = tempfile.mkdtemp(prefix="dupeclean_symtest_")
    try:
        os.symlink(d, os.path.join(d, "l"), target_is_directory=True)
        return True
    except OSError:
        return False
    finally:
        fsutil.remove_tree(d)


def make_junction(link: str, target: str) -> bool:
    r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
    return r.returncode == 0


class TreeTest(unittest.TestCase):
    """Basisklasse: legt pro Test einen frischen Testbaum an."""

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="dupeclean_test_")
        self.root = os.path.join(self.base, "Sicherung")
        self.data = os.path.join(self.base, "daten")
        self.quar = os.path.join(self.base, "Quarantaene")
        os.makedirs(self.root)
        self._env = {k: os.environ.get(k) for k in ("DUPECLEAN_HOME", "DUPECLEAN_QUIET")}
        os.environ["DUPECLEAN_HOME"] = os.path.join(self.base, "home")
        os.environ["DUPECLEAN_QUIET"] = "1"
        self._mt = 0

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k in ("DUPECLEAN_TEST_ABORT_SCAN_AFTER", "DUPECLEAN_TEST_ABORT_APPLY_AFTER",
                  "DUPECLEAN_TEST_CRASH_AFTER_MOVE"):
            os.environ.pop(k, None)
        fsutil.remove_tree(self.base)

    # ------------------------------------------------------------ Baum erzeugen
    def p(self, rel: str) -> str:
        return os.path.join(self.root, rel.replace("/", "\\"))

    def w(self, rel: str, data: bytes | str, mtime: int | None = None) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        path = self.p(rel)
        fsutil.makedirs(os.path.dirname(path))
        with open(lp(path), "wb") as f:
            f.write(data)
        if mtime is None:
            self._mt += 1
            mtime = T0 + self._mt * 60
        os.utime(lp(path), (mtime, mtime))
        return path

    # ------------------------------------------------------------ CLI
    def cli(self, *args: str, answer: str = "ja") -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(list(args), confirm=lambda _prompt: answer)
        return rc, buf.getvalue()

    def scan(self, *extra: str) -> int:
        rc, out = self.cli("scan", self.root, "--daten", self.data, *extra)
        self.assertIn(rc, (0, 130), out)
        self.last_out = out
        return rc

    def report(self, *extra: str) -> str:
        rc, out = self.cli("report", "--daten", self.data, *extra)
        self.assertEqual(rc, 0, out)
        self.last_out = out
        plans = sorted(glob.glob(os.path.join(self.data, "berichte", "*", "loeschplan.csv")))
        return plans[-1]

    def apply(self, csv_path: str, *extra: str, answer: str = "ja") -> tuple[int, str]:
        return self.cli("apply", csv_path, "--daten", self.data, "--quarantaene", self.quar, *extra,
                        answer=answer)

    def run_ids(self) -> list[str]:
        if not os.path.isdir(self.quar):
            return []
        return sorted(os.listdir(self.quar))

    # ------------------------------------------------------------ Auswertung
    @staticmethod
    def read_csv(path: str) -> list[dict]:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    @staticmethod
    def write_csv(path: str, rows: list[dict], encoding: str = "utf-8-sig", delimiter: str = ";") -> None:
        with open(path, "w", encoding=encoding, newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()), delimiter=delimiter)
            wr.writeheader()
            wr.writerows(rows)

    def plan_rows(self, csv_path: str) -> list[dict]:
        return self.read_csv(csv_path)

    def removed_paths(self, csv_path: str) -> set[str]:
        return {r["Pfad"] for r in self.read_csv(csv_path) if r["Aktion"] == "entfernen"}

    def snapshot(self, top: str | None = None) -> dict[str, str]:
        """relativer Pfad -> SHA-256 aller Dateien unterhalb von ``top`` (Standard: Testbaum)."""
        top = top or self.root
        out = {}
        for dirpath, dirnames, filenames in os.walk(lp(top)):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                with open(full, "rb") as f:
                    out[os.path.relpath(fsutil.strip_prefix(full), top)] = hashlib.sha256(f.read()).hexdigest()
        return out

    def contents(self) -> set[str]:
        return set(self.snapshot().values())

    def quarantine_files(self) -> dict[str, str]:
        out = {}
        for run in self.run_ids():
            rd = os.path.join(self.quar, run)
            for rel, h in self.snapshot(rd).items():
                if not rel.startswith("_dupeclean_lauf"):
                    out[os.path.join(run, rel)] = h
        return out

    def original_location_in_quarantine(self, run: str, path: str) -> str:
        return os.path.join(self.quar, run, fsutil.quarantine_subpath(path))
