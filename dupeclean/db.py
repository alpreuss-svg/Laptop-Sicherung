"""SQLite-Datenbank im Arbeitsordner (Standard: <Laufwerk>:\\.dupeclean)."""
from __future__ import annotations

import json
import os
import sqlite3

from . import fsutil

DB_NAME = "dupeclean.sqlite"
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Hash-Cache über Läufe hinweg: gültig, solange Größe und mtime gleich sind.
CREATE TABLE IF NOT EXISTS hashcache (
    path     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    algo     TEXT NOT NULL,
    partial  TEXT,
    full     TEXT,
    dev      INTEGER,
    ino      INTEGER,
    nlink    INTEGER
);
-- Momentaufnahme des letzten Scans.
CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    dir      TEXT NOT NULL,
    name     TEXT NOT NULL,
    size     INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    readonly INTEGER NOT NULL DEFAULT 0,
    partial  TEXT,
    full     TEXT,
    dev      INTEGER,
    ino      INTEGER,
    nlink    INTEGER,
    error    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir);
CREATE TABLE IF NOT EXISTS dirs (
    path       TEXT PRIMARY KEY,
    parent     TEXT,
    name       TEXT NOT NULL,
    -- 1 = Ordner nicht vollständig erfasst (Fehler, Verknüpfung, ausgeschlossener Unterordner)
    incomplete INTEGER NOT NULL DEFAULT 0,
    entries    TEXT  -- JSON-Liste aller direkten Eintragsnamen (für geschützte Strukturen)
);
CREATE TABLE IF NOT EXISTS errors (
    id      INTEGER PRIMARY KEY,
    path    TEXT,
    op      TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id  TEXT PRIMARY KEY,
    created  TEXT,
    scan_id  TEXT,
    summary  TEXT
);
CREATE TABLE IF NOT EXISTS plan_items (
    plan_id  TEXT NOT NULL,
    group_id TEXT NOT NULL,
    kind     TEXT NOT NULL,      -- 'datei' oder 'ordner'
    path     TEXT NOT NULL,
    action   TEXT NOT NULL,      -- 'behalten' oder 'entfernen'
    reason   TEXT,
    size     INTEGER,
    mtime_ns INTEGER,
    content  TEXT,               -- Inhalts-Hash bzw. Ordner-Fingerabdruck
    locked   INTEGER NOT NULL DEFAULT 0,  -- 1 = darf nie entfernt werden
    PRIMARY KEY (plan_id, path)
);
CREATE INDEX IF NOT EXISTS plan_items_group ON plan_items(plan_id, group_id);
"""


def connect(data_dir: str) -> sqlite3.Connection:
    fsutil.makedirs(data_dir)
    path = os.path.join(data_dir, DB_NAME)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),))
    _migrate(con)
    con.commit()
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Version 1 -> 2: Laufwerks-/Datei-IDs als Text statt als Ganzzahl speichern."""
    version = int(con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()[0])
    if version >= 2:
        return
    for table, key in (("files", "id"), ("hashcache", "path")):
        rows = con.execute(f"SELECT {key}, dev, ino FROM {table} "
                           "WHERE typeof(dev)='integer' OR typeof(ino)='integer'").fetchall()
        con.executemany(f"UPDATE {table} SET dev=?, ino=? WHERE {key}=?",
                        [(fsutil.enc_id(d) if isinstance(d, int) else d,
                          fsutil.enc_id(i) if isinstance(i, int) else i, k) for k, d, i in rows])
    con.execute("UPDATE meta SET value=? WHERE key='schema'", (str(SCHEMA_VERSION),))


def get_meta(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row[0])


def set_meta(con: sqlite3.Connection, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
