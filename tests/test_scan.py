"""Tests für scan und report: Erkennung von Duplikaten, Sonderfällen und Regeln."""
from __future__ import annotations

import os
import sqlite3
import stat
import unittest

from helpers import T0, TreeTest, can_symlink, make_junction

from dupeclean import fsutil, hashing
from dupeclean.fsutil import lp


class DuplicateDetection(TreeTest):
    def test_gleicher_name_anderer_inhalt(self):
        self.w("A/bericht.docx", "Version 1")
        self.w("B/bericht.docx", "Version 2 – länger")
        self.scan()
        plan = self.report()
        self.assertEqual(self.removed_paths(plan), set())
        self.assertIn("Namenskonflikte (gleicher Name, anderer Inhalt): 1", self.last_out)

    def test_gleicher_inhalt_anderer_name(self):
        self.w("A/urlaub.jpg", b"x" * 5000)
        self.w("B/IMG_0001.jpg", b"x" * 5000)
        self.scan()
        plan = self.report()
        self.assertEqual(len(self.removed_paths(plan)), 1)

    def test_gleiche_groesse_anderer_inhalt(self):
        self.w("A/a.bin", b"a" * 4096)
        self.w("B/b.bin", b"b" * 4096)
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())

    def test_unterschied_nur_in_der_mitte(self):
        size = 1024 * 1024
        a = bytearray(b"m" * size)
        b = bytearray(a)
        b[size // 2] = ord("X")
        self.w("A/gross.bin", bytes(a))
        self.w("B/gross.bin", bytes(b))
        self.w("C/gross.bin", bytes(a))
        self.scan()
        con = sqlite3.connect(os.path.join(self.data, "dupeclean.sqlite"))
        partials = {r[0] for r in con.execute("SELECT partial FROM files")}
        fulls = {r[0] for r in con.execute("SELECT full FROM files")}
        con.close()
        self.assertEqual(len(partials), 1, "Teil-Hash muss gleich sein (nur die Mitte unterscheidet sich)")
        self.assertEqual(len(fulls), 2, "Voll-Hash muss den Unterschied erkennen")
        removed = self.removed_paths(self.report())
        self.assertEqual(len(removed), 1)
        self.assertNotIn(self.p("B/gross.bin"), removed)

    def test_null_byte_dateien(self):
        self.w("A/leer.txt", b"")
        self.w("B/leer.txt", b"")
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())

    def test_ausschluesse(self):
        for d in ("A", "B"):
            self.w(f"{d}/Thumbs.db", d * 100)
            self.w(f"{d}/desktop.ini", d * 50)
            self.w(f"{d}/~$brief.docx", b"lock")
            self.w(f"{d}/foto.jpg", b"F" * 3000)
        self.w("$RECYCLE.BIN/geloescht.jpg", b"F" * 3000)
        self.scan()
        rows = self.plan_rows(self.report())
        paths = {r["Pfad"] for r in rows}
        self.assertFalse(any("Thumbs.db" in p or "desktop.ini" in p or "~$" in p or "RECYCLE" in p
                             for p in paths), paths)
        # A und B unterscheiden sich nur durch Systemdateien -> gelten als identische Ordner
        self.assertEqual({r["Typ"] for r in rows}, {"Ordner"})

    def test_hardlink_kein_duplikat(self):
        src = self.w("A/datei.bin", b"h" * 10000)
        os.makedirs(self.p("B"))
        os.link(lp(src), lp(self.p("B/datei_link.bin")))
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())
        # Mit einer echten dritten Kopie: nur die echte Kopie wird entfernt, Hardlinks bleiben
        self.w("C/kopie.bin", b"h" * 10000)
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertEqual(removed, {self.p("C/kopie.bin")})

    def test_junction_wird_nicht_verfolgt(self):
        self.w("A/foto.jpg", b"J" * 3000)
        os.makedirs(self.p("B"))
        if not make_junction(self.p("B/link_auf_A"), self.p("A")):
            self.skipTest("Junction konnte nicht angelegt werden")
        self.scan()
        con = sqlite3.connect(os.path.join(self.data, "dupeclean.sqlite"))
        n = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        links = con.execute("SELECT COUNT(*) FROM errors WHERE op='verknuepfung'").fetchone()[0]
        con.close()
        self.assertEqual(n, 1)
        self.assertEqual(links, 1)
        self.assertEqual(self.removed_paths(self.report()), set())

    @unittest.skipUnless(can_symlink(), "Symlinks erfordern unter Windows den Entwicklermodus oder Adminrechte")
    def test_symlink_wird_nicht_verfolgt(self):
        f = self.w("A/foto.jpg", b"S" * 3000)
        os.makedirs(self.p("B"))
        os.symlink(f, self.p("B/foto_link.jpg"))
        os.symlink(self.p("A"), self.p("B/ordner_link"), target_is_directory=True)
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())

    def test_langer_pfad(self):
        deep = "/".join(["Unterordner_mit_langem_Namen_" + str(i) for i in range(12)])
        a = self.w(f"A/{deep}/datei.txt", b"lang" * 1000)
        b = self.w(f"B/{deep}/datei_kopie.txt", b"lang" * 1000)
        self.assertGreater(len(a), 260)
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed <= {a, b})

    def test_umlaute_und_unicode(self):
        self.w("Müller/Größe_Ärger_ß.txt", "Inhalt äöü")
        self.w("Jürgen/Übersicht 😀.txt", "Inhalt äöü")
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertEqual(len(removed), 1)

    def test_schreibgeschuetzt(self):
        a = self.w("A/ro.txt", b"readonly" * 100)
        self.w("B - Kopie/ro.txt", b"readonly" * 100)
        os.chmod(lp(self.p("B - Kopie/ro.txt")), stat.S_IREAD)
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), {self.p("B - Kopie")})
        self.assertTrue(os.path.exists(a))

    def test_gesperrte_datei(self):
        import msvcrt
        self.w("A/frei.bin", b"f" * 1000)
        self.w("B/frei.bin", b"f" * 1000)
        locked = self.w("A/gesperrt.bin", b"g" * 1500)
        self.w("B/gesperrt.bin", b"g" * 1500)
        fh = open(locked, "r+b")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1500)
            rc = self.scan()
            self.assertEqual(rc, 0)
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1500)
            fh.close()
        con = sqlite3.connect(os.path.join(self.data, "dupeclean.sqlite"))
        errs = [r[0] for r in con.execute("SELECT path FROM errors WHERE op='lesen'")]
        con.close()
        self.assertEqual(errs, [locked])
        removed = self.removed_paths(self.report())
        # Ordner A ist wegen des Lesefehlers nicht vollständig bekannt -> nur Einzeldatei
        self.assertEqual(len(removed), 1)
        self.assertTrue(list(removed)[0].endswith("frei.bin"))


