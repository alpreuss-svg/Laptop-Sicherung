"""Kommandozeile: dupeclean scan | report | apply | undo | purge | runs."""
from __future__ import annotations

import argparse
import os
import sys

from . import __version__, apply as applymod, config, db, fsutil, workspace
from .report import build as build_report
from .scanner import Scanner


class _Fmt(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix if prefix is not None else "Aufruf: ")


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _Fmt)
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "Argumente"
        self._optionals.title = "Optionen"
        self.add_argument("-h", "--help", action="help", help="diese Hilfe anzeigen")

    def error(self, message):
        for en, de in (("the following arguments are required", "folgende Angaben fehlen"),
                       ("unrecognized arguments", "unbekannte Angaben"),
                       ("invalid choice", "ungültige Auswahl"),
                       ("invalid int value", "keine gültige Zahl"),
                       ("expected one argument", "erwartet einen Wert"),
                       ("argument ", "Angabe "), ("choose from", "möglich sind")):
            message = message.replace(en, de)
        self.print_usage(sys.stderr)
        sys.stderr.write(f"Fehler: {message}\n")
        sys.exit(2)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="dupeclean",
        description="DupeClean findet doppelte Dateien und Ordner über ihren Inhalt und "
                    "verschiebt Duplikate umkehrbar in eine Quarantäne.",
        epilog="Ablauf: scan -> report -> apply (Probelauf) -> apply --execute -> ggf. undo / purge")
    p.add_argument("--version", action="version", version=f"DupeClean {__version__}",
                   help="Versionsnummer anzeigen")
    sub = p.add_subparsers(dest="cmd", metavar="BEFEHL", parser_class=_Parser)

    def common(sp):
        sp.add_argument("--daten", metavar="ORDNER",
                        help="Arbeitsordner für Cache, Berichte und Laufliste "
                             "(Standard: <Laufwerk>:\\.dupeclean bzw. der zuletzt verwendete)")
        sp.add_argument("--config", metavar="DATEI", help="Konfigurationsdatei (TOML)")

    s = sub.add_parser("scan", help="Ordner durchsuchen und Inhalts-Hashes berechnen")
    s.add_argument("ordner", nargs="+", help="zu durchsuchende(r) Ordner, z. B. E:\\Sicherung")
    s.add_argument("--threads", type=int, default=1, metavar="N",
                   help="parallele Lesevorgänge (nur für SSDs sinnvoll; Standard 1 = HDD-freundlich)")
    s.add_argument("--neu", action="store_true",
                   help="unterbrochenen Scan nicht fortsetzen, sondern neu beginnen (Cache bleibt)")
    common(s)

    r = sub.add_parser("report", help="Bericht (HTML) und Löschplan (CSV) erzeugen")
    r.add_argument("--prefer", action="append", default=[], metavar="PFAD",
                   help="Kopien in diesem Pfad bevorzugt behalten (mehrfach angebbar)")
    r.add_argument("--ausgabe", metavar="ORDNER", help="Zielordner für Bericht und Löschplan")
    common(r)

    a = sub.add_parser("apply", help="Löschplan anwenden (Standard: Probelauf)")
    a.add_argument("plan", help="Löschplan (CSV)")
    a.add_argument("--execute", action="store_true",
                   help="wirklich verschieben (nach Zusammenfassung und Rückfrage)")
    a.add_argument("--gruendlich", action="store_true",
                   help="auch jedes Original vollständig neu einlesen (langsamer)")
    a.add_argument("--leere-ordner", action="store_true", dest="leere_ordner",
                   help="danach Ordner entfernen, die durch diesen Lauf leer geworden sind")
    a.add_argument("--quarantaene", metavar="ORDNER",
                   help="Quarantäne-Ordner (Standard: <Laufwerk>:\\_DupeClean_Quarantaene)")
    common(a)

    u = sub.add_parser("undo", help="einen Lauf vollständig rückgängig machen")
    u.add_argument("lauf", help="Lauf-ID (siehe 'dupeclean runs') oder Pfad des Laufs in der Quarantäne")
    common(u)

    g = sub.add_parser("purge", help="Quarantäne eines Laufs endgültig löschen (nach Rückfrage)")
    g.add_argument("lauf", help="Lauf-ID oder Pfad des Laufs in der Quarantäne")
    common(g)

    ls = sub.add_parser("runs", help="bisherige Läufe auflisten")
    common(ls)
    return p


def _say(msg: str) -> None:
    print(fsutil.console_safe(msg), flush=True)


def main(argv: list[str] | None = None, confirm=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    if confirm is None:
        confirm = input
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    try:
        cfg = config.load(args.config)
        if args.cmd == "scan":
            data_dir = workspace.data_dir_for_roots(args.ordner, args.daten)
            return Scanner(args.ordner, data_dir, cfg, threads=args.threads, fresh=args.neu).run()
        if args.cmd == "report":
            data_dir = workspace.resolve_data_dir(args.daten)
            con = db.connect(data_dir)
            try:
                build_report(con, cfg, args.prefer, args.ausgabe, data_dir)
            finally:
                con.close()
            return 0
        if args.cmd == "apply":
            if not os.path.isfile(args.plan):
                _say(f"Fehler: Löschplan nicht gefunden: {args.plan}")
                return 2
            data_dir = workspace.resolve_data_dir(args.daten, args.plan)
            return applymod.apply(args.plan, data_dir, cfg, execute=args.execute, thorough=args.gruendlich,
                                  remove_empty=args.leere_ordner, quarantine_root=args.quarantaene,
                                  confirm=confirm)
        if args.cmd == "undo":
            data_dir = _optional_data_dir(args.daten)
            return applymod.undo(args.lauf, data_dir)
        if args.cmd == "purge":
            data_dir = _optional_data_dir(args.daten)
            return applymod.purge(args.lauf, data_dir, confirm=confirm)
        if args.cmd == "runs":
            return applymod.list_runs(workspace.resolve_data_dir(args.daten))
    except (config.ConfigError, workspace.WorkspaceError, applymod.ApplyError, RuntimeError) as e:
        _say(f"Fehler: {e}")
        return 2
    except KeyboardInterrupt:
        _say("\nAbgebrochen.")
        return 130
    return 0


def _optional_data_dir(explicit: str | None) -> str | None:
    if explicit:
        return fsutil.norm(explicit)
    return workspace.last_data_dir()
