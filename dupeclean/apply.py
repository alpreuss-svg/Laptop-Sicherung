"""Unterbefehle ``apply``, ``undo``, ``purge`` und ``runs``.

Sicherheitsprinzipien:
* Standard ist ein Probelauf; erst mit --execute und der Antwort „ja“ wird etwas verschoben.
* „Entfernen“ heißt Verschieben in die Quarantäne auf derselben Platte (os.rename).
* Vor jeder Aktion wird erneut geprüft: Duplikat unverändert (Größe, mtime, Voll-Hash),
  Original vorhanden und identisch. Sonst wird übersprungen und protokolliert.
* Aus jeder Gruppe bleibt immer mindestens eine Kopie erhalten – auch wenn der Löschplan
  von Hand geändert wurde.
* Jede Aktion steht vor der Ausführung im Journal; ein Abbruch hinterlässt einen Zustand,
  den ``undo`` vollständig zurückdrehen kann.
"""
from __future__ import annotations

import csv
import io
import json
import os
import signal
import shutil
import stat as _stat
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from . import db, fsutil, hashing, workspace
from .config import Config
from .fsutil import fmt_int, fmt_size, lp
from .hashing import Aborted, FileChanged

META_DIR = "_dupeclean_lauf"
CONFIRM_WORD = "ja"


class ApplyError(Exception):
    pass


def _out(msg: str = "") -> None:
    print(fsutil.console_safe(msg), flush=True)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ====================================================================== CSV lesen
