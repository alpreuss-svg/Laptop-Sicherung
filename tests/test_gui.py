"""Test der Fensteroberfläche: kompletter Ablauf per Programm gesteuert (ohne Klicks)."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from helpers import TreeTest

try:
    import tkinter as tk
    tk.Tk().destroy()
    HAVE_TK = True
except Exception:  # pragma: no cover - ohne Anzeige
    HAVE_TK = False


@unittest.skipUnless(HAVE_TK, "keine grafische Oberfläche verfügbar")
class GuiFlow(TreeTest):
    def setUp(self):
        super().setUp()
        from dupeclean import gui
        self.gui = gui
        self.tk = tk.Tk()
        self.tk.withdraw()
        self.app = gui.App(self.tk, data_dir=self.data, quarantine=self.quar)
        self.patches = [mock.patch("os.startfile", create=True),
                        mock.patch("tkinter.messagebox.askyesno", return_value=True),
                        mock.patch("tkinter.messagebox.showinfo"),
                        mock.patch("tkinter.messagebox.showwarning")]
        self.mocks = [p.start() for p in self.patches]

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.app.close()
        super().tearDown()

    def wait(self, timeout: float = 60) -> None:
        end = time.time() + timeout
        self.tk.update()
        while self.app.busy and time.time() < end:
            self.tk.update()
            time.sleep(0.02)
        self.tk.update()
        self.assertFalse(self.app.busy, "Vorgang hängt")

    def text(self) -> str:
        return self.app.txt.get("1.0", "end")

    def test_kompletter_ablauf(self):
        self.w("Laptop_A/Bilder/a.jpg", b"A" * 300_000)
        self.w("Laptop_A/Bilder/b.jpg", b"B" * 2000)
        self.w("Laptop_B/Bilder/a.jpg", b"A" * 300_000)
        self.w("Laptop_B/Bilder/b.jpg", b"B" * 2000)
        self.w("Laptop_B/Dokumente/brief - Kopie.txt", "Hallo")
        self.w("Laptop_A/Dokumente/brief.txt", "Hallo")
        before = self.snapshot()
        app = self.app
        app.folders = [self.root]
        app.var_prefer.set(self.p("Laptop_A"))
        app._refresh()
        self.assertEqual(str(app.btn_dry.cget("state")), "disabled")

        app._step_scan()
        self.wait()
        self.assertIn("Suche abgeschlossen", self.text())
        self.assertIn("Einsparbar", self.text())
        self.assertTrue(os.path.isfile(app.plan_csv))
        self.assertEqual(str(app.btn_exec.cget("state")), "disabled", "erst nach dem Probelauf")
        self.mocks[0].assert_called()  # Bericht wurde geöffnet

        app._step_dry()
        self.wait()
        self.assertIn("Probelauf abgeschlossen", self.text())
        self.assertIn("Würde verschieben: 2 Einträge", self.text())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(str(app.btn_exec.cget("state")), "normal")

        app._step_exec()
        self.wait()
        self.assertIn("Verschieben abgeschlossen", self.text())
        self.assertFalse(os.path.exists(self.p("Laptop_B/Bilder")))
        self.assertTrue(os.path.exists(self.p("Laptop_A/Bilder/a.jpg")))
        self.assertTrue(app.last_run)

        app._open_runs()
        self.tk.update()
        self.assertEqual(len(app.runs_tv.get_children()), 1)

        app._undo_last()
        self.wait()
        self.assertIn("rückgängig gemacht", self.text())
        self.assertEqual(self.snapshot(), before)

    def test_stopp_waehrend_suche(self):
        for i in range(200):
            self.w(f"A/{i:03d}.bin", bytes([i]) * 4000)
            self.w(f"B/{i:03d}.bin", bytes([i]) * 4000)
        os.environ["DUPECLEAN_TEST_HASH_DELAY"] = "0.02"
        try:
            self.app.folders = [self.root]
            self.app._step_scan()
            t = time.time()
            while time.time() - t < 1.0:
                self.tk.update()
                time.sleep(0.02)
            self.app._stop()
            self.wait()
        finally:
            del os.environ["DUPECLEAN_TEST_HASH_DELAY"]
        self.assertIn("Suche angehalten", self.text())
        self.app._step_scan()
        self.wait()
        self.assertIn("Suche abgeschlossen", self.text())

    def test_einstellungen_bleiben_erhalten(self):
        self.app.folders = [self.root]
        self.app.var_ssd.set(True)
        self.app.var_prefer.set(self.p("A"))
        self.app._save_settings()
        other = self.gui.App(tk.Toplevel(self.tk), data_dir=self.data, quarantine=self.quar)
        self.assertEqual(other.folders, [self.root])
        self.assertTrue(other.var_ssd.get())
        self.assertEqual(other.var_prefer.get(), self.p("A"))


if __name__ == "__main__":
    unittest.main()
