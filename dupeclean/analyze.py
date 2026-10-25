"""Auswertung der Scan-Daten: Datei- und Ordnergruppen, Behalten-Regeln, Hinweise."""
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

from . import fsutil
from .config import Config

KEEP_LABELS = {
    "bevorzugt": "bevorzugter Pfad",
    "abwerten": "weniger Kopie-/Backup-Kennzeichnungen im Pfad",
    "aeltestes": "ältestes Änderungsdatum",
    "tiefe": "geringere Pfadtiefe",
    "laenge": "kürzerer Pfad",
}


@dataclass
class Member:
    path: str
    size: int
    mtime_ns: int
    action: str = "behalten"
    reason: str = ""
    locked: bool = False
    files: int = 1          # bei Ordnern: Anzahl Dateien


@dataclass
class Group:
    gid: str
    kind: str               # 'datei' | 'ordner'
    size: int               # Größe einer Kopie
    content: str
    members: list = field(default_factory=list)

    @property
    def savings(self) -> int:
        return sum(m.size for m in self.members if m.action == "entfernen")


@dataclass
class Analysis:
    groups: list
    name_conflicts: list    # [(name, [ {path,size,mtime_ns,content} ])]
    subsets: list           # [ {path, size, files, container, container_size} ]
    errors: list            # [(path, op, message)]
    stats: dict


class _Dir:
    __slots__ = ("path", "parent", "name", "incomplete", "entries", "files", "children",
                 "fp", "size", "nfiles", "min_mtime", "depth", "prot_root", "prot_anc",
                 "all_h", "idx")

    def __init__(self, path, parent, name, incomplete, entries):
        self.path = path
        self.parent = parent
        self.name = name
        self.incomplete = incomplete
        self.entries = entries
        self.files = []
        self.children = []
        self.fp = None
        self.size = 0
        self.nfiles = 0
        self.min_mtime = None
        self.depth = fsutil.depth(path)
        self.prot_root = None   # Grund, falls dieser Ordner selbst eine geschützte Struktur ist
        self.prot_anc = None    # nächstgelegene geschützte Struktur oberhalb (Pfad, Grund)
        self.all_h = True
        self.idx = 0


class _File:
    __slots__ = ("id", "path", "dir", "name", "size", "mtime_ns", "token", "nlink", "ino")

    def __init__(self, fid, path, d, name, size, mtime, token, nlink, ino):
        self.id = fid
        self.path = path
        self.dir = d
        self.name = name
        self.size = size
        self.mtime_ns = mtime
        self.token = token
        self.nlink = nlink
        self.ino = ino


