"""Fensteroberfläche für DupeClean (Tkinter, Teil der Python-Standardbibliothek).

Ablauf im Fenster:
  1. Ordner auswählen, 2. Einstellungen, 3. Schritte: Duplikate suchen -> Probelauf -> Verschieben.
Nach jedem Schritt erscheint eine kurze Zusammenfassung; Bericht und Löschplan lassen sich öffnen.
Die eigentliche Arbeit erledigen dieselben Funktionen wie auf der Kommandozeile.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import __version__, apply as applymod, config, db, fsutil, hashing, progress, workspace
from .fsutil import fmt_duration, fmt_int, fmt_size
from .report import build as build_report
from .scanner import Scanner

SETTINGS_NAME = "oberflaeche.json"
PAD = 8


def _settings_path() -> str:
    return os.path.join(workspace.home_dir(), SETTINGS_NAME)


class _QueueWriter(io.TextIOBase):
    """Leitet print-Ausgaben des Arbeits-Threads ins Protokollfenster um."""

    def __init__(self, q: queue.Queue):
        self.q = q

    def write(self, s: str) -> int:
        if s:
            self.q.put(("log", s))
        return len(s)


class App:
    def __init__(self, root: tk.Tk, data_dir: str | None = None, quarantine: str | None = None):
        """``data_dir``/``quarantine`` legen Arbeits- und Quarantäneordner fest (sonst automatisch)."""
        self.root = root
        self.fixed_data_dir = data_dir
        self.fixed_quarantine = quarantine
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.busy = False
        self.step_started = 0.0
        self.log_lines: list[str] = []
        self.log_win: tk.Toplevel | None = None
        self.runs_win: tk.Toplevel | None = None
        # Zustand
        self.folders: list[str] = []
        self.data_dir: str | None = None
        self.plan_csv: str | None = None
        self.plan_html: str | None = None
        self.dry_ok_for: str | None = None   # Löschplan, für den ein Probelauf erfolgreich war
        self.last_run: str | None = None
        self.last_run_dir: str | None = None
        self.var_prefer = tk.StringVar()
        self.var_ssd = tk.BooleanVar(value=False)
        self.var_empty = tk.BooleanVar(value=True)
        self.var_thorough = tk.BooleanVar(value=False)
        self.var_stage = tk.StringVar(value="Bereit.")
        self.var_detail = tk.StringVar(value="")
        self._build()
        self._load_settings()
        self._refresh()
        progress.LISTENER = lambda d: self.q.put(("progress", d))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after = self.root.after(100, self._poll)

    # ================================================================== Aufbau
    def _build(self) -> None:
        r = self.root
        r.title(f"DupeClean {__version__} – doppelte Dateien und Ordner finden")
        r.minsize(760, 640)
        style = ttk.Style(r)
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Head.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Step.TButton", font=("Segoe UI", 10, "bold"), padding=(10, 6))
        style.configure("Hint.TLabel", foreground="#5f6b7a")

        main = ttk.Frame(r, padding=PAD)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        # ---------------------------------------------------------- 1. Ordner
        f1 = ttk.LabelFrame(main, text="1. Welche Ordner sollen geprüft werden?", padding=PAD,
                            style="Head.TLabelframe")
        f1.grid(row=0, column=0, sticky="ew")
        f1.columnconfigure(0, weight=1)
        lb_frame = ttk.Frame(f1)
        lb_frame.grid(row=0, column=0, sticky="nsew")
        lb_frame.columnconfigure(0, weight=1)
        self.lb = tk.Listbox(lb_frame, height=3, activestyle="none", font=("Segoe UI", 10),
                             selectmode="extended")
        self.lb.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lb_frame, orient="vertical", command=self.lb.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.lb.configure(yscrollcommand=sb.set)
        bf = ttk.Frame(f1)
        bf.grid(row=0, column=1, sticky="n", padx=(PAD, 0))
        self.btn_add = ttk.Button(bf, text="Ordner hinzufügen …", command=self._add_folder)
        self.btn_add.pack(fill="x")
        self.btn_del = ttk.Button(bf, text="Entfernen", command=self._remove_folder)
        self.btn_del.pack(fill="x", pady=(4, 0))
        ttk.Label(f1, text="Alle gewählten Ordner werden miteinander und in sich verglichen – "
                           "z. B. E:\\Sicherung oder einzelne Laptop-Ordner.",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        pf = ttk.Frame(f1)
        pf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        pf.columnconfigure(1, weight=1)
        ttk.Label(pf, text="Bevorzugt behalten (optional):").grid(row=0, column=0, sticky="w")
        self.cb_prefer = ttk.Combobox(pf, textvariable=self.var_prefer)
        self.cb_prefer.grid(row=0, column=1, sticky="ew", padx=(PAD, 4))
        self.btn_prefer = ttk.Button(pf, text="…", width=3, command=self._choose_prefer)
        self.btn_prefer.grid(row=0, column=2)
        ttk.Label(pf, text="Liegt eine Datei mehrfach vor, bleibt bevorzugt die Kopie in diesem Ordner "
                           "(z. B. der Ordner des neuesten Laptops).",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")

        # ---------------------------------------------------------- 2. Einstellungen
        f2 = ttk.LabelFrame(main, text="2. Einstellungen", padding=PAD, style="Head.TLabelframe")
        f2.grid(row=1, column=0, sticky="ew", pady=(PAD, 0))
        self.chk = [
            ttk.Checkbutton(f2, variable=self.var_ssd,
                            text="Die Daten liegen auf einer SSD (mehrere Dateien gleichzeitig lesen – "
                                 "bei USB-Festplatten aus lassen)"),
            ttk.Checkbutton(f2, variable=self.var_empty,
                            text="Ordner, die durch das Verschieben leer werden, ebenfalls entfernen"),
            ttk.Checkbutton(f2, variable=self.var_thorough,
                            text="Besonders gründlich: vor dem Verschieben auch die Originale "
                                 "vollständig neu lesen (langsamer)"),
        ]
        for c in self.chk:
            c.pack(anchor="w", pady=1)

        # ---------------------------------------------------------- 3. Schritte
        f3 = ttk.LabelFrame(main, text="3. Schritte", padding=PAD, style="Head.TLabelframe")
        f3.grid(row=2, column=0, sticky="ew", pady=(PAD, 0))
        for i in range(4):
            f3.columnconfigure(i, weight=1 if i < 3 else 0)
        self.btn_scan = ttk.Button(f3, text="①  Duplikate suchen", style="Step.TButton",
                                   command=self._step_scan)
        self.btn_dry = ttk.Button(f3, text="②  Probelauf", style="Step.TButton", command=self._step_dry)
        self.btn_exec = ttk.Button(f3, text="③  In Quarantäne verschieben", style="Step.TButton",
                                   command=self._step_exec)
        self.btn_stop = ttk.Button(f3, text="Stopp", command=self._stop)
        self.btn_scan.grid(row=0, column=0, sticky="ew")
        self.btn_dry.grid(row=0, column=1, sticky="ew", padx=PAD)
        self.btn_exec.grid(row=0, column=2, sticky="ew")
        self.btn_stop.grid(row=0, column=3, sticky="ns", padx=(PAD, 0))
        hints = ["liest nur – ändert nichts", "zeigt, was passieren würde – ändert nichts",
                 "verschiebt Duplikate, jederzeit rückgängig"]
        for i, h in enumerate(hints):
            ttk.Label(f3, text=h, style="Hint.TLabel", anchor="center").grid(row=1, column=i, sticky="ew")
        ttk.Label(f3, textvariable=self.var_stage, font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(PAD, 0))
        self.pb = ttk.Progressbar(f3, mode="determinate", maximum=1000)
        self.pb.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(2, 0))
        ttk.Label(f3, textvariable=self.var_detail, style="Hint.TLabel").grid(
            row=4, column=0, columnspan=4, sticky="w")

        # ---------------------------------------------------------- Ergebnis
        f4 = ttk.LabelFrame(main, text="Ergebnis", padding=PAD, style="Head.TLabelframe")
        f4.grid(row=3, column=0, sticky="nsew", pady=(PAD, 0))
        f4.columnconfigure(0, weight=1)
        f4.rowconfigure(0, weight=1)
        self.txt = tk.Text(f4, height=10, wrap="word", font=("Segoe UI", 10), relief="flat",
                           background=r.cget("background"), padx=4, pady=4, cursor="arrow")
        self.txt.grid(row=0, column=0, sticky="nsew")
        tsb = ttk.Scrollbar(f4, orient="vertical", command=self.txt.yview)
        tsb.grid(row=0, column=1, sticky="ns")
        self.txt.configure(yscrollcommand=tsb.set, state="disabled")
        self.txt.tag_configure("head", font=("Segoe UI", 11, "bold"))
        self.txt.tag_configure("ok", foreground="#1f7a3d")
        self.txt.tag_configure("warn", foreground="#9a5b00")
        self.txt.tag_configure("err", foreground="#b3261e")
        self.txt.tag_configure("hint", foreground="#5f6b7a")
        self.txt.tag_configure("big", font=("Segoe UI", 12, "bold"))
        af = ttk.Frame(f4)
        af.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        self.btn_report = ttk.Button(af, text="Bericht öffnen", command=self._open_report)
        self.btn_csv = ttk.Button(af, text="Löschplan öffnen (Excel)", command=self._open_csv)
        self.btn_quar = ttk.Button(af, text="Quarantäne öffnen", command=self._open_quarantine)
        self.btn_undo = ttk.Button(af, text="Rückgängig machen", command=self._undo_last)
        self.btn_runs = ttk.Button(af, text="Läufe verwalten …", command=self._open_runs)
        self.btn_log = ttk.Button(af, text="Protokoll", command=self._open_log)
        for b in (self.btn_report, self.btn_csv, self.btn_quar, self.btn_undo):
            b.pack(side="left", padx=(0, 4))
        self.btn_log.pack(side="right")
        self.btn_runs.pack(side="right", padx=(0, 4))
        self._say("Willkommen bei DupeClean", "head")
        self._say("Wählen Sie oben die Ordner aus (z. B. Ihre Sicherungsplatte) und klicken Sie dann auf "
                  "„① Duplikate suchen“. Dabei wird nichts verändert. Gelöscht wird nie direkt: "
                  "Duplikate wandern erst in eine Quarantäne und lassen sich vollständig zurückholen.\n")

    # ================================================================== Hilfen
    def _say(self, text: str, *tags: str) -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tags)
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _separator(self) -> None:
        self._say("─" * 60, "hint")

    def _refresh(self) -> None:
        self.lb.delete(0, "end")
        for f in self.folders:
            self.lb.insert("end", f)
        self.cb_prefer["values"] = self.folders
        idle = not self.busy
        state = lambda ok: "normal" if ok else "disabled"  # noqa: E731
        self.btn_add.configure(state=state(idle))
        self.btn_del.configure(state=state(idle and bool(self.folders)))
        self.cb_prefer.configure(state=state(idle))
        self.btn_prefer.configure(state=state(idle))
        for c in self.chk:
            c.configure(state=state(idle))
        has_plan = bool(self.plan_csv and os.path.isfile(self.plan_csv))
        self.btn_scan.configure(state=state(idle and bool(self.folders)))
        self.btn_dry.configure(state=state(idle and has_plan))
        self.btn_exec.configure(state=state(idle and has_plan and self.dry_ok_for == self.plan_csv))
        self.btn_stop.configure(state=state(not idle))
        self.btn_report.configure(state=state(bool(self.plan_html and os.path.isfile(self.plan_html))))
        self.btn_csv.configure(state=state(has_plan))
        self.btn_quar.configure(state=state(bool(self.last_run_dir and os.path.isdir(self.last_run_dir))))
        self.btn_undo.configure(state=state(idle and bool(self.last_run)))
        self.btn_runs.configure(state=state(idle))

    def _load_settings(self) -> None:
        try:
            with open(_settings_path(), encoding="utf-8") as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        self.folders = [p for p in s.get("ordner", []) if isinstance(p, str)]
        self.var_prefer.set(s.get("bevorzugt", ""))
        self.var_ssd.set(bool(s.get("ssd", False)))
        self.var_empty.set(bool(s.get("leere_ordner", True)))
        self.var_thorough.set(bool(s.get("gruendlich", False)))
        self.data_dir = s.get("daten")
        if s.get("loeschplan") and os.path.isfile(s["loeschplan"]):
            self.plan_csv = s["loeschplan"]
            self.plan_html = s.get("bericht")
            self._say(f"Letzter Löschplan: {self.plan_csv}", "hint")
            self._say("Sie können direkt mit „② Probelauf“ weitermachen oder neu suchen.\n", "hint")

    def _save_settings(self) -> None:
        s = {"ordner": self.folders, "bevorzugt": self.var_prefer.get().strip(),
             "ssd": self.var_ssd.get(), "leere_ordner": self.var_empty.get(),
             "gruendlich": self.var_thorough.get(), "daten": self.data_dir,
             "loeschplan": self.plan_csv, "bericht": self.plan_html}
        try:
            os.makedirs(workspace.home_dir(), exist_ok=True)
            with open(_settings_path(), "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ================================================================== Ordnerwahl
    def _add_folder(self) -> None:
        d = filedialog.askdirectory(parent=self.root, title="Ordner zum Prüfen auswählen", mustexist=True)
        if not d:
            return
        d = fsutil.norm(d)
        if any(fsutil.key(d) == fsutil.key(f) for f in self.folders):
            return
        if self.folders and fsutil.drive_root(d).casefold() != fsutil.drive_root(self.folders[0]).casefold():
            messagebox.showwarning(
                "Anderes Laufwerk", parent=self.root,
                message="Dieser Ordner liegt auf einem anderen Laufwerk als der erste.\n\n"
                        "Duplikate werden trotzdem gefunden. In die Quarantäne verschoben werden aber nur "
                        "Kopien auf dem Laufwerk des ersten Ordners; die übrigen werden übersprungen.")
        sysdrive = (os.environ.get("SystemDrive", "C:") + "\\").casefold()
        if fsutil.drive_root(d).casefold() == sysdrive and not getattr(self, "_warned_sys", False):
            self._warned_sys = True
            messagebox.showinfo(
                "Systemlaufwerk", parent=self.root,
                message="Dieser Ordner liegt auf dem Systemlaufwerk.\n\nDurchsuchen ist unbedenklich. "
                        "Programm- und AppData-Ordner sollten Sie aber nicht bereinigen – dort liegen gleiche "
                        "Dateien oft absichtlich mehrfach. DupeClean ist für Sicherungsplatten gedacht.")
        self.folders.append(d)
        self._invalidate_plan()
        self._refresh()

    def _remove_folder(self) -> None:
        sel = list(self.lb.curselection())
        if not sel:
            return
        for i in reversed(sel):
            del self.folders[i]
        self._invalidate_plan()
        self._refresh()

    def _choose_prefer(self) -> None:
        init = self.folders[0] if self.folders else None
        d = filedialog.askdirectory(parent=self.root, title="Diesen Ordner bevorzugt behalten",
                                    initialdir=init, mustexist=True)
        if d:
            self.var_prefer.set(fsutil.norm(d))

    def _invalidate_plan(self) -> None:
        self.dry_ok_for = None

    # ================================================================== Arbeits-Thread
    def _start(self, name: str, func, *args) -> None:
        if self.busy:
            return
        self.busy = True
        self.step_started = time.monotonic()
        self.var_stage.set(name + " …")
        self.var_detail.set("")
        self.pb.configure(mode="indeterminate")
        self.pb.start(15)
        self._refresh()
        self._save_settings()
        self.log_lines.append(f"\n===== {name} – {time.strftime('%d.%m.%Y %H:%M:%S')} =====\n")

        def run():
            result: dict = {}
            try:
                with contextlib.redirect_stdout(_QueueWriter(self.q)):
                    rc = func(result, *args)
                self.q.put(("done", name, rc, result))
            except Exception as e:  # noqa: BLE001 - alles anzeigen, nie still abbrechen
                self.q.put(("error", name, f"{e}\n\n{traceback.format_exc()}"))

        self.worker = threading.Thread(target=run, name="dupeclean-arbeit", daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, *rest = self.q.get_nowait()
                if kind == "log":
                    self.log_lines.append(rest[0])
                    self._log_append(rest[0])
                elif kind == "progress":
                    self._on_progress(rest[0])
                elif kind == "stage":
                    self.var_stage.set(rest[0])
                    self.var_detail.set("")
                    self.pb.configure(mode="indeterminate")
                    self.pb.start(15)
                elif kind == "done":
                    name, rc, result = rest
                    self._finish()
                    result["_callback"](rc, result)
                elif kind == "error":
                    name, msg = rest
                    self._finish()
                    self.var_stage.set(f"{name}: Fehler")
                    self._separator()
                    self._say(f"✖ {name} – unerwarteter Fehler", "head", "err")
                    self._say(msg.split("\n\n")[0], "err")
                    self._say("Details stehen im Protokoll.\n", "hint")
                    self.log_lines.append(msg)
                    self._log_append(msg)
        except queue.Empty:
            pass
        self._after = self.root.after(100, self._poll)

    def _finish(self) -> None:
        self.busy = False
        self.pb.stop()
        self.pb.configure(mode="determinate", value=0)
        self.var_detail.set("")
        self._refresh()
        self._save_settings()

    def _on_progress(self, d: dict) -> None:
        label = d["label"].replace("Stufe 2 Teil-Hash", "Vergleiche Dateianfang und -ende") \
            .replace("Stufe 3 Voll-Hash", "Vergleiche vollständigen Inhalt") \
            .replace("Durchsuchen", "Ordner durchsuchen")
        self.var_stage.set(label + (" – fertig" if d["done"] else " …"))
        parts = []
        if d["total_items"]:
            parts.append(f"{fmt_int(d['items'])} von {fmt_int(d['total_items'])} Dateien")
        else:
            parts.append(f"{fmt_int(d['items'])} Dateien")
        if d["total_bytes"]:
            parts.append(f"{fmt_size(d['bytes'])} von {fmt_size(d['total_bytes'])}")
        elif d["bytes"]:
            parts.append(fmt_size(d["bytes"]))
        if d["bytes"] and not d["done"]:
            parts.append(f"{fmt_size(d['rate'])}/s")
        if d["eta"] is not None:
            parts.append(f"noch ca. {fmt_duration(d['eta'])}")
        self.var_detail.set("  ·  ".join(parts))
        frac = None
        if d["total_bytes"]:
            frac = d["bytes"] / max(d["total_bytes"], 1)
        elif d["total_items"]:
            frac = d["items"] / max(d["total_items"], 1)
        if frac is None:
            if str(self.pb.cget("mode")) != "indeterminate":
                self.pb.configure(mode="indeterminate")
                self.pb.start(15)
        else:
            if str(self.pb.cget("mode")) != "determinate":
                self.pb.stop()
                self.pb.configure(mode="determinate")
            self.pb.configure(value=min(1000, int(frac * 1000)))

    def _stop(self) -> None:
        if self.busy:
            hashing.STOP.set()
            self.var_stage.set("Wird angehalten – die aktuelle Datei wird noch fertig bearbeitet …")

    def _elapsed(self) -> str:
        return fmt_duration(time.monotonic() - self.step_started)

    # ================================================================== Schritt 1
    def _step_scan(self) -> None:
        missing = [f for f in self.folders if not os.path.isdir(fsutil.lp(f))]
        if missing:
            messagebox.showerror("Ordner nicht gefunden", parent=self.root,
                                 message="Diese Ordner sind nicht erreichbar (Platte angeschlossen?):\n\n"
                                         + "\n".join(missing))
            return
        prefer = self.var_prefer.get().strip()
        if prefer and not os.path.isdir(fsutil.lp(prefer)):
            messagebox.showerror("Ordner nicht gefunden", parent=self.root,
                                 message=f"Der bevorzugte Ordner existiert nicht:\n\n{prefer}")
            return
        self._invalidate_plan()
        folders = list(self.folders)
        threads = 4 if self.var_ssd.get() else 1
        self._start("Duplikate suchen", self._work_scan, folders, prefer, threads)

    def _work_scan(self, result: dict, folders: list[str], prefer: str, threads: int) -> int:
        result["_callback"] = self._done_scan
        cfg = config.load()
        data_dir = workspace.data_dir_for_roots(folders, self.fixed_data_dir)
        result["data_dir"] = data_dir
        sc = Scanner(folders, data_dir, cfg, threads=threads)
        rc = sc.run()
        result["scan"] = sc.stats
        if rc != 0:
            return rc
        self.q.put(("stage", "Bericht und Löschplan werden erstellt …"))
        con = db.connect(data_dir)
        try:
            rep = build_report(con, cfg, [prefer] if prefer else [], None, data_dir)
        finally:
            con.close()
        result.update({"html": rep["html"], "csv": rep["csv"], "plan_id": rep["plan_id"],
                       "stats": rep["analysis"].stats})
        return 0

    def _done_scan(self, rc: int, r: dict) -> None:
        self._separator()
        if rc == 130:
            self.var_stage.set("Angehalten.")
            self._say("⏸ Suche angehalten", "head", "warn")
            self._say("Bereits berechnete Prüfsummen sind gespeichert. Ein erneuter Klick auf "
                      "„① Duplikate suchen“ macht an dieser Stelle weiter.\n")
            return
        if rc != 0:
            self.var_stage.set("Fehler.")
            self._say("✖ Suche fehlgeschlagen – Details im Protokoll.\n", "head", "err")
            return
        self.data_dir = r["data_dir"]
        self.plan_csv, self.plan_html = r["csv"], r["html"]
        st, sc = r["stats"], r.get("scan", {})
        self.var_stage.set(f"Suche abgeschlossen ({self._elapsed()}).")
        self._say(f"✔ Suche abgeschlossen (Dauer {self._elapsed()})", "head", "ok")
        self._say(f"Geprüft: {fmt_int(st['files'])} Dateien, {fmt_size(st['bytes'])}")
        if st["remove_items"]:
            self._say(f"Einsparbar: {fmt_size(st['savings'])}", "big")
            self._say(f"Gefunden: {fmt_int(st['groups_dirs'])} doppelte Ordner und "
                      f"{fmt_int(st['groups_files'])} doppelte Dateien – "
                      f"{fmt_int(st['remove_items'])} Einträge würden in die Quarantäne verschoben.")
        else:
            self._say("Keine Duplikate gefunden.", "big")
        notes = []
        if st["conflicts"]:
            notes.append(f"{fmt_int(st['conflicts'])} Namen mit unterschiedlichem Inhalt (mögliche Versionen, "
                         "werden nicht angetastet)")
        if st["subsets"]:
            notes.append(f"{fmt_int(st['subsets'])} Ordner, deren Inhalt komplett in einem anderen steckt")
        if st["errors"]:
            notes.append(f"{fmt_int(st['errors'])} Dateien/Ordner konnten nicht gelesen werden")
        for n in notes:
            self._say("• " + n, "warn" if "nicht gelesen" in n else "")
        if st["remove_items"]:
            self._say("\nNächster Schritt: Bericht ansehen. Wer etwas anders haben möchte, ändert im Löschplan "
                      "(Excel) die Spalte „Aktion“ auf „behalten“ und speichert als „CSV UTF-8“. "
                      "Danach „② Probelauf“.\n", "hint")
            self._open_report()
        else:
            self.plan_csv = None
            self._say("")
        self._refresh()

    # ================================================================== Schritt 2
    def _step_dry(self) -> None:
        self._start("Probelauf", self._work_apply, False, self.plan_csv,
                    self.var_thorough.get(), self.var_empty.get())

    def _work_apply(self, result: dict, execute: bool, csv_path: str, thorough: bool, empty: bool) -> int:
        result["_callback"] = self._done_exec if execute else self._done_dry
        result["csv_path"] = csv_path
        cfg = config.load()
        data_dir = workspace.resolve_data_dir(self.data_dir, csv_path)
        result["data_dir"] = data_dir
        return applymod.apply(csv_path, data_dir, cfg, execute=execute, thorough=thorough,
                              remove_empty=empty, quarantine_root=self.fixed_quarantine,
                              confirm=lambda _p: "ja", result=result)

    def _say_warnings(self, r: dict) -> None:
        w = r.get("warnings") or []
        for x in w[:5]:
            self._say("• " + x, "warn")
        if len(w) > 5:
            self._say(f"• … und {fmt_int(len(w) - 5)} weitere Hinweise (siehe Protokoll)", "warn")

    def _done_dry(self, rc: int, r: dict) -> None:
        self._separator()
        if rc != 0 or "would_move" not in r and r.get("planned", 0):
            self.var_stage.set("Fehler.")
            self._say("✖ Probelauf fehlgeschlagen – Details im Protokoll.\n", "head", "err")
            return
        self.var_stage.set("Probelauf abgeschlossen – nichts wurde verändert.")
        self._say("✔ Probelauf abgeschlossen – nichts wurde verändert", "head", "ok")
        if not r.get("planned"):
            self._say("Im Löschplan ist nichts zum Entfernen markiert.\n")
            self._say_warnings(r)
            return
        self._say(f"Würde verschieben: {fmt_int(r['would_move'])} Einträge, {fmt_size(r['would_move_bytes'])}",
                  "big")
        if r["would_skip"]:
            self._say(f"Würde überspringen: {fmt_int(r['would_skip'])}", "warn")
            for reason, n in r["skip_reasons"][:5]:
                self._say(f"   {fmt_int(n)} × {reason}", "warn")
        if r.get("changed"):
            self._say(f"Von Hand im Löschplan geändert: {fmt_int(r['changed'])} Einträge")
        self._say_warnings(r)
        if r["would_move"]:
            self.dry_ok_for = r["csv_path"]
            self._say("\nNächster Schritt: „③ In Quarantäne verschieben“. Dabei wird jede Datei noch einmal "
                      "vollständig mit dem Original verglichen.\n", "hint")
        self._refresh()

    # ================================================================== Schritt 3
    def _step_exec(self) -> None:
        q = self.fixed_quarantine or (workspace.default_quarantine(self.data_dir) if self.data_dir
                                      else "die Quarantäne")
        ok = messagebox.askyesno(
            "Duplikate verschieben?", parent=self.root, icon="warning",
            message="Die im Löschplan mit „entfernen“ markierten Duplikate werden jetzt in die Quarantäne "
                    f"verschoben:\n\n{q}\n\nVorher wird jede Datei erneut geprüft. Gelöscht wird nichts – "
                    "alles lässt sich mit „Rückgängig machen“ wiederherstellen.\n\nFortfahren?")
        if ok:
            self._start("Verschieben", self._work_apply, True, self.plan_csv,
                        self.var_thorough.get(), self.var_empty.get())

    def _done_exec(self, rc: int, r: dict) -> None:
        self._separator()
        if "run_id" not in r:
            self.var_stage.set("Fehler.")
            self._say("✖ Verschieben nicht möglich – Details im Protokoll.\n", "head", "err")
            self._say_warnings(r)
            return
        self.last_run, self.last_run_dir = r["run_id"], r["run_dir"]
        self.dry_ok_for = None
        if r["status"] == "abgebrochen":
            self.var_stage.set("Angehalten.")
            self._say("⏸ Verschieben angehalten", "head", "warn")
        else:
            self.var_stage.set(f"Verschieben abgeschlossen ({self._elapsed()}).")
            self._say(f"✔ Verschieben abgeschlossen (Dauer {self._elapsed()})", "head", "ok")
        self._say(f"In die Quarantäne verschoben: {fmt_int(r['moved'])} Einträge, {fmt_size(r['moved_bytes'])}",
                  "big")
        if r["skipped"]:
            self._say(f"Übersprungen (zur Sicherheit): {fmt_int(r['skipped'])}", "warn")
            for reason, n in r["skip_reasons"][:5]:
                self._say(f"   {fmt_int(n)} × {reason}", "warn")
        if r["errors"]:
            self._say(f"Fehler: {fmt_int(r['errors'])} – siehe Protokoll", "err")
        if r.get("removed_empty"):
            self._say(f"Leere Ordner entfernt: {fmt_int(r['removed_empty'])}")
        self._say(f"Lauf: {r['run_id']}\nQuarantäne: {r['run_dir']}", "hint")
        self._say("\nPrüfen Sie in Ruhe, ob alles passt. „Rückgängig machen“ holt alles zurück. Endgültig "
                  "löschen (Platz freigeben) können Sie später unter „Läufe verwalten“.\n", "hint")
        self._refresh()

    # ================================================================== Rückgängig / Läufe
    def _undo_last(self) -> None:
        if self.last_run:
            self._confirm_undo(self.last_run)

    def _confirm_undo(self, run: str) -> None:
        if messagebox.askyesno("Rückgängig machen?", parent=self.root,
                               message=f"Alle Einträge aus Lauf {run} werden an ihren ursprünglichen Ort "
                                       "zurückgelegt.\n\nFortfahren?"):
            self._start("Rückgängig machen", self._work_undo, run)

    def _work_undo(self, result: dict, run: str) -> int:
        result["_callback"] = self._done_undo
        result["run"] = run
        return applymod.undo(run, self._runs_data_dir(), result=result)

    def _done_undo(self, rc: int, r: dict) -> None:
        self._separator()
        if "restored" not in r:
            self._say("✖ Rückgängig machen fehlgeschlagen – Details im Protokoll.\n", "head", "err")
            return
        tag = "ok" if rc == 0 else "warn"
        self.var_stage.set("Wiederherstellung abgeschlossen.")
        self._say(f"✔ Lauf {r['run']} rückgängig gemacht" if rc == 0 else
                  f"Lauf {r['run']} teilweise rückgängig gemacht", "head", tag)
        self._say(f"Wiederhergestellt: {fmt_int(r['restored'])} Einträge", "big")
        if r["conflicts"]:
            self._say(f"Konflikte: {fmt_int(r['conflicts'])} – am alten Ort liegt inzwischen etwas anderes; "
                      "diese Kopien bleiben in der Quarantäne (siehe Protokoll).", "warn")
        if r["missing"]:
            self._say(f"Fehlen: {fmt_int(r['missing'])} (siehe Protokoll)", "err")
        if r["run"] == self.last_run:
            self.last_run = None
        self._say("")
        self._refresh()
        self._fill_runs()

    def _runs_data_dir(self) -> str | None:
        return self.data_dir or workspace.last_data_dir()

    def _open_runs(self) -> None:
        if not self._runs_data_dir():
            messagebox.showinfo("Läufe", parent=self.root, message="Es gibt noch keine Läufe.")
            return
        if self.runs_win is not None and self.runs_win.winfo_exists():
            self.runs_win.lift()
            self._fill_runs()
            return
        w = tk.Toplevel(self.root)
        w.title("DupeClean – Läufe")
        w.geometry("760x320")
        w.transient(self.root)
        self.runs_win = w
        cols = ("lauf", "status", "anzahl", "groesse", "ordner")
        tv = ttk.Treeview(w, columns=cols, show="headings", selectmode="browse")
        for c, t, wd in zip(cols, ("Lauf", "Status", "Verschoben", "Größe", "Quarantäne"),
                            (140, 150, 80, 80, 300)):
            tv.heading(c, text=t)
            tv.column(c, width=wd, anchor="e" if c in ("anzahl", "groesse") else "w")
        tv.pack(fill="both", expand=True, padx=PAD, pady=(PAD, 0))
        self.runs_tv = tv
        bf = ttk.Frame(w, padding=PAD)
        bf.pack(fill="x")

        def selected():
            s = tv.selection()
            return s[0] if s else None

        def do_undo():
            if selected() and not self.busy:
                self._confirm_undo(selected())

        def do_purge():
            if selected() and not self.busy:
                self._confirm_purge(selected())

        def do_open():
            run = selected()
            if run:
                d = tv.set(run, "ordner")
                if os.path.isdir(d):
                    os.startfile(d)

        ttk.Button(bf, text="Rückgängig machen", command=do_undo).pack(side="left")
        ttk.Button(bf, text="Endgültig löschen …", command=do_purge).pack(side="left", padx=4)
        ttk.Button(bf, text="Ordner öffnen", command=do_open).pack(side="left")
        ttk.Button(bf, text="Schließen", command=w.destroy).pack(side="right")
        self._fill_runs()

    def _fill_runs(self) -> None:
        if self.runs_win is None or not self.runs_win.winfo_exists():
            return
        tv = self.runs_tv
        tv.delete(*tv.get_children())
        labels = {"fertig": "in Quarantäne", "laeuft": "unterbrochen", "abgebrochen": "angehalten",
                  "rueckgaengig": "rückgängig gemacht", "teilweise rueckgaengig": "teilweise rückgängig",
                  "endgueltig geloescht": "endgültig gelöscht", "teilweise geloescht": "teilweise gelöscht"}
        dd = self._runs_data_dir()
        for row in applymod.run_table(dd) if dd else []:
            st = str(row.get("status", "?"))
            tv.insert("", "end", iid=row["lauf"], values=(
                row["lauf"], labels.get(st, st), fmt_int(row.get("verschoben", 0)),
                fmt_size(row.get("bytes", 0)), row["ordner"]))

    def _confirm_purge(self, run: str) -> None:
        ans = simpledialog.askstring(
            "Endgültig löschen", parent=self.runs_win or self.root,
            prompt=f"Die Quarantäne von Lauf {run} wird ENDGÜLTIG gelöscht.\n"
                   "Das kann nicht rückgängig gemacht werden.\n\nZum Bestätigen „ja“ eingeben:")
        if (ans or "").strip().casefold() == "ja":
            self._start("Endgültig löschen", self._work_purge, run)

    def _work_purge(self, result: dict, run: str) -> int:
        result["_callback"] = self._done_purge
        result["run"] = run
        return applymod.purge(run, self._runs_data_dir(), confirm=lambda _p: "ja")

    def _done_purge(self, rc: int, r: dict) -> None:
        self._separator()
        if rc == 0:
            self.var_stage.set("Quarantäne gelöscht.")
            self._say(f"✔ Quarantäne von Lauf {r['run']} endgültig gelöscht – der Platz ist frei.\n", "head", "ok")
            if r["run"] == self.last_run:
                self.last_run = self.last_run_dir = None
        else:
            self._say("✖ Löschen nicht vollständig – Details im Protokoll.\n", "head", "err")
        self._refresh()
        self._fill_runs()

    # ================================================================== Öffnen
    def _open_report(self) -> None:
        if self.plan_html and os.path.isfile(self.plan_html):
            os.startfile(self.plan_html)

    def _open_csv(self) -> None:
        if self.plan_csv and os.path.isfile(self.plan_csv):
            self.dry_ok_for = None  # nach Änderungen bitte erneut einen Probelauf
            self._refresh()
            os.startfile(self.plan_csv)

    def _open_quarantine(self) -> None:
        if self.last_run_dir and os.path.isdir(self.last_run_dir):
            os.startfile(self.last_run_dir)

    def _open_log(self) -> None:
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_win.lift()
            return
        w = tk.Toplevel(self.root)
        w.title("DupeClean – Protokoll")
        w.geometry("900x500")
        t = tk.Text(w, wrap="none", font=("Consolas", 9))
        ys = ttk.Scrollbar(w, orient="vertical", command=t.yview)
        xs = ttk.Scrollbar(w, orient="horizontal", command=t.xview)
        t.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        t.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        w.rowconfigure(0, weight=1)
        w.columnconfigure(0, weight=1)
        t.insert("end", "".join(self.log_lines) or "Noch keine Ausgaben.")
        t.see("end")
        self.log_win, self.log_text = w, t

    def _log_append(self, s: str) -> None:
        if self.log_win is not None and self.log_win.winfo_exists():
            self.log_text.insert("end", s)
            self.log_text.see("end")

    # ================================================================== Beenden
    def _on_close(self) -> None:
        if self.busy:
            if not messagebox.askyesno("Beenden?", parent=self.root,
                                       message="Es läuft noch ein Vorgang. Anhalten und beenden?\n\n"
                                               "(Er wird sauber beendet und kann später fortgesetzt "
                                               "bzw. rückgängig gemacht werden.)"):
                return
            hashing.STOP.set()
            if self.worker is not None:
                self.worker.join(timeout=15)
        self.close()

    def close(self) -> None:
        self._save_settings()
        progress.LISTENER = None
        try:
            self.root.after_cancel(self._after)
        except tk.TclError:
            pass
        self.root.destroy()


def main() -> None:
    os.environ.setdefault("DUPECLEAN_QUIET", "1")
    try:  # scharfe Schrift auf hochauflösenden Bildschirmen
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root)
    h = min(860, root.winfo_screenheight() - 80)
    w = min(940, root.winfo_screenwidth() - 40)
    root.geometry(f"{w}x{h}+20+20")
    root.mainloop()


if __name__ == "__main__":
    main()
