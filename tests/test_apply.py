"""Tests für apply / undo / purge: Sicherheit, Umkehrbarkeit, Abbruchfestigkeit."""
from __future__ import annotations

import json
import os
import random
import stat
import subprocess
import sys
import unittest

from helpers import PROJECT, T0, TreeTest

from dupeclean import fsutil
from dupeclean.fsutil import lp


class ApplyBasics(TreeTest):
    def _tree(self):
        self.w("Laptop_A/Fotos/a.jpg", b"A" * 200_000)
        self.w("Laptop_A/Fotos/b.jpg", b"B" * 1000)
        self.w("Laptop_B/Fotos/a.jpg", b"A" * 200_000)
        self.w("Laptop_B/Fotos/b.jpg", b"B" * 1000)
        self.w("Laptop_A/Dokumente/brief.txt", "Hallo Welt")
        self.w("Laptop_B/Dokumente/brief - Kopie.txt", "Hallo Welt")
        self.w("Laptop_B/Dokumente/eigen.txt", "nur hier")
        self.w("Laptop_B/Dokumente/brief.txt", "andere Version")

    def test_probelauf_aendert_nichts(self):
        self._tree()
        before = self.snapshot()
        self.scan()
        plan = self.report()
        rc, out = self.apply(plan)
        self.assertEqual(rc, 0, out)
        self.assertIn("PROBELAUF", out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.quar))

    def test_execute_braucht_bestaetigung(self):
        self._tree()
        before = self.snapshot()
        self.scan()
        rc, out = self.apply(self.report(), "--execute", answer="nein")
        self.assertEqual(rc, 1)
        self.assertEqual(self.snapshot(), before)

    def test_ausfuehren_und_undo_bitgenau(self):
        self._tree()
        before = self.snapshot()
        contents = self.contents()
        self.scan()
        plan = self.report()
        removed = self.removed_paths(plan)
        self.assertEqual(removed, {self.p("Laptop_B/Fotos"), self.p("Laptop_B/Dokumente/brief - Kopie.txt")})
        rc, out = self.apply(plan, "--execute")
        self.assertEqual(rc, 0, out)
        for p in removed:
            self.assertFalse(os.path.exists(p))
        self.assertEqual(self.contents(), contents, "jeder Inhalt muss noch vorhanden sein")
        (run,) = self.run_ids()
        self.assertTrue(os.path.exists(self.original_location_in_quarantine(run, self.p("Laptop_B/Fotos/a.jpg"))))
        rc, out = self.cli("undo", run, "--daten", self.data)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.snapshot(), before, "undo muss alles bitgenau wiederherstellen")
        self.assertEqual(self.quarantine_files(), {})
        with open(os.path.join(self.quar, run, "_dupeclean_lauf", "lauf.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "rueckgaengig")

    def test_mtime_und_attribute_bleiben(self):
        self.w("A/x.txt", "gleich", mtime=T0)
        p = self.w("B - Kopie/x.txt", "gleich", mtime=T0 + 7)
        os.chmod(lp(p), stat.S_IREAD)
        self.scan()
        plan = self.report()
        self.assertEqual(self.removed_paths(plan), {self.p("B - Kopie")})
        self.apply(plan, "--execute")
        (run,) = self.run_ids()
        self.cli("undo", run, "--daten", self.data)
        st = os.stat(p)
        self.assertEqual(int(st.st_mtime), T0 + 7)
        self.assertFalse(st.st_mode & stat.S_IWRITE, "Schreibschutz muss erhalten bleiben")

    def test_purge_nur_nach_bestaetigung(self):
        self._tree()
        self.w("C/ro.txt", "schreibgeschützt")
        ro = self.w("C/ro - Kopie.txt", "schreibgeschützt")
        os.chmod(lp(ro), stat.S_IREAD)
        self.scan()
        self.apply(self.report(), "--execute")
        (run,) = self.run_ids()
        q_before = self.quarantine_files()
        self.assertTrue(q_before)
        rc, _ = self.cli("purge", run, "--daten", self.data, answer="nein")
        self.assertEqual(rc, 1)
        self.assertEqual(self.quarantine_files(), q_before)
        rc, out = self.cli("purge", run, "--daten", self.data, answer="ja")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.quar, run)))
        self.assertTrue(os.path.exists(os.path.join(self.data, "laeufe", run, "journal.jsonl")))
        rc, out = self.cli("runs", "--daten", self.data)
        self.assertIn("endgueltig geloescht", out)

    def test_leere_ordner_entfernen_und_undo(self):
        self.w("A/Musik/lied.mp3", b"M" * 5000)
        self.w("A/eigen.txt", "A")
        self.w("B/Tief/Musik - Kopie/lied.mp3", b"M" * 5000)
        self.w("B/anderes.txt", "B")
        before = self.snapshot()
        self.scan()
        plan = self.report()
        self.assertEqual(self.removed_paths(plan), {self.p("B/Tief/Musik - Kopie")})
        rc, out = self.apply(plan, "--execute", "--leere-ordner")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.p("B/Tief")))
        self.assertTrue(os.path.exists(self.p("B")))
        (run,) = self.run_ids()
        self.cli("undo", run, "--daten", self.data)
        self.assertEqual(self.snapshot(), before)

    def test_langer_pfad_und_umlaute(self):
        deep = "/".join(["Ordner_äöü_mit_langem_Namen_" + str(i) for i in range(10)])
        a = self.w(f"A/{deep}/Größe.txt", "ß" * 500)
        b = self.w(f"B - Kopie/{deep}/Größe.txt", "ß" * 500)
        self.w("A/eigen.txt", "A")
        self.assertGreater(len(b), 260)
        before = self.snapshot()
        self.scan()
        plan = self.report()
        rc, out = self.apply(plan, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(lp(a)))
        self.assertFalse(os.path.exists(lp(b)))
        (run,) = self.run_ids()
        self.cli("undo", run, "--daten", self.data)
        self.assertEqual(self.snapshot(), before)