class FolderDuplicates(TreeTest):
    def _tree(self, top: str, tag: bytes = b"") -> None:
        self.w(f"{top}/Fotos/2019/a.jpg", b"a" * 3000 + tag)
        self.w(f"{top}/Fotos/2019/b.jpg", b"b" * 3000 + tag)
        self.w(f"{top}/Fotos/2020/c.jpg", b"c" * 3000 + tag)
        self.w(f"{top}/Fotos/leer.txt", b"")

    def test_identische_ordner_nur_oberste_ebene(self):
        self._tree("Laptop_A/Eigene")
        self._tree("Laptop_B/Eigene")
        self.w("Laptop_A/einzig.txt", "nur A")
        self.w("Laptop_B/einzig.txt", "nur B")
        self.scan()
        rows = self.plan_rows(self.report())
        self.assertEqual({r["Typ"] for r in rows}, {"Ordner"})
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["Pfad"] for r in rows},
                         {self.p("Laptop_A/Eigene"), self.p("Laptop_B/Eigene")})

    def test_verschachtelte_identische_ordner(self):
        # A == B (ganz), und A/Fotos == C/Fotos (C sonst anders)
        self._tree("A")
        self._tree("B")
        self._tree("C")
        self.w("C/nur_c.txt", "C")
        self.w("A/x.txt", "gemeinsam")
        self.w("B/x.txt", "gemeinsam")
        self.scan()
        rows = self.plan_rows(self.report())
        groups = {}
        for r in rows:
            groups.setdefault(r["Gruppe"], []).append(r)
        paths = {r["Pfad"] for r in rows}
        self.assertIn(self.p("A"), paths)
        self.assertIn(self.p("B"), paths)
        self.assertIn(self.p("C/Fotos"), paths)
        # Unterordner von B werden nicht zusätzlich aufgeführt
        self.assertFalse(any(fsutil.is_strictly_within(p, self.p("B")) for p in paths))
        self.assertTrue(all(r["Typ"] == "Ordner" for r in rows))

    def test_null_byte_datei_zaehlt_im_ordnervergleich(self):
        self._tree("A")
        self._tree("B")
        self.w("B/Fotos/zusaetzlich_leer.txt", b"")
        self.scan()
        rows = self.plan_rows(self.report())
        paths = {r["Pfad"] for r in rows}
        self.assertNotIn(self.p("A/Fotos"), paths)
        # die Unterordner sind weiterhin identisch
        self.assertIn(self.p("B/Fotos/2019"), paths)

    def test_teilmenge(self):
        self._tree("Gross")
        self.w("Gross/Fotos/2021/d.jpg", b"d" * 3000)
        self.w("Gross/Fotos/2020/e.jpg", b"e" * 3000)
        self.w("Klein/a.jpg", b"a" * 3000)
        self.w("Klein/c.jpg", b"c" * 3000)
        self.scan()
        self.report()
        self.assertIn("Ordner, deren Inhalt in einem anderen enthalten ist: 1", self.last_out)
        from dupeclean import config, db
        from dupeclean.analyze import Analyzer
        con = db.connect(self.data)
        a = Analyzer(con, config.load()).run()
        con.close()
        self.assertEqual(a.subsets[0]["path"], self.p("Klein"))
        self.assertEqual(a.subsets[0]["container"], self.p("Gross\\Fotos"))