class Analyzer:
    def __init__(self, con, cfg: Config, prefer: list[str] | None = None):
        self.con = con
        self.cfg = cfg
        self.prefer = [fsutil.norm(p) for p in (prefer or [])] + list(cfg.prefer)
        self.devalue = cfg.devalue_patterns()

    # ------------------------------------------------------------------ Laden
    def _load(self):
        dirs: dict[str, _Dir] = {}
        for path, parent, name, incomplete, entries in self.con.execute(
                "SELECT path, parent, name, incomplete, entries FROM dirs"):
            dirs[path] = _Dir(path, parent, name, bool(incomplete), json.loads(entries or "[]"))
        files: list[_File] = []
        for fid, path, d, name, size, mtime, partial, full, dev, ino, nlink, error in self.con.execute(
                "SELECT id, path, dir, name, size, mtime_ns, partial, full, dev, ino, nlink, error FROM files"):
            if error:
                token = f"X{fid}"
            elif size == 0:
                token = "E"
            elif full:
                token = "H" + full
            elif ino:
                token = f"I{dev}:{ino}"
            else:
                token = f"U{fid}"
            f = _File(fid, path, d, name, size, mtime, token, nlink or 1, (dev, ino) if ino else None)
            files.append(f)
            if d in dirs:
                dirs[d].files.append(f)
        for d in dirs.values():
            if d.parent is not None and d.parent in dirs:
                dirs[d.parent].children.append(d)
        return dirs, files

    # ------------------------------------------------------------------ Ordner
    def _fingerprints(self, dirs: dict[str, _Dir]) -> None:
        for i, d in enumerate(sorted(dirs.values(), key=lambda x: -x.depth)):
            d.idx = i
            size = 0
            n = 0
            mt = None
            ok = not d.incomplete
            all_h = True
            items = []
            for f in d.files:
                size += f.size
                n += 1
                if f.size > 0:
                    mt = f.mtime_ns if mt is None else min(mt, f.mtime_ns)
                if f.token[0] in "XU" or f.token[0] == "I":
                    all_h = False
                if f.token[0] == "X":
                    ok = False
                items.append("F\0" + f.name + "\0" + str(f.size) + "\0" + f.token)
            for c in d.children:
                size += c.size
                n += c.nfiles
                if c.min_mtime is not None:
                    mt = c.min_mtime if mt is None else min(mt, c.min_mtime)
                all_h = all_h and c.all_h
                if c.fp is None:
                    ok = False
                else:
                    items.append("D\0" + c.name + "\0" + c.fp)
            d.size, d.nfiles, d.min_mtime, d.all_h = size, n, mt, all_h
            if ok:
                items.sort()
                h = hashlib.blake2b(digest_size=20)
                for it in items:
                    h.update(it.encode("utf-8", "surrogatepass"))
                    h.update(b"\n")
                d.fp = h.hexdigest()

    def _protection(self, dirs: dict[str, _Dir]) -> None:
        for d in sorted(dirs.values(), key=lambda x: x.depth):
            d.prot_root = self.cfg.protect_reason(d.name, d.entries)
            parent = dirs.get(d.parent) if d.parent else None
            if parent is not None:
                if parent.prot_root:
                    d.prot_anc = (parent.path, parent.prot_root)
                else:
                    d.prot_anc = parent.prot_anc

    # ------------------------------------------------------------------ Regeln
    def _rule_values(self, path: str, mtime: int | None) -> dict:
        prefer_idx = len(self.prefer)
        for i, p in enumerate(self.prefer):
            if fsutil.is_within(path, p):
                prefer_idx = i
                break
        return {
            "bevorzugt": prefer_idx,
            "abwerten": sum(1 for pat in self.devalue if pat.search(path)),
            "aeltestes": mtime if mtime is not None else 2 ** 63,
            "tiefe": fsutil.depth(path),
            "laenge": len(path),
        }

    def _choose(self, members: list[Member], mtimes: dict) -> None:
        """Setzt action/reason. Gesperrte Mitglieder bleiben immer; sonst genau ein Original."""
        locked = [m for m in members if m.locked]
        free = [m for m in members if not m.locked]
        if locked:
            keeper = None
            for m in locked:
                m.action = "behalten"
            ref = locked[0]
        else:
            vals = {m.path: self._rule_values(m.path, mtimes.get(m.path)) for m in free}
            order = self.cfg.keep_order

            def k(m):
                v = vals[m.path]
                return tuple(v[r] for r in order) + (m.path.casefold(), m.path)

            ranked = sorted(free, key=k)
            keeper = ranked[0]
            keeper.action = "behalten"
            reason = "Original"
            if len(ranked) > 1:
                a, b = vals[keeper.path], vals[ranked[1].path]
                for r in order:
                    if a[r] != b[r]:
                        reason = f"Original ({KEEP_LABELS[r]})"
                        break
                else:
                    reason = "Original (alphabetisch erster Pfad)"
            keeper.reason = reason
            ref = keeper
        for m in free:
            if m is not keeper:
                m.action = "entfernen"
                m.reason = "Duplikat von " + ref.path
        # Invariante: mindestens eine Kopie bleibt
        assert any(m.action == "behalten" for m in members)

    # ------------------------------------------------------------------ Haupt
    def run(self) -> Analysis:
        dirs, files = self._load()
        self._fingerprints(dirs)
        self._protection(dirs)

        def inside_removed(path_dir: str | None, removed: set) -> bool:
            cur = path_dir
            while cur is not None:
                if cur in removed:
                    return True
                d = dirs.get(cur)
                cur = d.parent if d else None
            return False

        # ---- Ordnergruppen (größte zuerst, damit übergeordnete vor enthaltenen kommen)
        by_fp: dict[str, list[_Dir]] = defaultdict(list)
        for d in dirs.values():
            if d.fp is not None and d.size > 0:
                by_fp[d.fp].append(d)
        fgroups = [g for g in by_fp.values() if len(g) >= 2]
        fgroups.sort(key=lambda g: (-g[0].size, min(d.depth for d in g), min(d.path for d in g)))
        removed_dirs: set[str] = set()
        in_identical: set[str] = set()
        groups: list[Group] = []
        n = 0
        for g in fgroups:
            for d in g:
                in_identical.add(d.path)
            live = [d for d in g if not inside_removed(d.path, removed_dirs)]
            if len(live) < 2:
                continue
            members = []
            mt = {}
            for d in sorted(live, key=lambda x: x.path):
                m = Member(d.path, d.size, d.min_mtime or 0, files=d.nfiles)
                if d.prot_anc:
                    m.locked = True
                    m.reason = f"Geschützt: liegt in {d.prot_anc[1]} ({d.prot_anc[0]})"
                members.append(m)
                mt[d.path] = d.min_mtime
            self._choose(members, mt)
            if not any(m.action == "entfernen" for m in members):
                continue
            n += 1
            for m in members:
                if m.action == "entfernen":
                    removed_dirs.add(m.path)
            groups.append(Group(f"O{n:06d}", "ordner", live[0].size, live[0].fp, members))

        # ---- Dateigruppen
        by_tok: dict[tuple, list[_File]] = defaultdict(list)
        for f in files:
            if f.token[0] == "H" or (f.token == "E" and not self.cfg.ignore_empty_files):
                by_tok[(f.size, f.token)].append(f)
        fn = 0
        file_groups = []
        for (size, tok), fl in by_tok.items():
            if len(fl) < 2:
                continue
            live = [f for f in fl if not inside_removed(f.dir, removed_dirs)]
            if len(live) < 2:
                continue
            # verschiedene physische Dateien?
            phys = {(f.ino or ("p", f.path)) for f in live}
            if len(phys) < 2:
                continue
            members = []
            mt = {}
            for f in sorted(live, key=lambda x: x.path):
                m = Member(f.path, f.size, f.mtime_ns)
                d = dirs.get(f.dir)
                if f.nlink > 1:
                    m.locked = True
                    m.reason = "Hardlink – wird nicht als Duplikat gezählt"
                elif d is not None and (d.prot_root or d.prot_anc):
                    where = (d.path, d.prot_root) if d.prot_root else d.prot_anc
                    m.locked = True
                    m.reason = f"Geschützt: liegt in {where[1]} ({where[0]})"
                members.append(m)
                mt[f.path] = f.mtime_ns
            self._choose(members, mt)
            if not any(m.action == "entfernen" for m in members):
                continue
            file_groups.append(Group("", "datei", size, tok[1:] if tok[0] == "H" else tok, members))
        file_groups.sort(key=lambda g: (-g.savings, g.members[0].path))
        for g in file_groups:
            fn += 1
            g.gid = f"D{fn:06d}"
        groups.extend(file_groups)
        groups.sort(key=lambda g: -g.savings)

        # ---- gleicher Name, anderer Inhalt
        by_name: dict[str, list[_File]] = defaultdict(list)
        for f in files:
            if f.size > 0 and f.token[0] != "X":
                by_name[f.name.casefold()].append(f)
        conflicts = []
        for name, fl in by_name.items():
            toks = {f.token for f in fl}
            if len(toks) >= 2:
                conflicts.append((fl[0].name, [
                    {"path": f.path, "size": f.size, "mtime_ns": f.mtime_ns,
                     "content": f.token[1:13] if f.token[0] == "H" else "einmalig"}
                    for f in sorted(fl, key=lambda x: (x.token, x.path))], len(toks)))
        conflicts.sort(key=lambda c: (-c[2], c[0].casefold()))

        subsets = self._subsets(dirs, files, in_identical)

        errors = list(self.con.execute("SELECT path, op, message FROM errors ORDER BY op, path"))
        stats = {
            "files": len(files),
            "bytes": sum(f.size for f in files),
            "dirs": len(dirs),
            "groups_files": len(file_groups),
            "groups_dirs": n,
            "savings": sum(g.savings for g in groups),
            "remove_items": sum(1 for g in groups for m in g.members if m.action == "entfernen"),
            "conflicts": len(conflicts),
            "subsets": len(subsets),
            "errors": sum(1 for e in errors if e[1] != "verknuepfung"),
            "links": sum(1 for e in errors if e[1] == "verknuepfung"),
        }
        return Analysis(groups, conflicts, subsets, errors, stats)

    # ------------------------------------------------------------------ Teilmengen
    def _subsets(self, dirs: dict[str, _Dir], files: list[_File], in_identical: set) -> list:
        tok_dirs: dict[str, list[_Dir]] = defaultdict(list)
        for f in files:
            if f.token[0] == "H":
                d = dirs.get(f.dir)
                if d is not None:
                    tok_dirs[f.token].append(d)
        anc_cache: dict[str, tuple] = {}

        def ancestors(d: _Dir) -> tuple:
            if d.path in anc_cache:
                return anc_cache[d.path]
            chain = []
            cur = d
            while cur is not None:
                chain.append(cur.path)
                cur = dirs.get(cur.parent) if cur.parent else None
            t = tuple(chain)
            anc_cache[d.path] = t
            return t

        def containers(tok: str) -> set:
            s = set()
            for d in tok_dirs[tok]:
                s.update(ancestors(d))
            return s

        def subtree_tokens(d: _Dir) -> set:
            out = set()
            stack = [d]
            while stack:
                x = stack.pop()
                for f in x.files:
                    if f.size > 0:
                        out.add(f.token)
                stack.extend(x.children)
            return out

        result = []
        reported: set[str] = set()
        for d in sorted(dirs.values(), key=lambda x: (x.depth, x.path)):
            if d.size == 0 or not d.all_h or d.fp is None or d.path in in_identical:
                continue
            anc = ancestors(d)
            if any(a in reported for a in anc[1:]):
                continue
            toks = sorted(subtree_tokens(d), key=lambda t: len(tok_dirs[t]))
            if not toks:
                continue
            cand = containers(toks[0])
            excluded = set(anc)
            cand = {c for c in cand if c not in excluded and not fsutil.is_within(c, d.path)}
            for t in toks[1:]:
                if not cand:
                    break
                cand &= containers(t)
            if not cand:
                continue
            best = min((dirs[c] for c in cand), key=lambda x: (x.size, -x.depth, x.path))
            reported.add(d.path)
            result.append({"path": d.path, "size": d.size, "files": d.nfiles,
                           "container": best.path, "container_size": best.size})
        result.sort(key=lambda r: -r["size"])
        return result