class SafetyChecks(TreeTest):
    def _pair(self, n: int = 3):
        for i in range(n):
            self.w(f"A/f{i}.txt", f"Inhalt {i} " * 50)
            self.w(f"B/f{i} - Kopie.txt", f"Inhalt {i} " * 50)
        self.w("A/eigen.txt", "nur A")
        self.w("B/eigen.txt", "nur B")

    def test_veraenderte_datei_wird_uebersprungen(self):
        self._pair()
        self.scan()
        plan = self.report()
        target = self.p("B/f0 - Kopie.txt")
        st = os.stat(target)
        with open(target, "r+b") as f:  # Inhalt ändern, Größe und Datum gleich lassen
            f.write(b"X")
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        rc, out = self.apply(plan, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(target), "veränderte Datei darf nicht verschoben werden")
        self.assertIn("Inhalt hat sich seit dem Scan verändert", out)
        self.assertFalse(os.path.exists(self.p("B/f1 - Kopie.txt")))

    def test_geaendertes_datum_wird_uebersprungen(self):
        self._pair()
        self.scan()
        plan = self.report()
        os.utime(self.p("B/f1 - Kopie.txt"), (T0 + 99999, T0 + 99999))
        rc, out = self.apply(plan, "--execute")
        self.assertTrue(os.path.exists(self.p("B/f1 - Kopie.txt")))

    def test_original_fehlt(self):
        self._pair()
        self.scan()
        plan = self.report()
        os.remove(self.p("A/f2.txt"))
        rc, out = self.apply(plan, "--execute")
        self.assertTrue(os.path.exists(self.p("B/f2 - Kopie.txt")), "letzte Kopie darf nie entfernt werden")
        self.assertIn("Original nicht bestätigt", out)

    def test_original_inhalt_veraendert_gruendlich(self):
        self._pair()
        self.scan()
        plan = self.report()
        o = self.p("A/f0.txt")
        st = os.stat(o)
        with open(o, "r+b") as f:
            f.write(b"Y")
        os.utime(o, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.apply(plan, "--execute", "--gruendlich")
        self.assertTrue(os.path.exists(self.p("B/f0 - Kopie.txt")))

    def test_alle_auf_entfernen_wird_abgelehnt(self):
        self._pair(1)
        self.scan()
        plan = self.report()
        rows = self.read_csv(plan)
        for r in rows:
            r["Aktion"] = "entfernen"
        self.write_csv(plan, rows)
        before = self.snapshot()
        rc, out = self.apply(plan, "--execute")
        self.assertIn("es bliebe keine Kopie übrig", out)
        self.assertEqual(self.snapshot(), before)

    def test_original_in_entferntem_ordner_wird_abgelehnt(self):
        # Ordnergruppe: X == Y. Dateigruppe: Y/... wäre Original für Z/datei.
        for top in ("X", "Y"):
            self.w(f"{top}/sub/a.bin", b"a" * 3000)
            self.w(f"{top}/sub/b.bin", b"b" * 3000)
        self.w("Z/a.bin", b"a" * 3000)
        self.w("Z/eigen.txt", "Z")
        self.scan()
        plan = self.report()
        rows = self.read_csv(plan)
        # Plan von Hand "verdrehen": beide Ordner entfernen
        for r in rows:
            if r["Typ"] == "Ordner":
                r["Aktion"] = "entfernen"
        self.write_csv(plan, rows)
        contents = self.contents()
        rc, out = self.apply(plan, "--execute")
        self.assertIn("es bliebe keine Kopie übrig", out)
        self.assertEqual(self.contents(), contents)
        self.assertTrue(os.path.isdir(self.p("X")) or os.path.isdir(self.p("Y")))

    def test_excel_csv_cp1252_und_trennzeichen(self):
        self.w("Müller/ä.txt", "ä" * 100)
        self.w("Müller - Kopie/ä.txt", "ä" * 100)
        self.scan()
        plan = self.report()
        rows = self.read_csv(plan)
        self.write_csv(plan, rows, encoding="cp1252", delimiter=";")
        rc, out = self.apply(plan, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.p("Müller - Kopie")))
        self.assertTrue(os.path.exists(self.p("Müller/ä.txt")))

    def test_geschuetzt_kann_nicht_per_csv_entfernt_werden(self):
        self.w("Repo/.git/HEAD", "ref")
        self.w("Repo/code.py", "print(1)" * 20)
        self.w("Lose/code.py", "print(1)" * 20)
        self.scan()
        plan = self.report()
        rows = self.read_csv(plan)
        for r in rows:
            r["Aktion"] = "behalten" if r["Pfad"].startswith(self.p("Lose")) else "entfernen"
        self.write_csv(plan, rows)
        self.apply(plan, "--execute")
        self.assertTrue(os.path.exists(self.p("Repo/code.py")))