class ProtectedStructures(TreeTest):
    def _repo(self, top: str, extra: str = "") -> None:
        self.w(f"{top}/.git/HEAD", "ref: refs/heads/main")
        self.w(f"{top}/.git/config", "[core]")
        self.w(f"{top}/src/main.py", "print('hallo')" + extra)
        self.w(f"{top}/README.md", "# Projekt")

    def test_dateien_im_repo_werden_nicht_ausgeduennt(self):
        self._repo("A/projekt")
        self.w("B/lose/main.py", "print('hallo')")
        self.w("B/lose/README.md", "# Projekt")
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertFalse(any(fsutil.is_within(p, self.p("A/projekt")) for p in removed))
        # Freie Kopien außerhalb dürfen entfernt werden (geschützte Kopie bleibt Original)
        self.assertEqual(removed, {self.p("B/lose/main.py"), self.p("B/lose/README.md")})

    def test_repo_nur_als_ganzes(self):
        self._repo("A/projekt")
        self._repo("B/projekt")
        self.scan()
        rows = self.plan_rows(self.report())
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["Typ"] for r in rows}, {"Ordner"})

    def test_abweichende_repos_bleiben_unangetastet(self):
        self._repo("A/projekt")
        self._repo("B/projekt", extra="# geändert")
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())

    def test_programmordner(self):
        for top in ("A/Programm", "B/Programm2"):
            self.w(f"{top}/app.exe", b"MZ" * 500)
            self.w(f"{top}/lib.dll", b"DL" * 500)
        self.w("B/Programm2/config.ini", "anders")
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), set())


