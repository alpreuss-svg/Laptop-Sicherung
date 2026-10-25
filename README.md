# DupeClean

DupeClean findet auf einer Sicherungsplatte doppelte **Dateien und ganze Ordner** – über den
**Inhalt**, nicht über den Namen – und entfernt Duplikate **umkehrbar**: Sie werden nicht gelöscht,
sondern in eine Quarantäne auf derselben Platte verschoben. Erst ein ausdrückliches `purge` löscht
endgültig.

Gedacht für eine externe USB-Festplatte, auf der mehrere alte Laptops jeweils in einem eigenen
Ordner gesichert sind, z. B. `E:\Sicherung\Laptop_Wohnzimmer\`.

## Voraussetzungen

- Windows 10/11
- Python 3.12 oder neuer (getestet mit 3.13). Prüfen mit `python --version`; falls nicht
  vorhanden: `winget install -e --id Python.Python.3.13`
- Keine weiteren Pakete nötig. Optional schneller: `pip install xxhash` (dann wird xxh3-128 statt
  BLAKE2b verwendet; der Cache wird dabei einmalig neu aufgebaut).

### Programm starten

**Am einfachsten:** im Explorer auf `dupeclean.bat` doppelklicken. Es öffnet sich ein Fenster, das
offen bleibt; dort tippen Sie die Befehle ein, z. B. `dupeclean scan E:\Sicherung`, und schließen
es am Ende mit `exit`.

Alternativ aus einer eigenen PowerShell oder Eingabeaufforderung mit vollem Pfad:

```
"C:\Users\alpre\.claude\Laptop sicherung\dupeclean.bat" scan E:\Sicherung
```

In den Beispielen unten steht kurz `dupeclean …` – das funktioniert so im Doppelklick-Fenster.

## Ablauf in fünf Schritten

| Schritt | Befehl | verändert Daten? |
|---|---|---|
| 1. Durchsuchen | `dupeclean scan E:\Sicherung` | nein |
| 2. Bericht + Löschplan | `dupeclean report` | nein |
| 3. Probelauf | `dupeclean apply <loeschplan.csv>` | nein |
| 4. Ausführen | `dupeclean apply <loeschplan.csv> --execute` | verschiebt in Quarantäne (nach Rückfrage) |
| 5a. Rückgängig | `dupeclean undo <Lauf>` | stellt alles wieder her |
| 5b. Endgültig löschen | `dupeclean purge <Lauf>` | löscht die Quarantäne (nach Rückfrage) |

`dupeclean runs` listet alle bisherigen Läufe. `dupeclean <befehl> --help` zeigt alle Optionen.

## Kompletter Beispielablauf

Die folgenden Ausgaben stammen aus einem echten Lauf über einen kleinen Beispielbaum
(Pfade auf `E:` umgeschrieben).

### 1. Scan

```
> dupeclean scan E:\Sicherung
Durchsuche E:\Sicherung
Durchsuchen:  15 Dateien  14,8 MB  7 Ordner  fertig in 00:00
Stufe 1: 11 Dateien mit gleicher Größe (0 aus dem Cache).
Stufe 2 Teil-Hash:  11/11 Dateien  …  fertig in 00:00
Stufe 3: 9 Dateien (14,7 MB) werden vollständig gelesen.
Stufe 3 Voll-Hash:  9/9 Dateien  14,7 MB/14,7 MB  …  fertig in 00:00

Scan abgeschlossen: 15 Dateien, 14,8 MB.
Gruppen identischer Dateien: 5, maximal einsparbar etwa 7,5 MB (vor Anwendung der Schutzregeln).
Arbeitsordner: E:\.dupeclean
Weiter mit:  dupeclean report
```

Während des Scans zeigt eine Zeile Dateien, MB/s und die geschätzte Restzeit.
**Strg+C** bricht sauber ab; derselbe Befehl setzt danach an der Abbruchstelle fort. Weil alle
Hashes in `E:\.dupeclean\dupeclean.sqlite` zwischengespeichert werden, sind Wiederholungsläufe
schnell (nur neue oder geänderte Dateien werden gelesen).

### 2. Bericht und Löschplan

```
> dupeclean report --prefer "E:\Sicherung\Laptop_Neu"
Auswertung fertig in 0.0 s.
  Gruppen: 1 Ordner, 2 Dateien
  Einsparbar: 7,5 MB (4 Einträge zum Entfernen)
  Namenskonflikte (gleicher Name, anderer Inhalt): 1
  Ordner, deren Inhalt in einem anderen enthalten ist: 1