class NeverLastCopy(TreeTest):
    """Zufällige Bäume und zufällig geänderte Löschpläne: nie verschwindet ein Inhalt."""

    def _random_tree(self, rnd: random.Random):
        blobs = [bytes([rnd.randrange(256)]) * rnd.randrange(1, 4000) for _ in range(12)]
        blobs.append(b"")
        dirs = ["L1", "L1/a", "L1/a/b", "L2", "L2/a", "L2/a/b", "L3/x", "L3/x - Kopie", "L4/.git", "L4"]
        for i in range(rnd.randrange(15, 40)):
            d = rnd.choice(dirs)
            name = rnd.choice(["f", "g", "Kopie von f", "h (1)", "x"]) + f"{rnd.randrange(4)}.dat"
            self.w(f"{d}/{name}", rnd.choice(blobs), mtime=T0 + rnd.randrange(1000))
        # vollständig kopierte Ordner erzwingen
        snap = self.snapshot(self.p("L1"))
        for rel in snap:
            src = os.path.join(self.p("L1"), rel)
            with open(src, "rb") as f:
                self.w(f"L5/copy/{rel}", f.read(), mtime=int(os.stat(src).st_mtime))

    def test_zufall(self):
        moved_total = 0
        for seed in range(25):
            with self.subTest(seed=seed):
                fsutil.remove_tree(self.root)
                fsutil.remove_tree(self.data)
                fsutil.remove_tree(self.quar)
                os.makedirs(self.root)
                rnd = random.Random(seed)
                self._random_tree(rnd)
                contents = self.contents()
                before = self.snapshot()
                self.scan()
                plan = self.report()
                rows = self.read_csv(plan)
                if seed % 3:
                    for r in rows:  # zufällige Handänderungen
                        if rnd.random() < 0.5:
                            r["Aktion"] = rnd.choice(["entfernen", "behalten"])
                    self.write_csv(plan, rows)
                rc, out = self.apply(plan, "--execute", "--leere-ordner")
                self.assertIn(rc, (0, 1), out)
                self.assertEqual(self.contents(), contents, f"Inhalt verloren (seed {seed})\n{out}")
                moved_total += len(self.quarantine_files())
                for run in self.run_ids():
                    rc, out = self.cli("undo", run, "--daten", self.data)
                    self.assertEqual(rc, 0, out)
                self.assertEqual(self.snapshot(), before)
        self.assertGreater(moved_total, 100, "der Test muss tatsächlich Dateien verschieben")