class KeepRules(TreeTest):
    def test_kopie_wird_abgewertet(self):
        self.w("A/brief - Kopie.txt", "x" * 100, mtime=T0)        # älter, aber "Kopie"
        self.w("A/brief.txt", "x" * 100, mtime=T0 + 100)
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), {self.p("A/brief - Kopie.txt")})

    def test_aeltestes_datum(self):
        self.w("A/neu.txt", "y" * 100, mtime=T0 + 1000)
        self.w("B/alt.txt", "y" * 100, mtime=T0)
        self.scan()
        self.assertEqual(self.removed_paths(self.report()), {self.p("A/neu.txt")})

    def test_pfadtiefe_dann_laenge(self):
        self.w("A/tief/tiefer/z.txt", "z" * 100, mtime=T0)
        self.w("B/z.txt", "z" * 100, mtime=T0)
        self.w("Clang/z.txt", "z" * 100, mtime=T0)
        for i, d in enumerate(("A/tief/tiefer", "B", "Clang")):  # sonst wären es identische Ordner
            self.w(f"{d}/eigen.txt", str(i))
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertEqual(removed, {self.p("A/tief/tiefer/z.txt"), self.p("Clang/z.txt")})

    def test_bevorzugter_pfad(self):
        self.w("Laptop_Alt/doc.txt", "d" * 100, mtime=T0)
        self.w("Laptop_Neu/tief/doc - Kopie.txt", "d" * 100, mtime=T0 + 999)
        self.scan()
        removed = self.removed_paths(self.report("--prefer", self.p("Laptop_Neu")))
        self.assertEqual(removed, {self.p("Laptop_Alt/doc.txt")})

    def test_downloads_und_temp_abgewertet(self):
        self.w("Downloads/setup.zip", "s" * 100, mtime=T0)
        self.w("Software/setup.zip", "s" * 100, mtime=T0 + 5)
        self.w("Template/vorlage.dotx", "t" * 100, mtime=T0)  # "Temp" darf nicht in "Template" treffen
        self.w("Vorlagen/vorlage.dotx", "t" * 100, mtime=T0 + 5)
        for i, d in enumerate(("Downloads", "Software", "Template", "Vorlagen")):
            self.w(f"{d}/eigen.txt", str(i))
        self.scan()
        removed = self.removed_paths(self.report())
        self.assertEqual(removed, {self.p("Downloads/setup.zip"), self.p("Vorlagen/vorlage.dotx")})

    def test_konfigurierbare_reihenfolge(self):
        cfg = os.path.join(self.base, "eigene.toml")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('[behalten]\nreihenfolge = ["laenge"]\n')
        self.w("A/sehr_langer_name.txt", "q" * 100, mtime=T0)
        self.w("B/k.txt", "q" * 100, mtime=T0 + 99)
        self.scan()
        removed = self.removed_paths(self.report("--config", cfg))
        self.assertEqual(removed, {self.p("A/sehr_langer_name.txt")})


class ScanResume(TreeTest):
    def _make(self):
        for i in range(40):
            self.w(f"A/f{i:03d}.bin", bytes([i]) * (3000 + i))
            self.w(f"B/f{i:03d}.bin", bytes([i]) * (3000 + i))
        self.w("C/gross.bin", b"G" * (300 * 1024))
        self.w("D/gross.bin", b"G" * (300 * 1024))

    def test_abbruch_und_fortsetzen(self):
        self._make()
        os.environ["DUPECLEAN_TEST_ABORT_SCAN_AFTER"] = "15"
        self.assertEqual(self.scan(), 130)
        self.assertIn("setzt fort", self.last_out)
        del os.environ["DUPECLEAN_TEST_ABORT_SCAN_AFTER"]
        self.assertEqual(self.scan(), 0)
        self.assertIn("Setze unterbrochenen Scan fort", self.last_out)
        plan_resumed = self.read_csv(self.report())
        # Vergleich mit einem frischen Scan in einem anderen Arbeitsordner
        self.data = os.path.join(self.base, "daten2")
        self.scan()
        plan_fresh = self.read_csv(self.report())
        strip = lambda rows: sorted((r["Gruppe"], r["Aktion"], r["Pfad"]) for r in rows)  # noqa: E731
        self.assertEqual(strip(plan_resumed), strip(plan_fresh))

    def test_cache_beschleunigt_wiederholung(self):
        self._make()
        self.scan()
        self.scan()
        self.assertIn("aus dem Cache", self.last_out)
        self.assertIn("Stufe 3: 0 Dateien", self.last_out)
        self.assertIn("(82 aus dem Cache)", self.last_out)


class Hashing(unittest.TestCase):
    def test_teil_und_vollhash_klein_identisch(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="dupeclean_test_")
        try:
            p = os.path.join(d, "k.bin")
            with open(p, "wb") as f:
                f.write(b"klein" * 100)
            part, full, *_ = hashing.partial_hash(p, 500)
            self.assertEqual(part, full)
            self.assertEqual(full, hashing.full_hash(p, 500)[0])
        finally:
            fsutil.remove_tree(d)


if __name__ == "__main__":
    unittest.main()