Bericht:   E:\.dupeclean\berichte\P20261025-121836\bericht.html
Löschplan: E:\.dupeclean\berichte\P20261025-121836\loeschplan.csv
```

`bericht.html` öffnet sich ohne Internet in jedem Browser: Gesamtersparnis, Gruppen sortiert nach
einsparbarer Größe (je Gruppe Original und Duplikate mit Pfad, Größe, Datum und Begründung),
Ordner-Duplikate, „gleicher Name, anderer Inhalt“, enthaltene Ordner und die Fehlerliste – mit
Suchfeld und Filtern (nur Ordner/Dateien, Mindestgröße).

`loeschplan.csv` (UTF-8 mit BOM, Semikolon – öffnet sich direkt in Excel):

```
Plan;Gruppe;Typ;Aktion;Pfad;Größe;Dateien;Geändert;Hinweis
P20261025-121836;O000001;Ordner;behalten;E:\Sicherung\Laptop_Neu\Bilder;6,9 MB;3;14.07.2017 04:40;Original (bevorzugter Pfad)
P20261025-121836;O000001;Ordner;entfernen;E:\Sicherung\Laptop_Wohnzimmer\Bilder;6,9 MB;3;14.07.2017 04:40;Duplikat von E:\Sicherung\Laptop_Neu\Bilder
P20261025-121836;D000001;Datei;behalten;E:\Sicherung\Laptop_Neu\Downloads\Steuer 2016.pdf;341,8 KB;;18.08.2018 04:40;Original (weniger Kopie-/Backup-Kennzeichnungen im Pfad)
P20261025-121836;D000001;Datei;entfernen;E:\Sicherung\Laptop_Neu\Dokumente\Steuer 2016 - Kopie.pdf;341,8 KB;;10.05.2018 04:40;Duplikat von E:\Sicherung\Laptop_Neu\Downloads\Steuer 2016.pdf
P20261025-121836;D000001;Datei;entfernen;E:\Sicherung\Laptop_Wohnzimmer\Dokumente\Steuer 2016.pdf;341,8 KB;;14.07.2017 04:41;Duplikat von E:\Sicherung\Laptop_Neu\Downloads\Steuer 2016.pdf
P20261025-121836;D000002;Datei;behalten;E:\Sicherung\Laptop_Neu\Projekte\website\index.html;2,3 KB;;14.07.2017 04:40;Geschützt: liegt in Git-Repository (E:\Sicherung\Laptop_Neu\Projekte\website)
P20261025-121836;D000002;Datei;entfernen;E:\Sicherung\Laptop_Wohnzimmer\Website-Sicherung\index.html;2,3 KB;;14.07.2017 04:40;Duplikat von E:\Sicherung\Laptop_Neu\Projekte\website\index.html
```

In der Spalte **Aktion** können Sie `behalten` und `entfernen` frei umstellen (z. B. um eine andere
Kopie als Original zu behalten). Zeilen, die Sie löschen, gelten als `behalten`. Speichern Sie in
Excel möglichst als **„CSV UTF-8“**; ein normales „CSV (Trennzeichen-getrennt)“ wird ebenfalls
gelesen, Pfade mit exotischen Zeichen werden dann aber übersprungen. Größe, Datum und Hash
werden nicht aus der CSV gelesen, sondern aus der Datenbank – Excel kann also nichts verfälschen.

Nicht mit `entfernen` überschreibbar sind Einträge, die als *geschützt* oder *Hardlink* markiert
sind. Und wenn in einer Gruppe keine Kopie mehr übrig bliebe, wird die ganze Gruppe nicht angefasst.

### 3. Probelauf (Standard, ändert nichts)

```
> dupeclean apply "E:\.dupeclean\berichte\P20261025-121836\loeschplan.csv"
Löschplan P20261025-121836: 4 Einträge zum Entfernen (1 Ordner, 3 Dateien), zusammen 7,5 MB.