class Interruption(TreeTest):
    def _many(self):
        for i in range(12):
            self.w(f"A/datei{i:02d}.bin", bytes([i]) * (2000 + i))
            self.w(f"B/datei{i:02d} - Kopie.bin", bytes([i]) * (2000 + i))
        self.w("A/eigen.txt", "A")
        self.w("B/eigen.txt", "B")

    def _check_consistent(self, before: dict, run: str):
        """Jede ursprüngliche Datei liegt genau einmal vor: am Ort oder in der Quarantäne."""
        now = self.snapshot()
        q = self.quarantine_files()
        for rel, h in before.items():
            path = os.path.join(self.root, rel)
            qrel = os.path.join(run, fsutil.quarantine_subpath(path))
            in_tree = rel in now
            in_q = qrel in q
            self.assertTrue(in_tree != in_q, f"{rel}: Baum={in_tree}, Quarantäne={in_q}")
            self.assertEqual(now.get(rel) or q.get(qrel), h)

    def test_abbruch_mitten_im_lauf(self):
        self._many()
        before = self.snapshot()
        self.scan()
        plan = self.report()
        os.environ["DUPECLEAN_TEST_ABORT_APPLY_AFTER"] = "4"
        rc, out = self.apply(plan, "--execute")
        del os.environ["DUPECLEAN_TEST_ABORT_APPLY_AFTER"]
        self.assertEqual(rc, 130, out)
        self.assertIn("ABGEBROCHEN", out)
        (run,) = self.run_ids()
        self._check_consistent(before, run)
        self.assertEqual(len(self.quarantine_files()), 4)
        # Fortsetzen mit neuem Lauf ist möglich; bereits verschobene werden übersprungen
        rc, out = self.apply(plan, "--execute")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.contents(), set(before.values()))
        for r in self.run_ids():
            self.assertEqual(self.cli("undo", r, "--daten", self.data)[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_absturz_direkt_nach_verschieben(self):
        self._many()
        before = self.snapshot()
        self.scan()
        plan = self.report()
        env = dict(os.environ, DUPECLEAN_TEST_CRASH_AFTER_MOVE="3", PYTHONIOENCODING="utf-8")
        r = subprocess.run([sys.executable, os.path.join(PROJECT, "dupeclean.py"), "apply", plan,
                            "--daten", self.data, "--quarantaene", self.quar, "--execute"],
                           input="ja\n", capture_output=True, text=True, encoding="utf-8", env=env)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        (run,) = self.run_ids()
        self._check_consistent(before, run)
        with open(os.path.join(self.quar, run, "_dupeclean_lauf", "lauf.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["status"], "laeuft")
        rc, out = self.cli("undo", run, "--daten", self.data)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.snapshot(), before)

    def test_harter_abbruch_waehrend_scan(self):
        for i in range(300):
            self.w(f"A/{i:03d}.bin", bytes([i % 256]) * (5000 + i))
            self.w(f"B/{i:03d}.bin", bytes([i % 256]) * (5000 + i))
        env = dict(os.environ, DUPECLEAN_TEST_HASH_DELAY="0.01", PYTHONIOENCODING="utf-8")
        p = subprocess.Popen([sys.executable, os.path.join(PROJECT, "dupeclean.py"), "scan", self.root,
                              "--daten", self.data], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # sobald die Hash-Phase läuft, Prozess hart beenden
        import sqlite3
        import time
        dbp = os.path.join(self.data, "dupeclean.sqlite")
        deadline = time.time() + 30
        while time.time() < deadline and p.poll() is None:
            con = None
            try:
                con = sqlite3.connect(dbp, timeout=1)
                n = con.execute("SELECT COUNT(*) FROM files WHERE partial IS NOT NULL").fetchone()[0]
                if n > 20:
                    break
            except sqlite3.Error:
                pass
            finally:
                if con is not None:
                    con.close()
            time.sleep(0.01)
        self.assertIsNone(p.poll(), "Scan sollte noch laufen")
        p.kill()
        p.communicate()
        con = sqlite3.connect(dbp)
        phase = json.loads(con.execute("SELECT value FROM meta WHERE key='scan'").fetchone()[0])["phase"]
        con.close()
        self.assertEqual(phase, "hash", "Abbruch muss mitten in der Hash-Phase erfolgt sein")
        self.assertEqual(self.scan(), 0, "nach hartem Abbruch muss ein neuer Scan funktionieren")
        plan = self.report()
        self.assertEqual(len(self.removed_paths(plan)), 1)  # Ordner B == Ordner A


if __name__ == "__main__":
    unittest.main()
