"""Ermittelt Arbeitsordner (Cache, Berichte) und Quarantäneordner."""
from __future__ import annotations

import json
import os

from . import fsutil

DATA_DIR_NAME = ".dupeclean"
QUARANTINE_DIR_NAME = "_DupeClean_Quarantaene"


class WorkspaceError(Exception):
    pass


def _home() -> str:
    base = os.environ.get("DUPECLEAN_HOME")
    if base:
        return base
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "DupeClean")


def home_dir() -> str:
    """Ordner für Benutzereinstellungen (Standard: %LOCALAPPDATA%\\DupeClean)."""
    return _home()


def _last_file() -> str:
    return os.path.join(_home(), "zuletzt.json")


def remember(data_dir: str) -> None:
    try:
        os.makedirs(_home(), exist_ok=True)
        with open(_last_file(), "w", encoding="utf-8") as f:
            json.dump({"daten": data_dir}, f, ensure_ascii=False)
    except OSError:
        pass


def last_data_dir() -> str | None:
    try:
        with open(_last_file(), encoding="utf-8") as f:
            return json.load(f).get("daten")
    except (OSError, ValueError):
        return None


def data_dir_for_roots(roots: list[str], explicit: str | None) -> str:
    if explicit:
        return fsutil.norm(explicit)
    return os.path.join(fsutil.drive_root(roots[0]), DATA_DIR_NAME)


def resolve_data_dir(explicit: str | None, hint_path: str | None = None) -> str:
    """Arbeitsordner für report/apply/undo/purge finden."""
    if explicit:
        d = fsutil.norm(explicit)
    else:
        d = None
        if hint_path:
            cur = fsutil.norm(hint_path)
            while True:
                if os.path.basename(cur).casefold() == DATA_DIR_NAME:
                    d = cur
                    break
                parent = os.path.dirname(cur)
                if parent == cur:
                    break
                cur = parent
        if d is None:
            d = last_data_dir()
    if not d:
        raise WorkspaceError(
            "Kein Arbeitsordner bekannt. Bitte zuerst 'dupeclean scan <Ordner>' ausführen "
            "oder den Arbeitsordner mit --daten angeben.")
    from .db import DB_NAME
    if not os.path.isfile(os.path.join(d, DB_NAME)):
        raise WorkspaceError(f"Im Arbeitsordner {d} wurde keine Scan-Datenbank gefunden.")
    return d


def default_quarantine(data_dir: str) -> str:
    return os.path.join(fsutil.drive_root(data_dir), QUARANTINE_DIR_NAME)


def runs_registry(data_dir: str) -> str:
    return os.path.join(data_dir, "laeufe.json")


def load_runs(data_dir: str) -> dict:
    try:
        with open(runs_registry(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def register_run(data_dir: str, run_id: str, run_dir: str) -> None:
    runs = load_runs(data_dir)
    runs[run_id] = run_dir
    tmp = runs_registry(data_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=1)
    os.replace(tmp, runs_registry(data_dir))
