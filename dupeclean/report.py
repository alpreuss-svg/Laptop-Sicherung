"""Unterbefehl ``report``: HTML-Bericht und Löschplan (CSV) erzeugen."""
from __future__ import annotations

import csv
import html
import json
import os
import time

from . import db, fsutil
from .analyze import Analysis, Analyzer
from .config import Config
from .fsutil import fmt_int, fmt_size

CSV_COLUMNS = ["Plan", "Gruppe", "Typ", "Aktion", "Pfad", "Größe", "Dateien", "Geändert", "Hinweis"]


def fmt_time(mtime_ns: int | None) -> str:
    if not mtime_ns:
        return ""
    return time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime_ns / 1e9))


def _out(msg: str = "") -> None:
    print(fsutil.console_safe(msg), flush=True)


def build(con, cfg: Config, prefer: list[str] | None, out_dir: str | None, data_dir: str) -> dict:
    scan = db.get_meta(con, "scan")
    if not scan or scan.get("phase") != "done":
        raise RuntimeError("Der letzte Scan ist nicht abgeschlossen. Bitte 'dupeclean scan' erneut "
                           "ausführen (er setzt an der Abbruchstelle fort).")
    t0 = time.monotonic()
    analysis = Analyzer(con, cfg, prefer).run()
    plan_id = "P" + time.strftime("%Y%m%d-%H%M%S")
    while con.execute("SELECT 1 FROM plans WHERE plan_id=?", (plan_id,)).fetchone():
        time.sleep(1)
        plan_id = "P" + time.strftime("%Y%m%d-%H%M%S")
    _store_plan(con, plan_id, scan, analysis)
    out_dir = out_dir or os.path.join(data_dir, "berichte", plan_id)
    fsutil.makedirs(out_dir)
    csv_path = os.path.join(out_dir, "loeschplan.csv")
    html_path = os.path.join(out_dir, "bericht.html")
    _write_csv(csv_path, plan_id, analysis)
    _write_html(html_path, plan_id, scan, analysis, cfg, csv_path)
    st = analysis.stats
    _out(f"Auswertung fertig in {time.monotonic() - t0:.1f} s.")
    _out(f"  Gruppen: {fmt_int(st['groups_dirs'])} Ordner, {fmt_int(st['groups_files'])} Dateien")
    _out(f"  Einsparbar: {fmt_size(st['savings'])} ({fmt_int(st['remove_items'])} Einträge zum Entfernen)")
    _out(f"  Namenskonflikte (gleicher Name, anderer Inhalt): {fmt_int(st['conflicts'])}")
    _out(f"  Ordner, deren Inhalt in einem anderen enthalten ist: {fmt_int(st['subsets'])}")
    if st["errors"]:
        _out(f"  Fehler beim Scan: {fmt_int(st['errors'])}")
    _out("")
    _out(f"Bericht:   {html_path}")
    _out(f"Löschplan: {csv_path}")
    _out("")
    _out("Den Löschplan können Sie in Excel prüfen und in der Spalte 'Aktion' zwischen")
    _out("'behalten' und 'entfernen' umstellen. Danach (Probelauf, ändert nichts):")
    _out(f'  dupeclean apply "{csv_path}"')
    return {"plan_id": plan_id, "html": html_path, "csv": csv_path, "analysis": analysis}


