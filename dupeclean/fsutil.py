"""Hilfsfunktionen für Dateisystemzugriffe unter Windows.

Alle Pfade werden intern als normale absolute Pfade geführt (z. B. ``E:\\Sicherung\\a.txt``).
Für jeden Betriebssystemaufruf wird ``lp()`` verwendet, das unter Windows das Präfix
``\\\\?\\`` voranstellt, damit auch Pfade mit mehr als 260 Zeichen funktionieren.
"""
from __future__ import annotations

import os
import stat as _stat
import sys

IS_WINDOWS = os.name == "nt"
_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"

FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def norm(path: str) -> str:
    """Absoluter, normalisierter Pfad ohne Langpfad-Präfix."""
    path = strip_prefix(os.fspath(path))
    path = os.path.abspath(path)
    # Laufwerkswurzel "E:" -> "E:\\"
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    return path


def strip_prefix(path: str) -> str:
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_PREFIX):
        return path[len(_PREFIX):]
    return path


def lp(path: str) -> str:
    """Pfad für Betriebssystemaufrufe (mit Langpfad-Präfix unter Windows)."""
    if not IS_WINDOWS:
        return path
    if path.startswith(_PREFIX):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return _UNC_PREFIX + path[2:]
    return _PREFIX + path


def key(path: str) -> str:
    """Vergleichsschlüssel für Pfade (Windows: Groß-/Kleinschreibung egal)."""
    k = path.rstrip("\\/") if len(path) > 3 else path
    return k.casefold() if IS_WINDOWS else k


def enc_id(n: int | None) -> str | None:
    """Datei- bzw. Laufwerkskennung als Text.

    Windows liefert Laufwerkskennungen bis 2**64 und (ab Python 3.12) Datei-IDs bis 2**128 –
    beides passt nicht in eine SQLite-Ganzzahl. 0 bzw. None bedeutet "unbekannt".
    """
    return f"x{n:x}" if n else None


def is_within(path: str, parent: str) -> bool:
    """True, wenn ``path`` gleich ``parent`` ist oder darin liegt."""
    p, q = key(path), key(parent)
    if p == q:
        return True
    if not q.endswith(("\\", "/")):
        q += os.sep
    return p.startswith(q)


def is_strictly_within(path: str, parent: str) -> bool:
    return key(path) != key(parent) and is_within(path, parent)


def depth(path: str) -> int:
    return len([p for p in path.replace("/", "\\").split("\\") if p])


def drive_root(path: str) -> str:
    drive, _ = os.path.splitdrive(norm(path))
    return drive + "\\" if drive else "/"


def quarantine_subpath(path: str) -> str:
    """Relativer Pfad, unter dem ``path`` in der Quarantäne abgelegt wird."""
    path = norm(path)
    drive, rest = os.path.splitdrive(path)
    if drive.startswith("\\\\"):
        drive = "UNC_" + drive.strip("\\").replace("\\", "_")
    else:
        drive = drive.rstrip(":")
    return os.path.join(drive, rest.lstrip("\\/"))


def valid_name(name: str) -> bool:
    """Prüft, ob sich ein Name als UTF-8 speichern lässt (keine einzelnen Surrogate)."""
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def entry_is_link(entry: os.DirEntry) -> bool:
    """Symlink, Junction oder sonstiger Analysepunkt -> nicht folgen."""
    try:
        if entry.is_symlink():
            return True
        if hasattr(entry, "is_junction") and entry.is_junction():
            return True
        if IS_WINDOWS:
            st = entry.stat(follow_symlinks=False)
            return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False
    return False


def path_is_link(path: str) -> bool:
    try:
        st = os.lstat(lp(path))
    except OSError:
        return False
    if _stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def exists(path: str) -> bool:
    try:
        os.lstat(lp(path))
        return True
    except OSError:
        return False


def makedirs(path: str) -> None:
    os.makedirs(lp(path), exist_ok=True)


def make_writable(path: str) -> None:
    try:
        st = os.lstat(lp(path))
        os.chmod(lp(path), _stat.S_IMODE(st.st_mode) | _stat.S_IWRITE)
    except OSError:
        pass


def remove_tree(path: str, errors: list | None = None) -> None:
    """Löscht einen Ordner rekursiv (Langpfade, schreibgeschützte Dateien, folgt keinen Links)."""
    if not exists(path):
        return
    if path_is_link(path) or not os.path.isdir(lp(path)):
        _remove_file(path, errors)
        return
    try:
        entries = list(os.scandir(lp(path)))
    except OSError as e:
        if errors is not None:
            errors.append((path, str(e)))
        return
    for entry in entries:
        child = os.path.join(path, entry.name)
        if entry.is_dir(follow_symlinks=False) and not entry_is_link(entry):
            remove_tree(child, errors)
        elif entry.is_dir(follow_symlinks=False):
            try:  # Junction/Verzeichnis-Symlink: nur den Link entfernen
                os.rmdir(lp(child))
            except OSError as e:
                if errors is not None:
                    errors.append((child, str(e)))
        else:
            _remove_file(child, errors)
    try:
        os.rmdir(lp(path))
    except OSError:
        make_writable(path)
        try:
            os.rmdir(lp(path))
        except OSError as e:
            if errors is not None:
                errors.append((path, str(e)))


def _remove_file(path: str, errors: list | None) -> None:
    try:
        os.remove(lp(path))
    except PermissionError:
        make_writable(path)
        try:
            os.remove(lp(path))
        except OSError as e:
            if errors is not None:
                errors.append((path, str(e)))
    except OSError as e:
        if errors is not None:
            errors.append((path, str(e)))


def remove_empty_dirs_upwards(start: str, stop: str) -> list[str]:
    """Entfernt leere Ordner von ``start`` aufwärts, höchstens bis unterhalb ``stop``."""
    removed = []
    cur = start
    while is_strictly_within(cur, stop):
        try:
            with os.scandir(lp(cur)) as it:
                if any(True for _ in it):
                    break
            os.rmdir(lp(cur))
            removed.append(cur)
        except OSError:
            break
        cur = os.path.dirname(cur)
    return removed


def fmt_size(n: float) -> str:
    """Größe deutsch formatiert, z. B. ``1,5 GB``."""
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    n = float(n)
    while abs(n) >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    s = f"{n:.0f}" if i == 0 else f"{n:.1f}"
    return s.replace(".", ",") + " " + units[i]


def fmt_int(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def console_safe(text: str) -> str:
    """Text so umwandeln, dass er sich auf jeder Konsole ausgeben lässt."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(enc, errors="backslashreplace").decode(enc, errors="replace")
