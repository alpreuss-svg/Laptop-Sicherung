"""Geschwindigkeitsmessung mit einem großen, erzeugten Testbaum (Standard: 100 000 Dateien).

Aufruf:  python tests\\benchmark.py [ANZAHL] [--behalten]
Der Baum entsteht im Temp-Ordner und wird danach gelöscht (außer mit --behalten).
"""
from __future__ import annotations

import contextlib
import io
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dupeclean import fsutil  # noqa: E402
from dupeclean.cli import main  # noqa: E402


def build_tree(root: str, n_files: int, rnd: random.Random) -> tuple[int, int]:
    """Fünf 'Laptops'; ein Teil der Ordner ist zwischen ihnen kopiert, dazu verstreute Einzelkopien."""
    per_dir = 50
    n_dirs = n_files // per_dir
    laptops = [f"Laptop_{i}" for i in range(1, 6)]
    blobs: list[bytes] = []
    written = 0
    total = 0
    templates = []  # Ordnervorlagen, die mehrfach vorkommen
    for d in range(n_dirs):
        if templates and rnd.random() < 0.35:
            tpl = rnd.choice(templates)  # ganzer Ordner doppelt
        else:
            tpl = []
            for i in range(per_dir):
                if blobs and rnd.random() < 0.15:
                    data = rnd.choice(blobs)  # einzelne doppelte Datei
                else:
                    size = int(rnd.expovariate(1 / 8000)) + 1
                    data = os.urandom(size)
                    if len(blobs) < 20000:
                        blobs.append(data)
                tpl.append((f"datei_{i:03d}.dat", data))
            templates.append(tpl)
        lap = rnd.choice(laptops)
        path = os.path.join(root, lap, f"Bereich_{d % 37:02d}", f"Ordner_{d:05d}")
        os.makedirs(path, exist_ok=True)
        for name, data in tpl:
            if written >= n_files:
                break
            with open(os.path.join(path, name), "wb") as f:
                f.write(data)
            written += 1
            total += len(data)
    # einige große Dateien (je zweimal) für die Lesegeschwindigkeit
    for i in range(10):
        data = os.urandom(20 * 1024 * 1024)
        for lap in ("Laptop_1", "Laptop_2"):
            p = os.path.join(root, lap, "Videos")
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, f"video_{i}.mp4"), "wb") as f:
                f.write(data)
            written += 1
            total += len(data)
    return written, total


def timed(label: str, argv: list[str], results: list) -> str:
    buf = io.StringIO()
    t = time.perf_counter()
    with contextlib.redirect_stdout(buf):
        rc = main(argv, confirm=lambda _p: "nein")
    dt = time.perf_counter() - t
    results.append((label, dt, rc))
    print(f"  {label:42s} {dt:8.1f} s  (rc={rc})", flush=True)
    return buf.getvalue()


def main_bench() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 100_000
    keep = "--behalten" in sys.argv
    base = tempfile.mkdtemp(prefix="dupeclean_bench_")
    os.environ["DUPECLEAN_HOME"] = os.path.join(base, "home")
    os.environ["DUPECLEAN_QUIET"] = "1"
    root = os.path.join(base, "Sicherung")
    data = os.path.join(base, "daten")
    print(f"Erzeuge Testbaum mit {n} Dateien in {base} …", flush=True)
    t = time.perf_counter()
    count, total = build_tree(root, n, random.Random(42))
    print(f"  {count} Dateien, {fsutil.fmt_size(total)} in {time.perf_counter() - t:.1f} s", flush=True)
    results: list = []
    try:
        out1 = timed("scan (erster Lauf)", ["scan", root, "--daten", data], results)
        timed("scan (Wiederholung, Hash-Cache)", ["scan", root, "--daten", data], results)
        timed("scan --threads 4 --neu (Cache)", ["scan", root, "--daten", data, "--threads", "4", "--neu"],
              results)
        out_r = timed("report (Analyse, HTML, CSV)", ["report", "--daten", data], results)
        csv_path = next(line.split("Löschplan:", 1)[1].strip() for line in out_r.splitlines()
                        if line.startswith("Löschplan:"))
        timed("apply (Probelauf)", ["apply", csv_path, "--daten", data], results)
        print()
        print("Scan-Ergebnis:")
        for line in out1.splitlines():
            if line.startswith(("Scan abgeschlossen", "Gruppen identischer")):
                print("  " + line)
        print("Bericht:")
        for line in out_r.splitlines():
            if line.startswith("  "):
                print(line)
        html = os.path.join(os.path.dirname(csv_path), "bericht.html")
        print(f"  Größe bericht.html: {fsutil.fmt_size(os.path.getsize(html))}, "
              f"loeschplan.csv: {fsutil.fmt_size(os.path.getsize(csv_path))}")
        scan_s = results[0][1]
        print(f"\nErster Scan: {fsutil.fmt_int(round(count / scan_s))} Dateien/s bzw. "
              f"{fsutil.fmt_size(total / scan_s)}/s bezogen auf die Gesamtdatenmenge "
              f"(Daten lagen im Windows-Dateicache; eine USB-HDD ist langsamer)")
    finally:
        if keep:
            print(f"Testbaum bleibt erhalten: {base}")
        else:
            fsutil.remove_tree(base)


if __name__ == "__main__":
    main_bench()