def read_plan_csv(path: str) -> tuple[list[dict], list[str]]:
    """Liest den (evtl. in Excel bearbeiteten) Löschplan. Liefert (Zeilen, Warnungen)."""
    warnings = []
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
        warnings.append("Die CSV-Datei ist nicht UTF-8-kodiert (vermutlich von Excel als "
                        "'CSV (Trennzeichen-getrennt)' gespeichert). Pfade mit Sonderzeichen werden "
                        "evtl. nicht erkannt und dann übersprungen. Besser als 'CSV UTF-8' speichern.")
    first = text.splitlines()[0] if text else ""
    delim = max([";", ",", "\t"], key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    try:
        header = next(reader)
    except StopIteration:
        raise ApplyError("Die CSV-Datei ist leer.")
    norm = [h.strip().lstrip("﻿").casefold() for h in header]
    need = {"plan": None, "gruppe": None, "aktion": None, "pfad": None}
    for k in need:
        if k in norm:
            need[k] = norm.index(k)
    missing = [k for k, v in need.items() if v is None]
    if missing:
        raise ApplyError("In der CSV-Datei fehlen die Spalten: " + ", ".join(m.capitalize() for m in missing))
    rows = []
    for i, r in enumerate(reader, start=2):
        if not any(c.strip() for c in r):
            continue
        try:
            rows.append({"line": i, "plan": r[need["plan"]].strip(), "group": r[need["gruppe"]].strip(),
                         "action": r[need["aktion"]].strip().casefold(), "path": r[need["pfad"]].strip()})
        except IndexError:
            warnings.append(f"Zeile {i}: unvollständig, ignoriert.")
    return rows, warnings


# ====================================================================== Planung
@dataclass
class Item:
    group: str
    kind: str
    path: str
    action: str
    planned: str
    size: int
    mtime_ns: int
    content: str
    locked: bool


def build_actions(con, rows: list[dict]) -> tuple[str, dict, dict, list[str]]:
    """Ordnet die CSV-Zeilen dem gespeicherten Plan zu und prüft die Invariante.

    Liefert (plan_id, items_by_path, groups, warnungen).
    """
    warnings: list[str] = []
    plans = {r["plan"] for r in rows if r["plan"]}
    if not plans:
        raise ApplyError("Die CSV-Datei enthält keine Plan-ID (Spalte 'Plan').")
    if len(plans) > 1:
        raise ApplyError("Die CSV-Datei enthält Zeilen aus mehreren Plänen: " + ", ".join(sorted(plans)))
    plan_id = plans.pop()
    prow = con.execute("SELECT scan_id FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not prow:
        raise ApplyError(f"Plan {plan_id} ist in der Datenbank nicht bekannt. Passt der Arbeitsordner (--daten)?")
    scan = db.get_meta(con, "scan") or {}
    if prow[0] != scan.get("scan_id"):
        warnings.append("Der Löschplan stammt aus einem früheren Scan. Geänderte Einträge werden "
                        "übersprungen; am besten mit 'dupeclean report' einen neuen Plan erstellen.")
    items: dict[str, Item] = {}
    groups: dict[str, list[Item]] = defaultdict(list)
    for gid, kind, path, action, size, mtime, content, locked in con.execute(
            "SELECT group_id, kind, path, action, size, mtime_ns, content, locked FROM plan_items "
            "WHERE plan_id=?", (plan_id,)):
        it = Item(gid, kind, path, "behalten", action, size, mtime, content, bool(locked))
        items[fsutil.key(path)] = it
        groups[gid].append(it)
    # Aktionen aus der CSV übernehmen (fehlende Zeilen = behalten)
    for r in rows:
        it = items.get(fsutil.key(r["path"]))
        if it is None or it.group != r["group"]:
            warnings.append(f"Zeile {r['line']}: Pfad gehört nicht zum Plan, ignoriert: {r['path']}")
            continue
        a = r["action"]
        if a in ("entfernen", "löschen", "loeschen"):
            if it.locked:
                warnings.append(f"Zeile {r['line']}: geschützt (Struktur/Hardlink), bleibt erhalten: {it.path}")
            else:
                it.action = "entfernen"
        elif a == "behalten":
            it.action = "behalten"
        else:
            warnings.append(f"Zeile {r['line']}: unbekannte Aktion '{r['action']}', wird als 'behalten' behandelt.")
    _enforce_invariant(items, groups, warnings)
    return plan_id, items, groups, warnings


def _removed_dirs(items: dict) -> list[str]:
    return [it.path for it in items.values() if it.kind == "ordner" and it.action == "entfernen"]


def _covered(path: str, removed_dirs: list[str], self_ok: bool = False) -> bool:
    for d in removed_dirs:
        if fsutil.is_within(path, d) and not (self_ok and fsutil.key(path) == fsutil.key(d)):
            return True
    return False


def survivors(group: list[Item], removed_dirs: list[str]) -> list[Item]:
    return [it for it in group if it.action == "behalten" and not _covered(it.path, removed_dirs)]


def _enforce_invariant(items: dict, groups: dict, warnings: list[str]) -> None:
    """Gruppen ohne überlebende Kopie werden komplett auf 'behalten' zurückgesetzt."""
    while True:
        removed = _removed_dirs(items)
        bad = [gid for gid, g in groups.items()
               if any(it.action == "entfernen" for it in g) and not survivors(g, removed)]
        if not bad:
            return
        for gid in bad:
            warnings.append(f"Gruppe {gid}: es bliebe keine Kopie übrig – die ganze Gruppe wird nicht angefasst.")
            for it in groups[gid]:
                it.action = "behalten"


def removal_list(items: dict) -> list[Item]:
    """Zu entfernende Einträge; Einträge innerhalb entfernter Ordner entfallen (der Ordner deckt sie ab)."""
    removed = _removed_dirs(items)
    out = []
    for it in items.values():
        if it.action != "entfernen":
            continue
        if _covered(it.path, removed, self_ok=True):
            continue
        out.append(it)
    out.sort(key=lambda it: (it.kind != "ordner", it.path.casefold()))
    return out


# ====================================================================== Prüfungen
class Skip(Exception):
    pass


def _snapshot_file(con, path: str):
    return con.execute("SELECT size, mtime_ns, full, dev, ino FROM files WHERE path=?", (path,)).fetchone()


def _snapshot_tree(con, root: str) -> dict:
    """Dateien unterhalb ``root`` aus dem Scan: relpath -> (size, mtime_ns, full)."""
    lo, hi = root + "\\", root + "]"
    out = {}
    for path, size, mtime, full in con.execute(
            "SELECT path, size, mtime_ns, full FROM files WHERE path>? AND path<?", (lo, hi)):
        out[path[len(lo):]] = (size, mtime, full)
    return out


def _walk_now(root: str, cfg: Config) -> dict:
    """Aktueller Inhalt eines Ordners: relpath -> (size, mtime_ns). Wirft Skip bei Auffälligkeiten."""
    out = {}
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(lp(d)))
        except OSError as e:
            raise Skip(f"Ordner nicht lesbar: {d} ({e.strerror or e})")
        for en in entries:
            p = os.path.join(d, en.name)
            is_dir = en.is_dir(follow_symlinks=False)
            if cfg.is_excluded_name(en.name):
                if is_dir:
                    raise Skip(f"enthält ausgeschlossenen Ordner {p}")
                continue
            if fsutil.entry_is_link(en):
                raise Skip(f"enthält Verknüpfung {p}")
            if is_dir:
                stack.append(p)
            else:
                st = en.stat(follow_symlinks=False)
                out[p[len(root) + 1:]] = (st.st_size, st.st_mtime_ns)
    return out


def _check_file(path: str, size: int, mtime: int) -> os.stat_result:
    try:
        st = os.stat(lp(path), follow_symlinks=False)
    except FileNotFoundError:
        raise Skip("nicht mehr vorhanden")
    except OSError as e:
        raise Skip(f"nicht lesbar ({e.strerror or e})")
    if not _stat.S_ISREG(st.st_mode) or fsutil.path_is_link(path):
        raise Skip("ist keine normale Datei mehr")
    if st.st_size != size or st.st_mtime_ns != mtime:
        raise Skip("wurde seit dem Scan verändert (Größe/Datum)")
    return st


class Verifier:
    def __init__(self, con, cfg: Config, thorough: bool, on_bytes=None):
        self.con = con
        self.cfg = cfg
        self.thorough = thorough
        self.on_bytes = on_bytes

    def _hash(self, path: str, size: int) -> str:
        try:
            return hashing.full_hash(path, size, on_bytes=self.on_bytes)[0]
        except FileChanged as e:
            raise Skip(str(e))
        except OSError as e:
            raise Skip(f"nicht lesbar ({e.strerror or e})")

    def file(self, dup: Item, originals: list[Item], quick: bool = False) -> str:
        """Prüft Duplikat-Datei und sucht ein bestätigtes Original. Liefert dessen Pfad."""
        st = _check_file(dup.path, dup.size, dup.mtime_ns)
        if st.st_nlink > 1:
            raise Skip("ist ein Hardlink – wird nicht entfernt")
        if not quick:
            h = self._hash(dup.path, dup.size) if dup.size else "E"
            if h != dup.content:
                raise Skip("Inhalt hat sich seit dem Scan verändert")
        last = "kein Original mehr vorhanden"
        for orig in originals:
            try:
                ost = _check_file(orig.path, orig.size, orig.mtime_ns)
            except Skip as e:
                last = f"Original {orig.path} {e}"
                continue
            if (ost.st_dev, ost.st_ino) == (st.st_dev, st.st_ino) and st.st_ino:
                last = "Original ist dieselbe physische Datei (Hardlink)"
                continue
            if quick:
                return orig.path
            snap = _snapshot_file(self.con, orig.path)
            need_hash = self.thorough or snap is None or (snap[4] and snap[4] != ost.st_ino)
            if need_hash and orig.size:
                if self._hash(orig.path, orig.size) != dup.content:
                    last = f"Original {orig.path} ist nicht mehr identisch"
                    continue
            return orig.path
        raise Skip(last)

    def folder(self, dup: Item, originals: list[Item], quick: bool = False) -> str:
        if not os.path.isdir(lp(dup.path)) or fsutil.path_is_link(dup.path):
            raise Skip("Ordner nicht mehr vorhanden")
        snap = _snapshot_tree(self.con, dup.path)
        now = _walk_now(dup.path, self.cfg)
        if {k: v for k, v in now.items()} != {k: (v[0], v[1]) for k, v in snap.items()}:
            raise Skip("Ordnerinhalt hat sich seit dem Scan verändert")
        last = "kein Original-Ordner mehr vorhanden"
        for orig in originals:
            if not os.path.isdir(lp(orig.path)) or fsutil.path_is_link(orig.path):
                last = f"Original-Ordner {orig.path} nicht mehr vorhanden"
                continue
            if fsutil.is_within(orig.path, dup.path) or fsutil.is_within(dup.path, orig.path):
                last = "Original und Duplikat sind ineinander verschachtelt"
                continue
            osnap = _snapshot_tree(self.con, orig.path)
            try:
                onow = _walk_now(orig.path, self.cfg)
            except Skip as e:
                last = f"Original-Ordner: {e}"
                continue
            if onow != {k: (v[0], v[1]) for k, v in osnap.items()} or set(onow) != set(now):
                last = f"Original-Ordner {orig.path} hat sich verändert"
                continue
            if any(now[k][0] != onow[k][0] for k in now):
                last = f"Original-Ordner {orig.path} weicht ab"
                continue
            if quick:
                return orig.path
            ok = True
            for rel in sorted(now):
                size = now[rel][0]
                if size == 0:
                    continue
                dh = self._hash(os.path.join(dup.path, rel), size)
                if snap[rel][2] and dh != snap[rel][2]:
                    raise Skip(f"Datei {rel} hat sich seit dem Scan verändert")
                oh = osnap[rel][2]
                if self.thorough or not oh:
                    oh = self._hash(os.path.join(orig.path, rel), size)
                if dh != oh:
                    ok = False
                    last = f"Original-Ordner {orig.path}: Datei {rel} weicht ab"
                    break
            if ok:
                return orig.path
        raise Skip(last)


# ====================================================================== Journal
class Journal:
    def __init__(self, path: str):
        self.path = path
        self.f = open(path, "a", encoding="utf-8")

    def write(self, rec: dict) -> None:
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.flush()
        os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()


def read_journal(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass  # unvollständige letzte Zeile nach Absturz
    except FileNotFoundError:
        pass
    return out


class Log:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")

    def __call__(self, msg: str) -> None:
        self.f.write(f"{_now()}  {msg}\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class _StopFlag:
    """Strg+C setzt nur ein Flag; die laufende Aktion wird sauber beendet."""

    def __enter__(self):
        hashing.STOP.clear()
        self._old = None
        if threading.current_thread() is not threading.main_thread():
            return self  # z. B. in der Fensteroberfläche: dort gibt es einen Stopp-Knopf
        self._old = signal.getsignal(signal.SIGINT)

        def handler(signum, frame):
            if not hashing.STOP.is_set():
                sys.stderr.write("\nAbbruch angefordert – die aktuelle Aktion wird noch beendet …\n")
            hashing.STOP.set()

        signal.signal(signal.SIGINT, handler)
        return self

    def __exit__(self, *exc):
        if self._old is not None:
            signal.signal(signal.SIGINT, self._old)
        return False

    @property
    def stop(self) -> bool:
        return hashing.STOP.is_set()


# ====================================================================== apply
def apply(csv_path: str, data_dir: str, cfg: Config, execute: bool = False, thorough: bool = False,
          remove_empty: bool = False, quarantine_root: str | None = None, confirm=input,
          result: dict | None = None) -> int:
    """Wendet den Löschplan an. ``result`` (falls angegeben) wird mit Kennzahlen gefüllt."""
    result = result if result is not None else {}
    con = db.connect(data_dir)
    try:
        rows, warnings = read_plan_csv(csv_path)
        plan_id, items, groups, w2 = build_actions(con, rows)
        warnings += w2
        todo = removal_list(items)
        removed_dirs = _removed_dirs(items)
        n_dirs = sum(1 for it in todo if it.kind == "ordner")
        total = sum(it.size for it in todo)
        changed = sum(1 for it in items.values() if it.action != it.planned)
        result.update({"plan_id": plan_id, "planned": len(todo), "planned_bytes": total,
                       "changed": changed, "warnings": warnings})
        _out(f"Löschplan {plan_id}: {fmt_int(len(todo))} Einträge zum Entfernen "
             f"({fmt_int(n_dirs)} Ordner, {fmt_int(len(todo) - n_dirs)} Dateien), zusammen {fmt_size(total)}.")
        if changed:
            _out(f"  {fmt_int(changed)} Einträge weichen vom Vorschlag des Berichts ab (von Hand geändert).")
        for w in warnings[:50]:
            _out("  Hinweis: " + w)
        if len(warnings) > 50:
            _out(f"  … und {fmt_int(len(warnings) - 50)} weitere Hinweise.")
        if not todo:
            _out("Nichts zu tun.")
            return 0
        quarantine_root = fsutil.norm(quarantine_root) if quarantine_root else workspace.default_quarantine(data_dir)
        if not execute:
            return _dry_run(con, cfg, plan_id, todo, groups, removed_dirs, data_dir, quarantine_root, result)

        _out("")
        _out(f"Die Einträge werden in die Quarantäne verschoben: {quarantine_root}")
        _out("Vorher wird jede Datei erneut vollständig gelesen und mit dem Original verglichen.")
        _out("Mit 'dupeclean undo <Lauf>' lässt sich alles wiederherstellen.")
        answer = confirm(f"Zum Fortfahren '{CONFIRM_WORD}' eingeben: ")
        if (answer or "").strip().casefold() != CONFIRM_WORD:
            _out("Abgebrochen, nichts wurde verändert.")
            return 1
        return _execute(con, cfg, plan_id, csv_path, todo, groups, removed_dirs, data_dir,
                        quarantine_root, thorough, remove_empty, result)
    finally:
        con.close()


def _originals(it: Item, groups: dict, removed_dirs: list[str]) -> list[Item]:
    surv = survivors(groups[it.group], removed_dirs)
    # Bevorzugt das Original laut Plan, dann die übrigen
    return sorted(surv, key=lambda o: (o.planned != "behalten", o.path))


def _dry_run(con, cfg, plan_id, todo, groups, removed_dirs, data_dir, quarantine_root, result) -> int:
    v = Verifier(con, cfg, thorough=False)
    reasons = Counter()
    lines = []
    ok = 0
    ok_bytes = 0
    for it in todo:
        try:
            fn = v.folder if it.kind == "ordner" else v.file
            orig = fn(it, _originals(it, groups, removed_dirs), quick=True)
            ok += 1
            ok_bytes += it.size
            lines.append(f"VERSCHIEBEN  {it.path}\n             (Original: {orig})")
        except Skip as e:
            reasons[str(e) if not str(e).startswith("Original") else "Original nicht bestätigt"] += 1
            lines.append(f"ÜBERSPRINGEN {it.path}\n             ({e})")
    out_dir = os.path.join(data_dir, "berichte", plan_id)
    fsutil.makedirs(out_dir)
    txt = os.path.join(out_dir, "probelauf_" + time.strftime("%Y%m%d-%H%M%S") + ".txt")
    with open(txt, "w", encoding="utf-8") as f:
        f.write(f"DupeClean Probelauf – Plan {plan_id} – {_now()}\nQuarantäne: {quarantine_root}\n\n")
        f.write("\n".join(lines) + "\n")
    result.update({"would_move": ok, "would_move_bytes": ok_bytes, "would_skip": sum(reasons.values()),
                   "skip_reasons": reasons.most_common(10), "list_file": txt})
    _out("")
    _out("PROBELAUF – es wurde nichts verändert.")
    _out(f"  Würde verschieben: {fmt_int(ok)} Einträge, {fmt_size(ok_bytes)}")
    if reasons:
        _out(f"  Würde überspringen: {fmt_int(sum(reasons.values()))}")
        for r, c in reasons.most_common(10):
            _out(f"    {fmt_int(c)} × {r}")
    _out("  (Der vollständige Inhaltsvergleich findet erst bei --execute statt.)")
    _out(f"  Liste aller Aktionen: {txt}")
    _out("")
    _out("Zum Ausführen denselben Befehl mit --execute aufrufen.")
    return 0


def _execute(con, cfg, plan_id, csv_path, todo, groups, removed_dirs, data_dir, quarantine_root,
             thorough, remove_empty, result) -> int:
    run_id = time.strftime("%Y-%m-%d_%H%M%S")
    run_dir = os.path.join(quarantine_root, run_id)
    while fsutil.exists(run_dir):
        time.sleep(1)
        run_id = time.strftime("%Y-%m-%d_%H%M%S")
        run_dir = os.path.join(quarantine_root, run_id)
    meta = os.path.join(run_dir, META_DIR)
    fsutil.makedirs(meta)
    # Quarantäne-Ordner bei künftigen Scans ausschließen
    q = db.get_meta(con, "quarantaene", []) or []
    if quarantine_root not in q:
        q.append(quarantine_root)
        db.set_meta(con, "quarantaene", q)
        con.commit()
    workspace.register_run(data_dir, run_id, run_dir)
    scan = db.get_meta(con, "scan") or {}
    roots = scan.get("roots", [])
    info = {"lauf": run_id, "plan": plan_id, "csv": csv_path, "daten": data_dir, "quarantaene": run_dir,
            "begonnen": _now(), "status": "laeuft", "wurzeln": roots}
    _write_json(os.path.join(meta, "lauf.json"), info)
    journal = Journal(os.path.join(meta, "journal.jsonl"))
    log = Log(os.path.join(meta, "protokoll.log"))
    log(f"Lauf {run_id} gestartet, Plan {plan_id}, {len(todo)} Einträge, Quarantäne {run_dir}")
    total_bytes = sum(it.size for it in todo)
    from .progress import Progress
    prog = Progress("Prüfen und verschieben", total_items=len(todo), total_bytes=total_bytes)
    hashed = [0]

    def on_bytes(n):
        hashed[0] += n

    v = Verifier(con, cfg, thorough=thorough, on_bytes=on_bytes)
    moved = 0
    moved_bytes = 0
    skipped = Counter()
    errors = 0
    abort_after = int(os.environ.get("DUPECLEAN_TEST_ABORT_APPLY_AFTER", "0") or 0)
    crash_after = int(os.environ.get("DUPECLEAN_TEST_CRASH_AFTER_MOVE", "0") or 0)
    parents = set()
    status = "fertig"
    with _StopFlag() as flag:
        for n, it in enumerate(todo, start=1):
            if flag.stop:
                status = "abgebrochen"
                break
            try:
                fn = v.folder if it.kind == "ordner" else v.file
                orig = fn(it, _originals(it, groups, removed_dirs))
                if flag.stop:
                    raise Aborted()
                dst = os.path.join(run_dir, fsutil.quarantine_subpath(it.path))
                if fsutil.exists(dst):
                    raise Skip(f"Ziel in der Quarantäne existiert bereits: {dst}")
                fsutil.makedirs(os.path.dirname(dst))
                rec = {"n": n, "op": "move", "kind": it.kind, "src": it.path, "dst": dst,
                       "size": it.size, "original": orig, "gruppe": it.group}
                journal.write(dict(rec, state="geplant"))
                try:
                    os.rename(lp(it.path), lp(dst))
                except OSError as e:
                    journal.write({"n": n, "op": "move", "state": "fehler", "src": it.path, "dst": dst,
                                   "fehler": str(e)})
                    if getattr(e, "winerror", None) == 17:
                        raise Skip("Quarantäne liegt auf einer anderen Platte – nicht verschoben")
                    raise Skip(f"Verschieben fehlgeschlagen ({e.strerror or e})")
                if crash_after and n >= crash_after:
                    os._exit(3)  # Testhilfe: Absturz direkt nach dem Verschieben
                journal.write({"n": n, "op": "move", "state": "erledigt", "src": it.path, "dst": dst})
                log(f"VERSCHOBEN  {it.kind:6s} {it.path}  ->  {dst}  (Original: {orig})")
                moved += 1
                moved_bytes += it.size
                parents.add(os.path.dirname(it.path))
                if abort_after and moved >= abort_after:
                    hashing.STOP.set()
            except Aborted:
                status = "abgebrochen"
                log(f"ABGEBROCHEN vor {it.path}")
                break
            except Skip as e:
                skipped[str(e) if not str(e).startswith("Original") else "Original nicht bestätigt"] += 1
                log(f"ÜBERSPRUNGEN {it.kind:6s} {it.path}  ({e})")
            except Exception as e:  # nie abbrechen wegen eines einzelnen Eintrags
                errors += 1
                log(f"FEHLER      {it.kind:6s} {it.path}  ({e!r})")
            prog.items = n
            prog.bytes = hashed[0]
            prog.add()
        prog.done()

    removed_empty = 0
    if remove_empty and status == "fertig":
        for p in sorted(parents, key=lambda x: -len(x)):
            stop = next((r for r in roots if fsutil.is_strictly_within(p, r) or fsutil.key(p) == fsutil.key(r)), None)
            if stop is None:
                continue
            for d in fsutil.remove_empty_dirs_upwards(p, stop):
                journal.write({"op": "rmdir", "state": "erledigt", "path": d})
                log(f"LEERER ORDNER ENTFERNT {d}")
                removed_empty += 1
    journal.close()
    info.update({"beendet": _now(), "status": status, "verschoben": moved, "bytes": moved_bytes,
                 "uebersprungen": sum(skipped.values()), "fehler": errors, "leere_ordner": removed_empty})
    _write_json(os.path.join(meta, "lauf.json"), info)
    log(f"Lauf beendet: {status}, {moved} verschoben ({moved_bytes} Bytes), "
        f"{sum(skipped.values())} übersprungen, {errors} Fehler")
    log.close()
    result.update({"run_id": run_id, "run_dir": run_dir, "status": status, "moved": moved,
                   "moved_bytes": moved_bytes, "skipped": sum(skipped.values()),
                   "skip_reasons": skipped.most_common(10), "errors": errors,
                   "removed_empty": removed_empty, "log": os.path.join(meta, "protokoll.log")})
    _out("")
    _out(f"Lauf {run_id}: {'ABGEBROCHEN' if status == 'abgebrochen' else 'fertig'}.")
    _out(f"  Verschoben: {fmt_int(moved)} Einträge, {fmt_size(moved_bytes)}")
    if skipped:
        _out(f"  Übersprungen: {fmt_int(sum(skipped.values()))}")
        for r, c in skipped.most_common(10):
            _out(f"    {fmt_int(c)} × {r}")
    if errors:
        _out(f"  Fehler: {fmt_int(errors)}")
    if removed_empty:
        _out(f"  Leere Ordner entfernt: {fmt_int(removed_empty)}")
    _out(f"  Protokoll: {os.path.join(meta, 'protokoll.log')}")
    _out(f"  Quarantäne: {run_dir}")
    _out(f"Rückgängig machen:     dupeclean undo {run_id}")
    _out(f"Endgültig löschen:     dupeclean purge {run_id}")
    return 130 if status == "abgebrochen" else 0


# ====================================================================== Läufe finden
def find_run(run: str, data_dir: str | None) -> str:
    """Liefert den Quarantäne-Ordner eines Laufs (Lauf-ID oder Pfad)."""
    if os.path.isdir(lp(run)) and os.path.isdir(lp(os.path.join(run, META_DIR))):
        return fsutil.norm(run)
    candidates = []
    if data_dir:
        candidates.append(data_dir)
    last = workspace.last_data_dir()
    if last and last not in candidates:
        candidates.append(last)
    for d in candidates:
        runs = workspace.load_runs(d)
        if run in runs:
            return runs[run]
        q = os.path.join(workspace.default_quarantine(d), run)
        if os.path.isdir(lp(os.path.join(q, META_DIR))):
            return q
    raise ApplyError(f"Lauf '{run}' nicht gefunden. 'dupeclean runs' zeigt alle bekannten Läufe.")


def _run_info(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, META_DIR, "lauf.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _moves(run_dir: str) -> tuple[list[dict], list[str]]:
    """Alle (geplanten) Verschiebungen und entfernten leeren Ordner eines Laufs."""
    moves: dict[int, dict] = {}
    rmdirs = []
    for rec in read_journal(os.path.join(run_dir, META_DIR, "journal.jsonl")):
        if rec.get("op") == "move":
            if rec.get("state") == "geplant":
                moves[rec["n"]] = rec
            elif rec.get("state") == "fehler":
                moves.pop(rec.get("n"), None)
        elif rec.get("op") == "rmdir":
            rmdirs.append(rec["path"])
    return [moves[k] for k in sorted(moves)], rmdirs


# ====================================================================== undo
def undo(run: str, data_dir: str | None, result: dict | None = None) -> int:
    run_dir = find_run(run, data_dir)
    info = _run_info(run_dir)
    meta = os.path.join(run_dir, META_DIR)
    if info.get("status") == "endgueltig geloescht":
        raise ApplyError("Dieser Lauf wurde bereits endgültig gelöscht.")
    moves, rmdirs = _moves(run_dir)
    _out(f"Stelle Lauf {info.get('lauf', run)} wieder her: {fmt_int(len(moves))} Einträge …")
    log = Log(os.path.join(meta, "protokoll.log"))
    journal = Journal(os.path.join(meta, "undo.jsonl"))
    log("UNDO gestartet")
    for d in reversed(rmdirs):
        try:
            fsutil.makedirs(d)
            log(f"ORDNER WIEDER ANGELEGT {d}")
        except OSError as e:
            log(f"FEHLER beim Anlegen von {d}: {e}")
    restored = already = conflicts = missing = 0
    with _StopFlag() as flag:
        for rec in reversed(moves):
            if flag.stop:
                break
            src, dst = rec["src"], rec["dst"]
            s_ex, d_ex = fsutil.exists(src), fsutil.exists(dst)
            if d_ex and not s_ex:
                try:
                    fsutil.makedirs(os.path.dirname(src))
                    os.rename(lp(dst), lp(src))
                except OSError as e:
                    log(f"FEHLER  {dst} -> {src}: {e}")
                    conflicts += 1
                    continue
                journal.write({"n": rec["n"], "state": "wiederhergestellt", "src": src})
                log(f"WIEDERHERGESTELLT {src}")
                restored += 1
            elif s_ex and not d_ex:
                already += 1
            elif s_ex and d_ex:
                log(f"KONFLIKT {src} existiert bereits – Kopie bleibt in der Quarantäne: {dst}")
                conflicts += 1
            else:
                log(f"FEHLT   weder {src} noch {dst} vorhanden")
                missing += 1
    stopped = hashing.STOP.is_set()
    journal.close()
    # leere Ordner in der Quarantäne aufräumen
    _prune_empty(run_dir, keep=meta)
    status = "abgebrochen (undo)" if stopped else ("rueckgaengig" if not conflicts and not missing
                                                   else "teilweise rueckgaengig")
    info.update({"status": status, "undo": _now()})
    _write_json(os.path.join(meta, "lauf.json"), info)
    log(f"UNDO beendet: {restored} wiederhergestellt, {already} waren nicht verschoben, "
        f"{conflicts} Konflikte, {missing} fehlen")
    log.close()
    if result is not None:
        result.update({"restored": restored, "already": already, "conflicts": conflicts,
                       "missing": missing, "status": status})
    _out(f"  Wiederhergestellt: {fmt_int(restored)}")
    if already:
        _out(f"  Waren nicht verschoben: {fmt_int(already)}")
    if conflicts:
        _out(f"  Konflikte (bleiben in der Quarantäne): {fmt_int(conflicts)} – siehe Protokoll")
    if missing:
        _out(f"  Fehlen: {fmt_int(missing)} – siehe Protokoll")
    if stopped:
        _out("Abgebrochen – ein erneuter Aufruf setzt fort.")
        return 130
    return 0 if not conflicts and not missing else 1


def _prune_empty(root: str, keep: str) -> None:
    for dirpath, dirnames, filenames in os.walk(lp(root), topdown=False):
        p = fsutil.strip_prefix(dirpath)
        if fsutil.is_within(p, keep) or fsutil.key(p) == fsutil.key(root):
            continue
        try:
            if not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass


def _tree_size(root: str, skip: str) -> tuple[int, int]:
    n = size = 0
    for dirpath, dirnames, filenames in os.walk(lp(root)):
        if fsutil.is_within(fsutil.strip_prefix(dirpath), skip):
            dirnames[:] = []
            continue
        for fn in filenames:
            try:
                size += os.lstat(os.path.join(dirpath, fn)).st_size
                n += 1
            except OSError:
                pass
    return n, size


# ====================================================================== purge
def purge(run: str, data_dir: str | None, confirm=input) -> int:
    run_dir = find_run(run, data_dir)
    info = _run_info(run_dir)
    meta = os.path.join(run_dir, META_DIR)
    n, size = _tree_size(run_dir, meta)
    _out(f"Lauf {info.get('lauf', run)} – Status: {info.get('status', '?')}")
    _out(f"Quarantäne: {run_dir}")
    _out(f"Darin: {fmt_int(n)} Dateien, {fmt_size(size)}")
    if info.get("status") == "laeuft":
        _out("Achtung: Dieser Lauf wurde nicht ordnungsgemäß beendet (Absturz?).")
    if "teilweise" in str(info.get("status", "")):
        _out("Achtung: Beim Wiederherstellen gab es Konflikte; die betroffenen Dateien liegen noch hier.")
    _out("Die Dateien werden ENDGÜLTIG gelöscht. Das kann nicht rückgängig gemacht werden.")
    answer = confirm(f"Zum endgültigen Löschen '{CONFIRM_WORD}' eingeben: ")
    if (answer or "").strip().casefold() != CONFIRM_WORD:
        _out("Abgebrochen, nichts wurde gelöscht.")
        return 1
    # Protokolle aufbewahren
    archive_root = data_dir or info.get("daten")
    archive = None
    if archive_root:
        archive = os.path.join(archive_root, "laeufe", info.get("lauf", os.path.basename(run_dir)))
        fsutil.makedirs(archive)
        for fn in os.listdir(lp(meta)):
            shutil.copy2(lp(os.path.join(meta, fn)), lp(os.path.join(archive, fn)))
    errors: list = []
    for en in list(os.scandir(lp(run_dir))):
        if en.name == META_DIR:
            continue
        fsutil.remove_tree(os.path.join(run_dir, en.name), errors)
    info.update({"status": "endgueltig geloescht", "geloescht": _now(), "geloescht_dateien": n,
                 "geloescht_bytes": size})
    if errors:
        info["status"] = "teilweise geloescht"
        for p, e in errors[:20]:
            _out(f"  Fehler: {p}: {e}")
        _write_json(os.path.join(meta, "lauf.json"), info)
        if archive:
            _write_json(os.path.join(archive, "lauf.json"), info)
        _out(f"{fmt_int(len(errors))} Einträge konnten nicht gelöscht werden.")
        return 1
    if archive:
        _write_json(os.path.join(archive, "lauf.json"), info)
        fsutil.remove_tree(run_dir)
        if data_dir:
            workspace.register_run(data_dir, info.get("lauf", run), archive)
        _out(f"Gelöscht. Protokolle aufbewahrt in {archive}")
    else:
        _write_json(os.path.join(meta, "lauf.json"), info)
        _out("Gelöscht.")
    return 0


# ====================================================================== runs
def run_table(data_dir: str) -> list[dict]:
    """Alle bekannten Läufe mit ihren Angaben (neueste zuerst)."""
    out = []
    for run_id, d in workspace.load_runs(data_dir).items():
        info = _run_info(d)
        if not info:
            try:
                with open(os.path.join(d, "lauf.json"), encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                info = {}
        out.append(dict(info, lauf=run_id, ordner=d))
    out.sort(key=lambda r: r["lauf"], reverse=True)
    return out


def list_runs(data_dir: str) -> int:
    runs = workspace.load_runs(data_dir)
    if not runs:
        _out("Noch keine Läufe.")
        return 0
    _out(f"{'Lauf':20s} {'Status':24s} {'Verschoben':>11s} {'Größe':>10s}  Ordner")
    for run_id in sorted(runs):
        d = runs[run_id]
        info = _run_info(d)
        if not info:
            try:
                with open(os.path.join(d, "lauf.json"), encoding="utf-8") as f:
                    info = json.load(f)
            except (OSError, ValueError):
                info = {}
        _out(f"{run_id:20s} {str(info.get('status', '?')):24s} {fmt_int(info.get('verschoben', 0)):>11s} "
             f"{fmt_size(info.get('bytes', 0)):>10s}  {d}")
    return 0