PROBELAUF – es wurde nichts verändert.
  Würde verschieben: 4 Einträge, 7,5 MB
  (Der vollständige Inhaltsvergleich findet erst bei --execute statt.)
  Liste aller Aktionen: E:\.dupeclean\berichte\P20261025-121836\probelauf_20261025-121836.txt

Zum Ausführen denselben Befehl mit --execute aufrufen.
```

### 4. Ausführen

```
> dupeclean apply "E:\.dupeclean\berichte\P20261025-121836\loeschplan.csv" --execute --leere-ordner
Löschplan P20261025-121836: 4 Einträge zum Entfernen (1 Ordner, 3 Dateien), zusammen 7,5 MB.

Die Einträge werden in die Quarantäne verschoben: E:\_DupeClean_Quarantaene
Vorher wird jede Datei erneut vollständig gelesen und mit dem Original verglichen.
Mit 'dupeclean undo <Lauf>' lässt sich alles wiederherstellen.
Zum Fortfahren 'ja' eingeben: ja

Lauf 2026-10-25_121836: fertig.
  Verschoben: 4 Einträge, 7,5 MB
  Protokoll: E:\_DupeClean_Quarantaene\2026-10-25_121836\_dupeclean_lauf\protokoll.log
  Quarantäne: E:\_DupeClean_Quarantaene\2026-10-25_121836
Rückgängig machen:     dupeclean undo 2026-10-25_121836
Endgültig löschen:     dupeclean purge 2026-10-25_121836
```

Direkt vor jeder Aktion wird erneut geprüft, sonst wird übersprungen und protokolliert:

- das Duplikat ist unverändert: Größe und Änderungsdatum wie beim Scan, und der **Voll-Hash wird
  neu berechnet** und muss stimmen; Hardlinks werden nie verschoben;
- das Original existiert noch und ist identisch (Größe und Datum wie beim Scan; bei geänderter
  Datei-ID oder mit `--gruendlich` wird auch das Original vollständig neu gelesen);
- bei Ordnern: Inhalt (alle Dateien, Größen, Daten) beider Ordner ist unverändert, jede Datei des
  Duplikats wird neu gehasht und mit dem Original verglichen.

Die Quarantäne liegt auf derselben Platte, deshalb ist das Verschieben auch bei großen Ordnern
sofort erledigt. Die ursprüngliche Ordnerstruktur bleibt darin erhalten:
`E:\_DupeClean_Quarantaene\<Lauf>\E\Sicherung\Laptop_Wohnzimmer\Bilder\…`.
Jede Aktion steht **vorher** im Journal (`journal.jsonl`) und danach im Protokoll. Strg+C beendet
nur noch die laufende Aktion; auch ein Absturz oder Stromausfall hinterlässt nichts, was `undo`
nicht zurückdrehen könnte. `--leere-ordner` entfernt anschließend Ordner, die durch diesen Lauf
leer geworden sind (auch das macht `undo` rückgängig).

### 5. Rückgängig machen oder endgültig löschen

```
> dupeclean runs
Lauf                 Status                    Verschoben      Größe  Ordner
2026-10-25_121836    fertig                             4     7,5 MB  E:\_DupeClean_Quarantaene\2026-10-25_121836

> dupeclean undo 2026-10-25_121836
Stelle Lauf 2026-10-25_121836 wieder her: 4 Einträge …
  Wiederhergestellt: 4
