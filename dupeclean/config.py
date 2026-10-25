"""Konfiguration: eingebaute Standardwerte, optional überschrieben durch eine TOML-Datei."""
from __future__ import annotations

import copy
import fnmatch
import os
import re
import tomllib
from dataclasses import dataclass, field

PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_FILE = os.path.join(PROGRAM_DIR, "dupeclean.toml")

DEFAULTS: dict = {
    "ausschliessen": {
        "namen": [
            "$RECYCLE.BIN", "System Volume Information", "Thumbs.db", "desktop.ini",
            ".DS_Store", "~$*", ".dupeclean", "_DupeClean_Quarantaene",
        ],
        "pfade": [],
        "leere_dateien_ignorieren": True,
    },
    "behalten": {
        "reihenfolge": ["bevorzugt", "abwerten", "aeltestes", "tiefe", "laenge"],
        "bevorzugt": [],
        "abwerten": ["Kopie", "- Kopie", "Copy", "(1)", "Backup", "Temp", "Downloads"],
        "abwerten_regex": [r"\(\d+\)", r"Kopie \(\d+\)"],
    },
    "geschuetzt": [
        {"name": "Git-Repository", "eines": [".git"]},
        {"name": "node_modules", "ordnername": ["node_modules"]},
        {"name": "Python-Umgebung", "eines": ["pyvenv.cfg"]},
        {"name": "Python-Umgebung", "ordnername": [".venv"]},
        {"name": "Programmordner", "alle": ["*.exe", "*.dll"]},
        {"name": "Lightroom-Katalog", "eines": ["*.lrcat"]},
        {"name": "Thunderbird-/Firefox-Profil", "alle": ["prefs.js"],
         "eines": ["places.sqlite", "key4.db", "ImapMail", "Mail", "panacea.dat"]},
        {"name": "Outlook-Datendatei", "eines": ["*.pst", "*.ost"]},
    ],
}

KEEP_RULES = {"bevorzugt", "abwerten", "aeltestes", "tiefe", "laenge"}


class ConfigError(Exception):
    pass


def _match(name: str, patterns) -> bool:
    n = name.casefold()
    return any(fnmatch.fnmatchcase(n, p.casefold()) for p in patterns)


@dataclass
class ProtectRule:
    name: str
    ordnername: list = field(default_factory=list)
    alle: list = field(default_factory=list)
    eines: list = field(default_factory=list)

    def matches(self, dirname: str, entry_names: list[str]) -> bool:
        if not (self.ordnername or self.alle or self.eines):
            return False
        if self.ordnername and not _match(dirname, self.ordnername):
            return False
        for pat in self.alle:
            if not any(_match(e, [pat]) for e in entry_names):
                return False
        if self.eines and not any(_match(e, self.eines) for e in entry_names):
            return False
        return True


@dataclass
class Config:
    exclude_names: list
    exclude_paths: list
    ignore_empty_files: bool
    keep_order: list
    prefer: list
    devalue_words: list
    devalue_regex: list
    protect_rules: list
    source: str = "eingebaut"

    def is_excluded_name(self, name: str) -> bool:
        return _match(name, self.exclude_names)

    def devalue_patterns(self) -> list[re.Pattern]:
        pats = []
        for w in self.devalue_words:
            pats.append(re.compile(r"(?<!\w)" + re.escape(w) + r"(?!\w)", re.IGNORECASE))
        for r in self.devalue_regex:
            pats.append(re.compile(r, re.IGNORECASE))
        return pats

    def protect_reason(self, dirname: str, entry_names: list[str]) -> str | None:
        for rule in self.protect_rules:
            if rule.matches(dirname, entry_names):
                return rule.name
        return None


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | None = None) -> Config:
    data = copy.deepcopy(DEFAULTS)
    source = "eingebaut"
    if path is None and os.path.isfile(DEFAULT_CONFIG_FILE):
        path = DEFAULT_CONFIG_FILE
    if path:
        if not os.path.isfile(path):
            raise ConfigError(f"Konfigurationsdatei nicht gefunden: {path}")
        try:
            with open(path, "rb") as f:
                data = _merge(data, tomllib.load(f))
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"Fehler in der Konfigurationsdatei {path}: {e}") from e
        source = path
    ex = data.get("ausschliessen", {})
    keep = data.get("behalten", {})
    order = list(keep.get("reihenfolge", []))
    unknown = [r for r in order if r not in KEEP_RULES]
    if unknown:
        raise ConfigError(
            f"Unbekannte Regel(n) in behalten.reihenfolge: {', '.join(unknown)} "
            f"(erlaubt: {', '.join(sorted(KEEP_RULES))})")
    rules = []
    for r in data.get("geschuetzt", []):
        rules.append(ProtectRule(
            name=str(r.get("name", "Geschützt")),
            ordnername=list(r.get("ordnername", [])),
            alle=list(r.get("alle", [])),
            eines=list(r.get("eines", [])),
        ))
    cfg = Config(
        exclude_names=list(ex.get("namen", [])),
        exclude_paths=[os.path.abspath(p) for p in ex.get("pfade", [])],
        ignore_empty_files=bool(ex.get("leere_dateien_ignorieren", True)),
        keep_order=order,
        prefer=[os.path.abspath(p) for p in keep.get("bevorzugt", [])],
        devalue_words=list(keep.get("abwerten", [])),
        devalue_regex=list(keep.get("abwerten_regex", [])),
        protect_rules=rules,
        source=source,
    )
    try:
        cfg.devalue_patterns()
    except re.error as e:
        raise ConfigError(f"Ungültiger regulärer Ausdruck in behalten.abwerten_regex: {e}") from e
    # Eigene Ordner immer ausschließen
    for own in (".dupeclean", "_DupeClean_Quarantaene"):
        if not cfg.is_excluded_name(own):
            cfg.exclude_names.append(own)
    return cfg
