"""Inhalts-Hashes: Teil-Hash (Anfang + Ende) und Voll-Hash."""
from __future__ import annotations

import hashlib
import os
import threading

from .fsutil import lp

try:  # optional, deutlich schneller
    import xxhash  # type: ignore
    ALGO = "xxh3_128"

    def _new():
        return xxhash.xxh3_128()
except ImportError:  # pragma: no cover - abhängig von der Installation
    xxhash = None
    ALGO = "blake2b256"

    def _new():
        return hashlib.blake2b(digest_size=32)

BLOCK = 64 * 1024
CHUNK = 1024 * 1024
_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_SEQUENTIAL", 0) | getattr(os, "O_NOINHERIT", 0)

STOP = threading.Event()


class Aborted(Exception):
    """Abbruch durch den Benutzer (Strg+C)."""


class FileChanged(Exception):
    pass


def new_hasher():
    return _new()


def _read_exact(fd: int, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        b = os.read(fd, n - len(out))
        if not b:
            break
        out += b
    return bytes(out)


def partial_hash(path: str, size: int):
    """Liefert (teil_hash, voll_hash_oder_None, dev, ino, nlink).

    Kleine Dateien (<= 128 KB) werden vollständig gelesen; dann ist der Teil-Hash
    zugleich der Voll-Hash.
    """
    fd = os.open(lp(path), _FLAGS)
    try:
        st = os.fstat(fd)
        if st.st_size != size:
            raise FileChanged("Größe hat sich während des Scans geändert")
        h = _new()
        if size <= 2 * BLOCK:
            data = _read_exact(fd, size)
            if len(data) != size:
                raise FileChanged("Datei ist kürzer als erwartet")
            h.update(data)
            digest = h.hexdigest()
            return digest, digest, st.st_dev, st.st_ino, st.st_nlink
        h.update(str(size).encode() + b":")
        h.update(_read_exact(fd, BLOCK))
        os.lseek(fd, size - BLOCK, os.SEEK_SET)
        h.update(_read_exact(fd, BLOCK))
        return "p" + h.hexdigest(), None, st.st_dev, st.st_ino, st.st_nlink
    finally:
        os.close(fd)


def full_hash(path: str, size: int | None = None, on_bytes=None, stop: threading.Event | None = STOP):
    """Voll-Hash über den gesamten Inhalt. Liefert (hash, dev, ino, nlink, size)."""
    fd = os.open(lp(path), _FLAGS)
    try:
        st = os.fstat(fd)
        if size is not None and st.st_size != size:
            raise FileChanged("Größe hat sich geändert")
        h = _new()
        buf = bytearray(CHUNK)
        view = memoryview(buf)
        total = 0
        with os.fdopen(os.dup(fd), "rb", buffering=0) as f:
            while True:
                if stop is not None and stop.is_set():
                    raise Aborted()
                n = f.readinto(buf)
                if not n:
                    break
                h.update(view[:n])
                total += n
                if on_bytes:
                    on_bytes(n)
        if size is not None and total != size:
            raise FileChanged("Datei hat sich beim Lesen geändert")
        return h.hexdigest(), st.st_dev, st.st_ino, st.st_nlink, total
    finally:
        os.close(fd)