```

`undo` überschreibt nie etwas: Liegt am alten Ort inzwischen eine andere Datei, bleibt die Kopie
in der Quarantäne und der Konflikt wird gemeldet. Ein unterbrochenes `undo` kann einfach erneut
gestartet werden.

Wenn Sie nach einiger Zeit sicher sind, dass nichts fehlt:

```
> dupeclean purge 2026-10-25_121836
Lauf 2026-10-25_121836 – Status: fertig
Quarantäne: E:\_DupeClean_Quarantaene\2026-10-25_121836
Darin: 6 Dateien, 7,5 MB
Die Dateien werden ENDGÜLTIG gelöscht. Das kann nicht rückgängig gemacht werden.
Zum endgültigen Löschen 'ja' eingeben:
```

Die Protokolle des Laufs werden vorher nach `E:\.dupeclean\laeufe\<Lauf>\` kopiert.

## Wie DupeClean Duplikate erkennt

1. **Größe:** Dateien werden nach Größe gruppiert; Einzelgänger scheiden sofort aus.
2. **Teil-Hash:** erste und letzte 64 KB (kleine Dateien bis 128 KB werden dabei ganz gelesen).
3. **Voll-Hash:** BLAKE2b-256 (bzw. xxh3-128) über den ganzen Inhalt – nur für die verbleibenden
   Kandidaten. Dateien werden nacheinander und nach Pfad sortiert gelesen (HDD-freundlich);
   `--threads N` liest parallel und lohnt sich nur bei SSDs.

**Ordner:** Für jeden Ordner wird von unten nach oben ein Fingerabdruck über die sortierte Liste
(relativer Pfad, Größe, Inhalts-Hash) aller enthaltenen Dateien gebildet. Identische Ordner
werden als Ganzes behandelt, und nur die oberste Ebene wird gemeldet – Unterordner und Dateien
darin erscheinen nicht noch einmal einzeln. 0-Byte-Dateien zählen dabei mit ihrem Namen mit;
Systemdateien wie `Thumbs.db`/`desktop.ini` werden ignoriert. Ordner, deren Inhalt vollständig in
einem anderen Ordner steckt, erscheinen im Bericht als Hinweis (dort wird nichts entfernt).

**Gleicher Name, anderer Inhalt** (z. B. verschiedene Versionen von `Bewerbung.docx`) wird im
Bericht aufgeführt. Solche Dateien sind keine Duplikate und werden dadurch nie entfernt; von jedem
Inhalt bleibt immer mindestens eine Kopie.

**Nicht gezählt bzw. übersprungen:** Hardlinks (dieselbe physische Datei unter mehreren Namen),
Symlinks und Junctions (werden nicht verfolgt), 0-Byte-Dateien, `$RECYCLE.BIN`,
`System Volume Information`, `Thumbs.db`, `desktop.ini`, `.DS_Store`, `~$*`-Dateien und die
eigenen Ordner `.dupeclean` und `_DupeClean_Quarantaene`. Pfade über 260 Zeichen, Umlaute und
Unicode, schreibgeschützte und gesperrte Dateien werden korrekt behandelt; Zugriffsfehler stehen
im Bericht, der Scan läuft weiter. Ein Ordner mit Lesefehlern oder Verknüpfungen darin wird nie als
Ganzes entfernt.

## Welche Kopie bleibt

Die Regeln werden der Reihe nach angewendet (Reihenfolge in `dupeclean.toml` änderbar):

1. **bevorzugte Pfade** (`--prefer PFAD`, mehrfach möglich, oder `bevorzugt` in der Konfiguration),
2. **Abwertung** von Pfaden/Namen mit „Kopie“, „- Kopie“, „Copy“, „(1)“, „(2)“ …, „Backup“, „Temp“,
   „Downloads“ (nur ganze Wörter: „Temp“ trifft nicht „Template“),
3. **ältestes Änderungsdatum**,
4. **geringere Pfadtiefe**, dann **kürzerer Pfad**.

Immer gilt (und ist durch Tests abgesichert): **Aus jeder Gruppe bleibt mindestens eine Kopie
erhalten.**

## Geschützte Strukturen

Ordner, deren Dateien zusammengehören, werden nie einzeln ausgedünnt – sie können nur als ganzer
Ordner entfernt werden, wenn eine vollständig identische Kopie existiert. Erkannt werden
(konfigurierbar): Git-Repositories (`.git`), `node_modules`, Python-Umgebungen (`.venv`,
`pyvenv.cfg`), Programmordner (`*.exe` + `*.dll`), Lightroom-Kataloge (`*.lrcat`),
Thunderbird-/Firefox-Profile (`prefs.js` + `places.sqlite`/`key4.db`/`Mail`/`ImapMail`) und
Outlook-Datendateien (`*.pst`, `*.ost`). Liegt eine Datei sowohl in einer geschützten Struktur als
auch frei woanders, gilt die geschützte Kopie als Original; die freie Kopie darf entfernt werden.

## Konfiguration

`dupeclean.toml` neben `dupeclean.bat` wird automatisch gelesen (eine andere Datei per
`--config`). Dort lassen sich Ausschlüsse, Regelreihenfolge, bevorzugte Pfade, Abwertungs-Wörter
und geschützte Strukturen anpassen – die Datei ist ausführlich kommentiert.

## Ordner und Dateien von DupeClean

| Ort | Inhalt |
|---|---|
| `E:\.dupeclean\dupeclean.sqlite` | Hash-Cache und letzter Scan |
| `E:\.dupeclean\berichte\<Plan>\` | `bericht.html`, `loeschplan.csv`, Probelauf-Listen |
| `E:\.dupeclean\laeufe.json` | Liste aller Läufe |
| `E:\_DupeClean_Quarantaene\<Lauf>\` | verschobene Duplikate mit Originalpfad |
| `…\<Lauf>\_dupeclean_lauf\` | `lauf.json`, `journal.jsonl`, `protokoll.log` |

Der Arbeitsordner liegt standardmäßig im Wurzelverzeichnis der gescannten Platte; mit `--daten`
lässt sich ein anderer angeben. `report`, `apply`, `undo` und `runs` finden den zuletzt
verwendeten Arbeitsordner von selbst.

## Tests

```
cd "C:\Users\alpre\.claude\Laptop sicherung\tests"
python -m unittest -v
```

Die Tests erzeugen ihre Testbäume im Temp-Ordner (nie auf echten Daten) und decken ab: gleicher
Name/anderer Inhalt, gleicher Inhalt/anderer Name, gleiche Größe/anderer Inhalt, Unterschied nur
in der Dateimitte, identische und verschachtelte identische Ordner, Teilmengen-Ordner, Hardlink,
Junction, Symlink (nur mit Windows-Entwicklermodus, sonst übersprungen), 0-Byte-Dateien,
Pfade > 260 Zeichen, Umlaute/Emoji, schreibgeschützte und gesperrte Dateien, geschützte
Strukturen (`.git`, Programmordner), alle Behalten-Regeln, Excel-CSV in cp1252.

Nachgewiesen wird außerdem:

- **nie wird die letzte Kopie entfernt** – auch nicht bei 25 zufällig erzeugten Bäumen mit zufällig
  von Hand geänderten Löschplänen, bei veränderten oder gelöschten Originalen und wenn alle
  Kopien einer Gruppe auf „entfernen“ gesetzt werden;
- **undo stellt alles bitgenau wieder her** (SHA-256 aller Dateien vorher = nachher, inkl.
  Änderungsdatum und Schreibschutz);
- **ein Abbruch mitten im Lauf hinterlässt keinen inkonsistenten Zustand** – Strg+C im Scan und in
  `apply`, harter Prozessabbruch während der Hash-Phase und ein simulierter Absturz direkt nach
  dem Verschieben: Jede Datei liegt danach genau einmal vor (am Ort oder in der Quarantäne), und
  `undo` stellt den Ausgangszustand her.

Geschwindigkeitsmessung mit einem großen erzeugten Baum:

```
python tests\benchmark.py 100000
```

Ergebnis auf dem Entwicklungsrechner (Windows 11, interne SSD, Python 3.13, BLAKE2b) mit
100 020 Dateien / 1,1 GB in fünf „Laptop“-Ordnern, davon 35 % kopierte Ordner und 15 %
verstreute Einzelkopien:

| Schritt | Dauer |
|---|---|
| Testbaum erzeugen | 57 s |
| `scan`, erster Lauf | 28,3 s (≈ 3 500 Dateien/s) |
| `scan`, Wiederholung mit Hash-Cache | 12,4 s |
| `scan --threads 4 --neu` (Cache) | 12,6 s |
| `report` (Analyse, HTML, CSV) | 3,3 s |
| `apply`, Probelauf mit 10 462 Einträgen | 16,7 s |

Gefunden: 486 Ordner- und 7 564 Dateigruppen, 550 MB einsparbar (10 462 Einträge),
`bericht.html` 20 MB, `loeschplan.csv` 4,8 MB.

Die Werte sind eine Obergrenze: Die frisch erzeugten Dateien lagen im Windows-Dateicache. Auf
einer USB-Festplatte bestimmt die Platte das Tempo – erfahrungsgemäß 80–150 MB/s für große
Dateien und deutlich weniger bei vielen kleinen. Ein erster Scan von einigen hundert GB dauert
daher eher Stunden als Minuten; jeder weitere Scan liest dank Cache nur noch Neues.
