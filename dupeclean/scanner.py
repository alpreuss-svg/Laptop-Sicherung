"""Unterbefehl ``scan``: Dateien erfassen und Inhalts-Hashes berechnen."""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import db, fsutil, hashing, workspace
from .config import Config
from .fsutil import fmt_int, fmt_size, lp
from .hashing import Aborted, FileChanged
from .progress import Progress

BATCH = 2000
COMMIT_INTERVAL = 2.0


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _out(msg: str = "") -> None:
    print(fsutil.console_safe(msg), flush=True)


class Scanner:
    def __init__(self, roots: list[str], data_dir: str, cfg: Config, threads: int = 1,
                 fresh: bool = False, show_progress: bool = True):
        roots = [fsutil.norm(r) for r in roots]
        # verschachtelte Wurzeln entfernen
        roots.sort(key=lambda r: len(r))
        uniq: list[str] = []
        for r in roots:
            if not any(fsutil.is_within(r, u) for u in uniq):
                uniq.append(r)
        self.roots = uniq
        self.data_dir = data_dir
        self.cfg = cfg
        self.threads = max(1, int(threads))
        self.fresh = fresh
        self.show_progress = show_progress
        self.exclude_paths = [fsutil.key(p) for p in cfg.exclude_paths]
        self.exclude_paths.append(fsutil.key(data_dir))
        # Testhilfen (nur in automatisierten Tests gesetzt)
        self._abort_after = int(os.environ.get("DUPECLEAN_TEST_ABORT_SCAN_AFTER", "0") or 0)
        self._delay = float(os.environ.get("DUPECLEAN_TEST_HASH_DELAY", "0") or 0)
        self._hashed = 0

    # ------------------------------------------------------------------ Ablauf
    def run(self) -> int:
        for r in self.roots:
            if not os.path.isdir(lp(r)):
                _out(f"Fehler: Ordner nicht gefunden: {r}")
                return 2
        hashing.STOP.clear()
        con = db.connect(self.data_dir)
        workspace.remember(self.data_dir)
        try:
            state = db.get_meta(con, "scan")
            same = bool(state) and [fsutil.key(r) for r in state.get("roots", [])] == \
                [fsutil.key(r) for r in self.roots]
            if not self.fresh and same and state.get("phase") == "hash" and state.get("algo") == hashing.ALGO:
                _out(f"Setze unterbrochenen Scan fort (begonnen {state.get('started')}).")
            else:
                state = {"roots": self.roots, "phase": "walk", "started": _now(),
                         "algo": hashing.ALGO, "scan_id": time.strftime("%Y%m%d-%H%M%S")}
                db.set_meta(con, "scan", state)
                con.commit()
                _out("Durchsuche " + ", ".join(self.roots))
                self._walk(con)
                state["phase"] = "hash"
                db.set_meta(con, "scan", state)
                con.commit()
            self._hash_stages(con)
            state["phase"] = "done"
            state["finished"] = _now()
            db.set_meta(con, "scan", state)
            con.commit()
            self._summary(con)
            return 0
        except (KeyboardInterrupt, Aborted):
            hashing.STOP.set()
            try:
                con.commit()
            except Exception:
                pass
            sys.stderr.write("\n")
            _out("Scan abgebrochen. Bereits berechnete Hashes sind gespeichert.")
            _out("Ein erneuter Aufruf von 'dupeclean scan' mit denselben Ordnern setzt fort.")
            return 130
        finally:
            con.close()

    # ------------------------------------------------------------------ Phase 1
    def _excluded_path(self, path: str) -> bool:
        k = fsutil.key(path)
        return any(k == p or k.startswith(p + "\\") for p in self.exclude_paths)

    def _walk(self, con) -> None:
        con.execute("DELETE FROM files")
        con.execute("DELETE FROM dirs")
        con.execute("DELETE FROM errors")
        con.commit()
        prog = Progress("Durchsuchen", enabled=self.show_progress)
        file_rows: list = []
        dir_rows: list = []
        err_rows: list = []
        ndirs = 0
        cfg = self.cfg

        def flush():
            con.executemany(
                "INSERT OR REPLACE INTO files(path, dir, name, size, mtime_ns, readonly) VALUES(?,?,?,?,?,?)",
                file_rows)
            con.executemany(
                "INSERT OR REPLACE INTO dirs(path, parent, name, incomplete, entries) VALUES(?,?,?,?,?)",
                dir_rows)
            con.executemany("INSERT INTO errors(path, op, message) VALUES(?,?,?)", err_rows)
            con.commit()
            file_rows.clear()
            dir_rows.clear()
            err_rows.clear()

        stack = [(r, None) for r in reversed(self.roots)]
        while stack:
            d, parent = stack.pop()
            ndirs += 1
            incomplete = 0
            names: list[str] = []
            subdirs: list[str] = []
            try:
                it = os.scandir(lp(d))
            except OSError as e:
                err_rows.append((d, "ordner", _err(e)))
                dir_rows.append((d, parent, os.path.basename(d) or d, 1, "[]"))
                continue
            with it:
                while True:
                    try:
                        entry = next(it)
                    except StopIteration:
                        break
                    except OSError as e:
                        err_rows.append((d, "ordner", _err(e)))
                        incomplete = 1
                        break
                    name = entry.name
                    path = os.path.join(d, name)
                    if not fsutil.valid_name(name):
                        err_rows.append((d, "name", "Ungültiger Dateiname (Unicode) übersprungen: "
                                         + name.encode("utf-8", "backslashreplace").decode()))
                        incomplete = 1
                        continue
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError as e:
                        err_rows.append((path, "lesen", _err(e)))
                        incomplete = 1
                        continue
                    if cfg.is_excluded_name(name) or self._excluded_path(path):
                        if is_dir:
                            incomplete = 1  # Ordner nicht vollständig bekannt
                        continue
                    names.append(name)
                    if fsutil.entry_is_link(entry):
                        err_rows.append((path, "verknuepfung", "Verknüpfung (Symlink/Junction) – nicht gefolgt"))
                        incomplete = 1
                        continue
                    if is_dir:
                        subdirs.append(path)
                        continue
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError as e:
                        err_rows.append((path, "lesen", _err(e)))
                        incomplete = 1
                        continue
                    ro = 1 if getattr(st, "st_file_attributes", 0) & fsutil.FILE_ATTRIBUTE_READONLY else 0
                    file_rows.append((path, d, name, st.st_size, st.st_mtime_ns, ro))
                    prog.add(1, st.st_size, extra=f"{fmt_int(ndirs)} Ordner")
            dir_rows.append((d, parent, os.path.basename(d) or d, incomplete,
                             json.dumps(names, ensure_ascii=False)))
            for s in sorted(subdirs, reverse=True):
                stack.append((s, d))
            if len(file_rows) + len(dir_rows) >= BATCH:
                flush()
        flush()
        prog.done(extra=f"{fmt_int(ndirs)} Ordner")
        self._prune_cache(con)

    def _prune_cache(self, con) -> None:
        """Cache-Einträge für Dateien entfernen, die es unter den Wurzeln nicht mehr gibt."""
        current = {row[0] for row in con.execute("SELECT path FROM files")}
        stale = [(p,) for (p,) in con.execute("SELECT path FROM hashcache")
                 if p not in current and any(fsutil.is_within(p, r) for r in self.roots)]
        con.executemany("DELETE FROM hashcache WHERE path=?", stale)
        con.commit()

    # ------------------------------------------------------------------ Phase 2+3
    def _hash_stages(self, con) -> None:
        algo = hashing.ALGO
        # Stufe 1: nach Größe gruppieren
        rows = con.execute(
            """SELECT f.id, f.path, f.size, f.mtime_ns, f.partial, f.full,
                      h.partial, h.full, h.dev, h.ino, h.nlink
               FROM files f
               LEFT JOIN hashcache h ON h.path=f.path AND h.size=f.size
                    AND h.mtime_ns=f.mtime_ns AND h.algo=?
               WHERE f.error=0 AND f.size>0 AND f.size IN (
                   SELECT size FROM files WHERE error=0 AND size>0 GROUP BY size HAVING COUNT(*)>1)
               ORDER BY f.path""", (algo,)).fetchall()
        updates = []
        todo = []
        for (fid, path, size, mtime, fpart, ffull, hpart, hfull, dev, ino, nlink) in rows:
            if fpart:
                continue  # bereits in diesem Lauf berechnet (Fortsetzung)
            if hpart:
                updates.append((hpart, hfull, dev, ino, nlink, fid))
            else:
                todo.append((fid, path, size, mtime))
        con.executemany("UPDATE files SET partial=?, full=?, dev=?, ino=?, nlink=? WHERE id=?", updates)
        con.commit()
        cached = len(rows) - len(todo)
        _out(f"Stufe 1: {fmt_int(len(rows))} Dateien mit gleicher Größe "
             f"({fmt_int(cached)} aus dem Cache).")

        # Stufe 2: Teil-Hash
        prog = Progress("Stufe 2 Teil-Hash", total_items=len(todo), enabled=self.show_progress)
        self._run_jobs(con, todo, self._job_partial, prog)
        prog.done()

        # Stufe 3: Voll-Hash nur für verbleibende Kandidaten
        groups: dict = defaultdict(list)
        for (fid, path, size, mtime, part, full, dev, ino) in con.execute(
                "SELECT id, path, size, mtime_ns, partial, full, dev, ino FROM files "
                "WHERE partial IS NOT NULL AND error=0"):
            groups[(size, part)].append((fid, path, size, mtime, full, dev, ino))
        todo3 = []
        for members in groups.values():
            phys = {(m[5], m[6]) if m[6] else ("p", m[1]) for m in members}
            if len(phys) < 2:
                continue
            seen = set()
            for m in members:
                pk = (m[5], m[6]) if m[6] else ("p", m[1])
                if m[4] is None and pk not in seen:
                    seen.add(pk)
                    todo3.append((m[0], m[1], m[2], m[3]))
        todo3.sort(key=lambda t: t[1])
        total = sum(t[2] for t in todo3)
        _out(f"Stufe 3: {fmt_int(len(todo3))} Dateien ({fmt_size(total)}) werden vollständig gelesen.")
        prog = Progress("Stufe 3 Voll-Hash", total_items=len(todo3), total_bytes=total,
                        enabled=self.show_progress)
        self._run_jobs(con, todo3, self._job_full, prog)
        prog.done()
        # Voll-Hash auf Hardlink-Geschwister übertragen
        con.execute("""UPDATE files SET full=(SELECT g.full FROM files g WHERE g.dev=files.dev
                         AND g.ino=files.ino AND g.size=files.size AND g.full IS NOT NULL LIMIT 1)
                       WHERE full IS NULL AND ino IS NOT NULL AND ino<>0 AND partial IS NOT NULL""")
        con.commit()

    def _job_partial(self, job):
        fid, path, size, mtime = job
        part, full, dev, ino, nlink = hashing.partial_hash(path, size)
        return part, full, dev, ino, nlink, size

    def _job_full(self, job):
        fid, path, size, mtime = job
        full, dev, ino, nlink, _ = hashing.full_hash(path, size)
        return None, full, dev, ino, nlink, size

    def _run_jobs(self, con, jobs, fn, prog) -> None:
        last_commit = time.monotonic()
        file_upd = []
        cache_upd = []
        err_upd = []

        def store():
            con.executemany(
                "UPDATE files SET partial=COALESCE(?, partial), full=?, dev=?, ino=?, nlink=? WHERE id=?",
                file_upd)
            con.executemany(
                """INSERT INTO hashcache(path, size, mtime_ns, algo, partial, full, dev, ino, nlink)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                     algo=excluded.algo, partial=COALESCE(excluded.partial, hashcache.partial),
                     full=excluded.full, dev=excluded.dev, ino=excluded.ino, nlink=excluded.nlink""",
                cache_upd)
            con.executemany("UPDATE files SET error=1 WHERE id=?", [(e[0],) for e in err_upd])
            con.executemany("INSERT INTO errors(path, op, message) VALUES(?,?,?)",
                            [(e[1], "lesen", e[2]) for e in err_upd])
            con.commit()
            file_upd.clear()
            cache_upd.clear()
            err_upd.clear()

        try:
            for job, result, error in self._iter_results(jobs, fn):
                fid, path, size, mtime = job
                if error is not None:
                    err_upd.append((fid, path, error))
                else:
                    part, full, dev, ino, nlink, _ = result
                    file_upd.append((part, full, dev, ino, nlink, fid))
                    cache_upd.append((path, size, mtime, hashing.ALGO, part, full, dev, ino, nlink))
                prog.add(1, size)
                self._hashed += 1
                if self._delay:
                    time.sleep(self._delay)
                if self._abort_after and self._hashed >= self._abort_after:
                    raise KeyboardInterrupt
                if time.monotonic() - last_commit > COMMIT_INTERVAL or len(file_upd) >= BATCH:
                    store()
                    last_commit = time.monotonic()
        finally:
            store()

    def _iter_results(self, jobs, fn):
        def safe(job):
            try:
                return fn(job), None
            except (Aborted, KeyboardInterrupt):
                raise
            except FileChanged as e:
                return None, str(e)
            except OSError as e:
                return None, _err(e)

        if self.threads <= 1:
            for job in jobs:
                res, err = safe(job)
                yield job, res, err
            return
        window = self.threads * 4
        with ThreadPoolExecutor(max_workers=self.threads) as ex:
            pending = []
            it = iter(jobs)
            try:
                for job in it:
                    pending.append((job, ex.submit(safe, job)))
                    if len(pending) >= window:
                        j, fut = pending.pop(0)
                        res, err = fut.result()
                        yield j, res, err
                for j, fut in pending:
                    res, err = fut.result()
                    yield j, res, err
            except BaseException:
                hashing.STOP.set()
                ex.shutdown(wait=True, cancel_futures=True)
                raise

    # ------------------------------------------------------------------ Ergebnis
    def _summary(self, con) -> None:
        nfiles, total = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files").fetchone()
        nerr = con.execute("SELECT COUNT(*) FROM errors WHERE op<>'verknuepfung'").fetchone()[0]
        nlinks = con.execute("SELECT COUNT(*) FROM errors WHERE op='verknuepfung'").fetchone()[0]
        groups = con.execute(
            """SELECT COUNT(*), COALESCE(SUM(waste),0) FROM (
                 SELECT size*(COUNT(DISTINCT CASE WHEN ino IS NULL OR ino=0 THEN path
                              ELSE dev||':'||ino END)-1) AS waste
                 FROM files WHERE full IS NOT NULL AND error=0 GROUP BY size, full
                 HAVING COUNT(DISTINCT CASE WHEN ino IS NULL OR ino=0 THEN path
                              ELSE dev||':'||ino END)>1)""").fetchone()
        _out("")
        _out(f"Scan abgeschlossen: {fmt_int(nfiles)} Dateien, {fmt_size(total)}.")
        _out(f"Gruppen identischer Dateien: {fmt_int(groups[0])}, "
             f"maximal einsparbar etwa {fmt_size(groups[1])} (vor Anwendung der Schutzregeln).")
        if nerr:
            _out(f"Zugriffsfehler: {fmt_int(nerr)} (siehe Bericht).")
        if nlinks:
            _out(f"Verknüpfungen (nicht gefolgt): {fmt_int(nlinks)}.")
        _out(f"Arbeitsordner: {self.data_dir}")
        _out("Weiter mit:  dupeclean report")


def _err(e: OSError) -> str:
    msg = e.strerror or str(e)
    if getattr(e, "winerror", None):
        msg = f"{msg} (WinError {e.winerror})"
    return msg