def _store_plan(con, plan_id: str, scan: dict, a: Analysis) -> None:
    rows = []
    for g in a.groups:
        for m in g.members:
            rows.append((plan_id, g.gid, g.kind, m.path, m.action, m.reason, m.size,
                         m.mtime_ns, g.content, 1 if m.locked else 0))
    con.execute("INSERT INTO plans(plan_id, created, scan_id, summary) VALUES(?,?,?,?)",
                (plan_id, time.strftime("%Y-%m-%d %H:%M:%S"), scan.get("scan_id"), json.dumps(a.stats)))
    con.executemany(
        "INSERT INTO plan_items(plan_id, group_id, kind, path, action, reason, size, mtime_ns, content, locked)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


def _write_csv(path: str, plan_id: str, a: Analysis) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow(CSV_COLUMNS)
        for g in a.groups:
            for m in sorted(g.members, key=lambda m: (m.action != "behalten", m.path)):
                w.writerow([plan_id, g.gid, "Ordner" if g.kind == "ordner" else "Datei", m.action,
                            m.path, fmt_size(m.size), m.files if g.kind == "ordner" else "",
                            fmt_time(m.mtime_ns), m.reason])


def _write_html(path: str, plan_id: str, scan: dict, a: Analysis, cfg: Config, csv_path: str) -> None:
    groups = []
    for g in a.groups:
        groups.append([g.gid, g.kind, g.size, g.savings,
                       [[m.path, m.size, m.mtime_ns // 1_000_000_000, m.action, m.reason,
                         m.files if g.kind == "ordner" else 0]
                        for m in sorted(g.members, key=lambda m: (m.action != "behalten", m.path))]])
    conflicts = [[name, n, [[e["path"], e["size"], e["mtime_ns"] // 1_000_000_000, e["content"]]
                            for e in entries]] for name, entries, n in a.name_conflicts]
    subsets = [[s["path"], s["size"], s["files"], s["container"], s["container_size"]] for s in a.subsets]
    errors = [[p, op, msg] for p, op, msg in a.errors]
    payload = {
        "plan": plan_id,
        "roots": scan.get("roots", []),
        "scanned": scan.get("finished", ""),
        "created": time.strftime("%d.%m.%Y %H:%M"),
        "csv": csv_path,
        "config": cfg.source,
        "stats": a.stats,
        "groups": groups,
        "conflicts": conflicts,
        "subsets": subsets,
        "errors": errors,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    page = HTML_TEMPLATE.replace("__DATA__", data).replace("__TITLE__", html.escape(f"DupeClean – Bericht {plan_id}"))
    with open(path, "w", encoding="utf-8", errors="surrogatepass") as f:
        f.write(page)


HTML_TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#5f6b7a;--line:#e1e5ea;--keep:#1f7a3d;--keep-bg:#e7f5ec;
--rm:#b3261e;--rm-bg:#fbeaea;--lock:#7a5b00;--lock-bg:#fff4d6;--acc:#2456c7}
@media (prefers-color-scheme: dark){:root{--bg:#14171c;--card:#1d2128;--fg:#e6e9ee;--muted:#9aa4b1;--line:#2d333c;
--keep:#6fd08f;--keep-bg:#183223;--rm:#f28b82;--rm-bg:#3a1d1c;--lock:#e6c55c;--lock-bg:#3a3117;--acc:#8ab4ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{padding:20px 24px 8px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
main,.grp,.flat{max-width:100%;overflow-x:auto}
.cards{display:flex;flex-wrap:wrap;gap:12px;padding:12px 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:160px}
.card b{display:block;font-size:20px}
.card span{color:var(--muted);font-size:12px}
nav{display:flex;gap:4px;padding:8px 24px 0;border-bottom:1px solid var(--line);flex-wrap:wrap}
nav button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px;color:var(--muted);
font:inherit;cursor:pointer}
nav button.on{color:var(--fg);border-bottom-color:var(--acc);font-weight:600}
.tools{display:flex;flex-wrap:wrap;gap:8px;padding:12px 24px;align-items:center}
.tools input,.tools select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg)}
.tools input[type=search]{flex:1;min-width:220px}
main{padding:0 24px 40px}
.grp{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:10px 0;overflow:hidden}
.gh{display:flex;gap:12px;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap;align-items:baseline}
.gh .id{font-family:Consolas,monospace;color:var(--muted)}
.gh .sv{margin-left:auto;font-weight:600}
table{border-collapse:collapse;width:100%}
td,th{padding:5px 12px;text-align:left;vertical-align:top;border-top:1px solid var(--line)}
tr:first-child td{border-top:0}
th{color:var(--muted);font-weight:600;font-size:12px}
td.p{word-break:break-all;font-family:Consolas,monospace;font-size:12.5px}
td.n{white-space:nowrap;text-align:right}
.tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;white-space:nowrap}
.t-keep{color:var(--keep);background:var(--keep-bg)}
.t-rm{color:var(--rm);background:var(--rm-bg)}
.t-lock{color:var(--lock);background:var(--lock-bg)}
.why{color:var(--muted);font-size:12px}
.more{margin:12px 0;padding:8px 14px;font:inherit;border:1px solid var(--line);border-radius:6px;
background:var(--card);color:var(--fg);cursor:pointer}
.empty{color:var(--muted);padding:20px 0}
.flat{background:var(--card);border:1px solid var(--line);border-radius:8px}
code{font-family:Consolas,monospace}
</style>
</head>
<body>
<header>
<h1>DupeClean – Bericht</h1>
<div class="sub" id="meta"></div>
</header>
<div class="cards" id="cards"></div>
<nav id="tabs"></nav>
<div class="tools">
<input type="search" id="q" placeholder="Suche in Pfaden …">
<select id="kind"><option value="">Dateien und Ordner</option><option value="ordner">nur Ordner</option>
<option value="datei">nur Dateien</option></select>
<select id="min"><option value="0">jede Größe</option><option value="1048576">ab 1 MB</option>
<option value="104857600">ab 100 MB</option><option value="1073741824">ab 1 GB</option></select>
<span class="sub" id="count"></span>
</div>
<main id="main"></main>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const PAGE = 100;
let tab = "groups", shown = PAGE;
const $ = s => document.querySelector(s);
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function size(n){const u=["B","KB","MB","GB","TB"];let i=0;n=+n;while(Math.abs(n)>=1024&&i<u.length-1){n/=1024;i++;}
  return (i?n.toFixed(1):n.toFixed(0)).replace(".",",")+" "+u[i];}
function num(n){return Number(n).toLocaleString("de-DE");}
function date(s){if(!s)return "";const d=new Date(s*1000);return d.toLocaleDateString("de-DE")+" "+
  d.toLocaleTimeString("de-DE",{hour:"2-digit",minute:"2-digit"});}
const S = D.stats;
$("#meta").innerHTML = "Plan <code>"+esc(D.plan)+"</code> · gescannt: "+esc(D.roots.join(", "))+
  " · Scan beendet "+esc(D.scanned)+" · Bericht erstellt "+esc(D.created)+"<br>Löschplan: <code>"+esc(D.csv)+"</code>";
const cards = [[size(S.savings),"einsparbar"],[num(S.remove_items),"Einträge zum Entfernen"],
  [num(S.groups_dirs),"Ordner-Gruppen"],[num(S.groups_files),"Datei-Gruppen"],
  [num(S.files),"Dateien gescannt ("+size(S.bytes)+")"],[num(S.conflicts),"Namenskonflikte"],
  [num(S.subsets),"Teilmengen-Ordner"],[num(S.errors),"Fehler"]];
$("#cards").innerHTML = cards.map(c=>'<div class="card"><b>'+esc(c[0])+'</b><span>'+esc(c[1])+'</span></div>').join("");
const TABS = [["groups","Duplikate ("+num(D.groups.length)+")"],["conflicts","Gleicher Name, anderer Inhalt ("+num(D.conflicts.length)+")"],
  ["subsets","Enthaltene Ordner ("+num(D.subsets.length)+")"],["errors","Fehler und Hinweise ("+num(D.errors.length)+")"]];
$("#tabs").innerHTML = TABS.map(t=>'<button data-t="'+t[0]+'">'+esc(t[1])+'</button>').join("");
document.querySelectorAll("#tabs button").forEach(b=>b.onclick=()=>{tab=b.dataset.t;shown=PAGE;render();});
["#q","#kind","#min"].forEach(s=>$(s).addEventListener("input",()=>{shown=PAGE;render();}));
function tag(a,why){if(a==="entfernen")return '<span class="tag t-rm">entfernen</span>';
  if(why&&why.startsWith("Geschützt")||why&&why.startsWith("Hardlink"))return '<span class="tag t-lock">behalten (geschützt)</span>';
  return '<span class="tag t-keep">behalten</span>';}
function filtered(){
  const q=$("#q").value.trim().toLowerCase(), k=$("#kind").value, mn=+$("#min").value;
  if(tab==="groups") return D.groups.filter(g=>(!k||g[1]===k)&&g[3]>=mn&&(!q||g[4].some(m=>m[0].toLowerCase().includes(q))));
  if(tab==="conflicts") return D.conflicts.filter(c=>(!q||c[0].toLowerCase().includes(q)||c[2].some(e=>e[0].toLowerCase().includes(q)))&&c[2].some(e=>e[1]>=mn));
  if(tab==="subsets") return D.subsets.filter(s=>s[1]>=mn&&(!q||s[0].toLowerCase().includes(q)||s[3].toLowerCase().includes(q)));
  return D.errors.filter(e=>!q||e[0].toLowerCase().includes(q)||e[2].toLowerCase().includes(q));
}
function render(){
  document.querySelectorAll("#tabs button").forEach(b=>b.classList.toggle("on",b.dataset.t===tab));
  $("#kind").style.display = tab==="groups"?"":"none";
  $("#min").style.display = tab==="errors"?"none":"";
  const items = filtered(), part = items.slice(0,shown);
  $("#count").textContent = num(items.length)+" Treffer";
  let h="";
  if(tab==="groups"){
    h = part.map(g=>'<div class="grp"><div class="gh"><span class="id">'+esc(g[0])+'</span><b>'+(g[1]==="ordner"?"Ordner":"Datei")+
      '</b><span>'+num(g[4].length)+' Kopien à '+size(g[2])+'</span><span class="sv">'+size(g[3])+' einsparbar</span></div><table>'+
      g[4].map(m=>'<tr><td>'+tag(m[3],m[4])+'</td><td class="p">'+esc(m[0])+'<div class="why">'+esc(m[4])+'</div></td><td class="n">'+
        size(m[1])+(g[1]==="ordner"?'<div class="why">'+num(m[5])+' Dateien</div>':'')+'</td><td class="n">'+date(m[2])+'</td></tr>').join("")+
      '</table></div>').join("");
  } else if(tab==="conflicts"){
    h = '<p class="sub">Diese Dateien heißen gleich, haben aber unterschiedlichen Inhalt (mögliche Versionen). Sie werden dadurch nicht entfernt; von jedem Inhalt bleibt immer mindestens eine Kopie.</p>'+
      part.map(c=>'<div class="grp"><div class="gh"><b>'+esc(c[0])+'</b><span>'+num(c[1])+' verschiedene Inhalte, '+num(c[2].length)+' Dateien</span></div><table>'+
      c[2].map(e=>'<tr><td class="p">'+esc(e[0])+'</td><td class="n">'+size(e[1])+'</td><td class="n">'+date(e[2])+'</td><td><code>'+esc(e[3])+'</code></td></tr>').join("")+
      '</table></div>').join("");
  } else if(tab==="subsets"){
    h = '<p class="sub">Nur ein Hinweis, hier wird nichts entfernt: Der gesamte Inhalt (alle Dateien) des linken Ordners liegt auch irgendwo im rechten Ordner.</p>'+
      '<div class="flat"><table><tr><th>Ordner</th><th>Größe</th><th>Dateien</th><th>enthalten in</th></tr>'+
      part.map(s=>'<tr><td class="p">'+esc(s[0])+'</td><td class="n">'+size(s[1])+'</td><td class="n">'+num(s[2])+'</td><td class="p">'+esc(s[3])+
        '<div class="why">'+size(s[4])+'</div></td></tr>').join("")+'</table></div>';
  } else {
    const L={lesen:"Lesefehler",ordner:"Ordner nicht lesbar",name:"Ungültiger Name",verknuepfung:"Verknüpfung übersprungen"};
    h = '<div class="flat"><table><tr><th>Art</th><th>Pfad</th><th>Meldung</th></tr>'+
      part.map(e=>'<tr><td>'+esc(L[e[1]]||e[1])+'</td><td class="p">'+esc(e[0])+'</td><td>'+esc(e[2])+'</td></tr>').join("")+'</table></div>';
  }
  if(!items.length) h += '<div class="empty">Keine Einträge.</div>';
  if(items.length>shown) h += '<button class="more" id="more">Weitere '+num(Math.min(PAGE,items.length-shown))+' von '+num(items.length-shown)+' anzeigen</button>';
  $("#main").innerHTML = h;
  const mb=$("#more"); if(mb) mb.onclick=()=>{shown+=PAGE;render();};
}
render();
</script>
</body>
</html>
"""
