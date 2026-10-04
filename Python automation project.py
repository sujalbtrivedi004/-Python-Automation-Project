#!/usr/bin/env python3
"""
Cadence - a task-automation dashboard written in pure Python (standard library only).

* Database : SQLite (sqlite3) - automations + run history
* Backend  : http.server with a small router, PRG redirects, input validation
* Frontend : server-rendered HTML built from Python component functions,
             CSS generated from Python design tokens (responsive, light/dark)
* Automation: four built-in jobs + a background scheduler thread

Run:  python app.py        then open  http://127.0.0.1:8000
"""
from __future__ import annotations

import html
import os
import shutil
import sqlite3
import threading
import time
import zipfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import parse_qs, quote, urlparse

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CADENCE_DB", BASE / "cadence.db"))
BACKUP_DIR = BASE / "backups"
DEMO_DIR = BASE / "demo"
HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", "8000"))
SCHEDULER_TICK = 15  # seconds between "is anything due?" checks
BEAT_LENGTH = 24     # runs shown in each heartbeat strip
KEEP_RUNS = 200      # history kept per automation


# --------------------------------------------------------------------------
# Database layer
# --------------------------------------------------------------------------
def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db() -> None:
    with connect() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS automations (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                kind         TEXT    NOT NULL,
                target       TEXT    NOT NULL,
                interval_min INTEGER NOT NULL DEFAULT 0,
                enabled      INTEGER NOT NULL DEFAULT 1,
                created_at   INTEGER NOT NULL,
                last_run_at  INTEGER
            );
            CREATE TABLE IF NOT EXISTS runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                automation_id INTEGER NOT NULL
                              REFERENCES automations(id) ON DELETE CASCADE,
                started_at    INTEGER NOT NULL,
                duration_ms   INTEGER NOT NULL,
                status        TEXT    NOT NULL CHECK (status IN ('ok','failed')),
                trigger       TEXT    NOT NULL,
                message       TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_runs_auto ON runs(automation_id, started_at);
            """
        )


# --------------------------------------------------------------------------
# Automation jobs. Each takes (target, automation_row) -> (status, message)
# --------------------------------------------------------------------------
def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def safe_folder(target: str) -> Path:
    path = Path(target).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Folder not found: {path}")
    if path == Path(path.anchor) or path == Path.home().resolve():
        raise ValueError("Pick a specific folder, not your home or root folder.")
    return path


CATEGORIES = {
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".heic"},
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"},
    "Archives": {".zip", ".tar", ".gz", ".7z", ".rar"},
    "Media": {".mp3", ".wav", ".mp4", ".mov", ".mkv"},
    "Code": {".py", ".js", ".html", ".css", ".json", ".sql"},
}


def category_for(suffix: str) -> str:
    suffix = suffix.lower()
    return next((c for c, exts in CATEGORIES.items() if suffix in exts), "Other")


def job_organize(target: str, _a) -> tuple[str, str]:
    folder = safe_folder(target)
    moved, used = 0, set()
    for item in sorted(folder.iterdir()):
        if not item.is_file() or item.name.startswith("."):
            continue
        cat = category_for(item.suffix)
        dest_dir = folder / cat
        dest_dir.mkdir(exist_ok=True)
        dest, n = dest_dir / item.name, 1
        while dest.exists():  # never overwrite
            dest = dest_dir / f"{item.stem} ({n}){item.suffix}"
            n += 1
        shutil.move(str(item), str(dest))
        moved += 1
        used.add(cat)
    if not moved:
        return "ok", "Nothing to sort - the folder has no loose files."
    return "ok", f"Moved {moved} file{'s' if moved != 1 else ''} into {len(used)} folder{'s' if len(used) != 1 else ''}."


def job_backup(target: str, a) -> tuple[str, str]:
    folder = safe_folder(target)
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = BACKUP_DIR / f"a{a['id']}-{stamp}.zip"
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(folder):
            if Path(root).resolve() == BACKUP_DIR.resolve():
                continue
            for name in files:
                p = Path(root) / name
                if p.is_symlink():
                    continue
                zf.write(p, p.relative_to(folder))
                count += 1
    for old in sorted(BACKUP_DIR.glob(f"a{a['id']}-*.zip"))[:-5]:  # keep newest 5
        old.unlink(missing_ok=True)
    return "ok", f"Archived {count} file{'s' if count != 1 else ''} ({human_size(archive.stat().st_size)}) to backups/{archive.name}."


def job_report(target: str, _a) -> tuple[str, str]:
    folder = safe_folder(target)
    sizes: list[tuple[int, str]] = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            p = Path(root) / name
            if not p.is_symlink():
                sizes.append((p.stat().st_size, name))
    if not sizes:
        return "ok", "The folder is empty."
    total = sum(s for s, _ in sizes)
    biggest = ", ".join(f"{n} ({human_size(s)})" for s, n in sorted(sizes, reverse=True)[:3])
    return "ok", f"{len(sizes)} files, {human_size(total)} in total. Largest: {biggest}."


def job_site(target: str, _a) -> tuple[str, str]:
    if urlparse(target).scheme not in ("http", "https"):
        raise ValueError("Address must start with http:// or https://")
    req = urlrequest.Request(target, headers={"User-Agent": "Cadence/1.0"})
    began = time.perf_counter()
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            code = resp.status
    except urlerror.HTTPError as exc:
        code = exc.code
    ms = round((time.perf_counter() - began) * 1000)
    return ("ok" if code < 400 else "failed"), f"HTTP {code} in {ms} ms."


@dataclass(frozen=True)
class Kind:
    label: str
    blurb: str
    hint: str
    run: Callable


KINDS: dict[str, Kind] = {
    "organize": Kind("Sort a folder", "Moves loose files into Images, Documents, Archives and more.", "Folder path", job_organize),
    "backup": Kind("Back up a folder", "Saves a timestamped .zip and keeps the newest five.", "Folder path", job_backup),
    "report": Kind("Summarise a folder", "Counts files and lists the largest. Changes nothing.", "Folder path", job_report),
    "site": Kind("Check a website", "Requests a URL and records status and response time.", "Web address", job_site),
}

INTERVALS = [(0, "Only when I press Run"), (5, "Every 5 minutes"), (15, "Every 15 minutes"),
             (60, "Every hour"), (360, "Every 6 hours"), (1440, "Every day")]

_running: set[int] = set()
_running_lock = threading.Lock()


def run_automation(aid: int, trigger: str = "manual") -> bool:
    """Execute one automation and log the outcome. Returns False if it was already running."""
    with _running_lock:
        if aid in _running:
            return False
        _running.add(aid)
    try:
        with connect() as con:
            a = con.execute("SELECT * FROM automations WHERE id = ?", (aid,)).fetchone()
        if a is None:
            return False
        began, t0 = int(time.time()), time.perf_counter()
        try:
            status, message = KINDS[a["kind"]].run(a["target"], a)
        except Exception as exc:  # a failed job is data, not a crash
            status, message = "failed", f"{type(exc).__name__}: {exc}"
        ms = round((time.perf_counter() - t0) * 1000)
        with connect() as con:
            con.execute(
                "INSERT INTO runs(automation_id,started_at,duration_ms,status,trigger,message) VALUES (?,?,?,?,?,?)",
                (aid, began, ms, status, trigger, message[:500]),
            )
            con.execute("UPDATE automations SET last_run_at = ? WHERE id = ?", (began, aid))
            con.execute(
                "DELETE FROM runs WHERE automation_id = ? AND id NOT IN "
                "(SELECT id FROM runs WHERE automation_id = ? ORDER BY id DESC LIMIT ?)",
                (aid, aid, KEEP_RUNS),
            )
        return True
    finally:
        with _running_lock:
            _running.discard(aid)


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.wait(SCHEDULER_TICK):
        now = int(time.time())
        with connect() as con:
            due = con.execute(
                "SELECT id FROM automations WHERE enabled = 1 AND interval_min > 0 "
                "AND (last_run_at IS NULL OR last_run_at + interval_min * 60 <= ?)",
                (now,),
            ).fetchall()
        for row in due:
            run_automation(row["id"], "schedule")


# --------------------------------------------------------------------------
# Validation + data helpers
# --------------------------------------------------------------------------
def validate_new(form: dict[str, str]) -> tuple[dict, str | None]:
    name = form.get("name", "").strip()
    kind = form.get("kind", "")
    target = form.get("target", "").strip()
    try:
        interval = int(form.get("interval", "0"))
    except ValueError:
        interval = -1
    if not 1 <= len(name) <= 60:
        return {}, "Give the automation a name of 1 to 60 characters."
    if kind not in KINDS:
        return {}, "Choose what the automation should do."
    if not target or len(target) > 500:
        return {}, f"Enter a {KINDS[kind].hint.lower()}."
    if interval not in dict(INTERVALS):
        return {}, "Choose how often it should run."
    return {"name": name, "kind": kind, "target": target, "interval": interval}, None


def create_automation(data: dict) -> int:
    with connect() as con:
        cur = con.execute(
            "INSERT INTO automations(name,kind,target,interval_min,created_at) VALUES (?,?,?,?,?)",
            (data["name"], data["kind"], data["target"], data["interval"], int(time.time())),
        )
        return cur.lastrowid


def seed_demo() -> None:
    inbox = DEMO_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for name in ("invoice-march.pdf", "holiday.jpg", "notes.txt", "site.zip", "budget.csv", "talk.mp4", "random.xyz"):
        f = inbox / name
        if not f.exists() and not any(inbox.rglob(name)):
            f.write_text(f"sample content for {name}\n" * 40)
    for data in (
        {"name": "Tidy the inbox folder", "kind": "organize", "target": str(inbox), "interval": 60},
        {"name": "Back up the demo files", "kind": "backup", "target": str(DEMO_DIR), "interval": 360},
        {"name": "Weekly size report", "kind": "report", "target": str(DEMO_DIR), "interval": 1440},
        {"name": "Is example.com up?", "kind": "site", "target": "https://example.com", "interval": 15},
    ):
        create_automation(data)


def ago(ts: int | None) -> str:
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        return f"{d // 3600} h ago"
    return f"{d // 86400} d ago"


def clock(ts: int) -> str:
    return time.strftime("%d %b, %H:%M", time.localtime(ts))


def schedule_text(minutes: int) -> str:
    return dict(INTERVALS).get(minutes, f"Every {minutes} minutes") if minutes else "Runs only when you press Run"


# --------------------------------------------------------------------------
# UI components (each returns an HTML string)
# --------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value), quote=True)


def state_of(a: sqlite3.Row, last: sqlite3.Row | None) -> tuple[str, str]:
    if not a["enabled"]:
        return "paused", "Paused"
    if last is None:
        return "new", "Not run yet"
    return ("ok", "Healthy") if last["status"] == "ok" else ("failed", "Failing")


def heartbeat(runs: list[sqlite3.Row]) -> str:
    ordered = list(reversed(runs))  # oldest -> newest, newest on the right
    ticks = ['<li class="tick"></li>'] * (BEAT_LENGTH - len(ordered))
    for r in ordered:
        word = "succeeded" if r["status"] == "ok" else "failed"
        tip = f"{clock(r['started_at'])}: {r['message']}"
        ticks.append(f'<li class="tick {esc(r["status"])}" title="{esc(tip)}"><span class="sr">Run {word} {esc(ago(r["started_at"]))}</span></li>')
    label = f"Last {len(runs)} runs, oldest first" if runs else "No runs yet"
    return f'<ol class="beat" aria-label="{label}">{"".join(ticks)}</ol>'


def card(a: sqlite3.Row, runs: list[sqlite3.Row]) -> str:
    kind = KINDS[a["kind"]]
    last = runs[0] if runs else None
    state, state_label = state_of(a, last)
    if last:
        summary = f'<p class="last"><strong>{esc(ago(last["started_at"]))}</strong> {esc(last["message"])}</p>'
    else:
        summary = '<p class="last">Press Run to try it now.</p>'
    toggle = "Resume" if not a["enabled"] else "Pause"
    return f"""
<article class="card" data-state="{state}">
  <header class="card-head">
    <div class="titles">
      <h3>{esc(a['name'])}</h3>
      <p class="what">{esc(kind.label)}</p>
    </div>
    <span class="pill {state}">{state_label}</span>
  </header>
  <p class="target" title="{esc(a['target'])}">{esc(a['target'])}</p>
  {heartbeat(runs)}
  {summary}
  <p class="sched">{esc(schedule_text(a['interval_min']))}</p>
  <footer class="actions">
    <form method="post" action="/run/{a['id']}"><button class="btn primary">Run now</button></form>
    <form method="post" action="/toggle/{a['id']}"><button class="btn">{toggle}</button></form>
    <details class="danger">
      <summary class="btn quiet">Delete</summary>
      <form method="post" action="/delete/{a['id']}">
        <p>Delete this automation and its history?</p>
        <button class="btn warn">Yes, delete</button>
      </form>
    </details>
  </footer>
</article>"""


def run_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return '<p class="empty-note">Runs will appear here as soon as an automation has run.</p>'
    body = "".join(
        f"""<tr>
  <td data-label="When">{esc(clock(r['started_at']))}</td>
  <td data-label="Automation">{esc(r['name'])}</td>
  <td data-label="Result"><span class="pill {esc(r['status'])}">{'Succeeded' if r['status'] == 'ok' else 'Failed'}</span>
      <span class="msg">{esc(r['message'])}</span></td>
  <td data-label="Took">{esc(r['duration_ms'])} ms</td>
  <td data-label="Started by">{'Scheduler' if r['trigger'] == 'schedule' else 'You'}</td>
</tr>"""
        for r in rows
    )
    return f"""<div class="table-wrap"><table>
<thead><tr><th>When</th><th>Automation</th><th>Result</th><th>Took</th><th>Started by</th></tr></thead>
<tbody>{body}</tbody></table></div>"""


def new_form() -> str:
    kinds = "".join(
        f"""<label class="kind-option">
  <input type="radio" name="kind" value="{k}" {'checked' if i == 0 else ''} required>
  <span class="kind-body"><strong>{esc(v.label)}</strong><span>{esc(v.blurb)}</span><em>Needs a {esc(v.hint.lower())}</em></span>
</label>"""
        for i, (k, v) in enumerate(KINDS.items())
    )
    intervals = "".join(f'<option value="{m}">{esc(t)}</option>' for m, t in INTERVALS)
    return f"""
<details class="composer" id="new">
  <summary class="btn primary">New automation</summary>
  <form method="post" action="/create" class="composer-form">
    <fieldset class="kinds"><legend>What should it do?</legend>{kinds}</fieldset>
    <div class="fields">
      <label>Name<input name="name" maxlength="60" required placeholder="Tidy the downloads folder"></label>
      <label>Folder path or web address<input name="target" maxlength="500" required placeholder="/home/you/Downloads  or  https://example.com"></label>
      <label>How often<select name="interval">{intervals}</select></label>
    </div>
    <button class="btn primary">Create automation</button>
  </form>
</details>"""


def sidebar(counts: dict[str, int], current: str) -> str:
    items = [("all", "All automations"), ("ok", "Healthy"), ("failed", "Failing"), ("paused", "Paused")]
    current_attr = ' aria-current="page"'
    links = "".join(
        f'<a href="/?show={k}"{current_attr if k == current else ""}>{label}<span class="count">{counts[k]}</span></a>'
        for k, label in items
    )
    return f"""
<aside class="side">
  <a class="brand" href="/">Cadence</a>
  <p class="tag">Python task automation</p>
  <nav aria-label="Filter automations">{links}</nav>
  <p class="foot">The scheduler checks for due jobs every {SCHEDULER_TICK} seconds while this page's server is running.</p>
</aside>"""


def page(show: str, notice: tuple[str, str] | None) -> str:
    with connect() as con:
        automations = con.execute("SELECT * FROM automations ORDER BY created_at DESC, id DESC").fetchall()
        recent = con.execute(
            "SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER (PARTITION BY automation_id ORDER BY started_at DESC, id DESC) rn "
            "FROM runs r) WHERE rn <= ?",
            (BEAT_LENGTH,),
        ).fetchall()
        log = con.execute(
            "SELECT r.*, a.name FROM runs r JOIN automations a ON a.id = r.automation_id "
            "ORDER BY r.started_at DESC, r.id DESC LIMIT 12"
        ).fetchall()
        day = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(status='ok'),0) ok FROM runs WHERE started_at >= ?",
            (int(time.time()) - 86400,),
        ).fetchone()

    by_auto: dict[int, list[sqlite3.Row]] = {}
    for r in sorted(recent, key=lambda r: (r["started_at"], r["id"]), reverse=True):
        by_auto.setdefault(r["automation_id"], []).append(r)

    counts = {"all": len(automations), "ok": 0, "failed": 0, "paused": 0}
    visible = []
    for a in automations:
        runs = by_auto.get(a["id"], [])
        state, _ = state_of(a, runs[0] if runs else None)
        if state in counts:
            counts[state] += 1
        if show == "all" or state == show:
            visible.append(card(a, runs))

    active = sum(1 for a in automations if a["enabled"])
    if automations:
        headline = f"{active} of {len(automations)} automations are switched on."
    else:
        headline = "Nothing is automated yet."
    sub = (f"{day['ok']} of {day['n']} runs succeeded in the last 24 hours." if day["n"]
           else "No runs in the last 24 hours.")

    if not automations:
        grid = """<section class="empty">
  <h2>Set up your first automation</h2>
  <p>Create one with the button above, or load four ready-made examples that work on a small demo folder.</p>
  <form method="post" action="/seed"><button class="btn primary">Add sample automations</button></form>
</section>"""
    elif not visible:
        grid = '<section class="empty"><h2>Nothing matches this filter</h2><p><a href="/">Show all automations</a></p></section>'
    else:
        grid = f'<section class="grid" aria-label="Automations">{"".join(visible)}</section>'

    flash = ""
    if notice:
        flash = f'<p class="flash {notice[0]}" role="status">{esc(notice[1])}</p>'

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cadence - task automation</title>
<style>{CSS}</style>
</head><body>
<div class="shell">
{sidebar(counts, show)}
<main>
  <header class="top">
    <div><h1>{esc(headline)}</h1><p class="sub">{esc(sub)}</p></div>
    {new_form()}
  </header>
  {flash}
  {grid}
  <section class="log" aria-labelledby="log-h">
    <h2 id="log-h">Recent runs</h2>
    {run_table(log)}
  </section>
</main>
</div>
</body></html>"""


# --------------------------------------------------------------------------
# CSS - generated from Python design tokens
# --------------------------------------------------------------------------
LIGHT = dict(paper="#EDF1EE", surface="#FFFFFF", ink="#18262E", muted="#58686F", line="#D3DBD6",
             side="#163540", side_ink="#E6F0EC", brand="#2540C9", brand_ink="#FFFFFF",
             ok="#17804F", ok_bg="#DDF1E6", fail="#B93131", fail_bg="#F8E1DE",
             idle="#C9D2CD", warn_bg="#F7ECCB", warn="#7A5600")
DARK = dict(paper="#0F1A1F", surface="#17252C", ink="#E4EEEA", muted="#9AAFB0", line="#2C3F47",
            side="#0A1318", side_ink="#DCE9E4", brand="#7C93FF", brand_ink="#0B1230",
            ok="#58D190", ok_bg="#16382A", fail="#FF8C84", fail_bg="#40201F",
            idle="#2F434B", warn_bg="#3E3216", warn="#F0C560")


def tokens(t: dict) -> str:
    return "".join(f"--{k.replace('_', '-')}:{v};" for k, v in t.items())


CSS = ":root{" + tokens(LIGHT) + """color-scheme:light dark;
--radius:14px;--radius-sm:8px;
--font:"Avenir Next","Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}
@media (prefers-color-scheme:dark){:root{""" + tokens(DARK) + """}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.5 var(--font)}
h1,h2,h3,p{margin:0}
a{color:var(--brand)}
:focus-visible{outline:3px solid var(--brand);outline-offset:2px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* layout: sidebar + main, collapsing to a stacked page on small screens */
.shell{display:grid;grid-template-columns:15.5rem minmax(0,1fr);min-height:100vh}
.side{background:var(--side);color:var(--side-ink);padding:1.75rem 1.25rem;position:sticky;top:0;height:100vh;
  display:flex;flex-direction:column;gap:.35rem}
.brand{font-size:1.6rem;font-weight:700;letter-spacing:-.02em;color:inherit;text-decoration:none}
.tag{opacity:.7;font-size:.9rem;margin-bottom:1.5rem}
.side nav{display:flex;flex-direction:column;gap:.15rem}
.side nav a{display:flex;justify-content:space-between;align-items:center;color:inherit;text-decoration:none;
  padding:.55rem .75rem;border-radius:var(--radius-sm);transition:background .15s}
.side nav a:hover{background:rgb(255 255 255/.08)}
.side nav a[aria-current=page]{background:rgb(255 255 255/.16);font-weight:600}
.count{font-size:.85rem;opacity:.8}
.foot{margin-top:auto;font-size:.8rem;opacity:.6;line-height:1.4}
main{padding:2rem clamp(1rem,3vw,2.5rem) 3rem;max-width:80rem;width:100%}
.top{display:flex;flex-wrap:wrap;gap:1rem 2rem;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem}
h1{font-size:clamp(1.6rem,3.2vw,2.35rem);line-height:1.15;letter-spacing:-.025em;max-width:22ch}
.sub{color:var(--muted);margin-top:.4rem}

/* buttons */
.btn{font:inherit;font-weight:600;font-size:.92rem;border:1px solid var(--line);background:var(--surface);color:var(--ink);
  padding:.55rem 1rem;border-radius:var(--radius-sm);cursor:pointer;min-height:2.5rem;
  display:inline-flex;align-items:center;list-style:none;transition:transform .1s,background .15s}
.btn:hover{background:var(--paper)}
.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--brand);color:var(--brand-ink);border-color:var(--brand)}
.btn.primary:hover{filter:brightness(1.1);background:var(--brand)}
.btn.quiet{border-color:transparent;background:transparent;color:var(--muted)}
.btn.warn{background:var(--fail);border-color:var(--fail);color:#fff}
summary::-webkit-details-marker{display:none}

/* composer (no JavaScript: <details> + radio cards) */
.composer{flex:1 1 100%}
.composer>summary{width:max-content;margin-left:auto}
.composer-form{margin-top:1rem;background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  padding:1.25rem;display:grid;gap:1.25rem}
.kinds{border:0;padding:0;margin:0;display:grid;gap:.75rem;grid-template-columns:repeat(auto-fit,minmax(min(100%,14rem),1fr))}
.kinds legend{font-weight:700;margin-bottom:.6rem;padding:0}
.kind-option{display:block;cursor:pointer}
.kind-option input{position:absolute;opacity:0}
.kind-body{display:flex;flex-direction:column;gap:.25rem;height:100%;padding:.9rem 1rem;border:2px solid var(--line);
  border-radius:var(--radius-sm);font-size:.9rem;color:var(--muted);transition:border-color .15s,background .15s}
.kind-body strong{color:var(--ink);font-size:1rem}
.kind-body em{font-style:normal;font-size:.82rem}
.kind-option input:checked+.kind-body{border-color:var(--brand);background:color-mix(in srgb,var(--brand) 9%,transparent)}
.kind-option input:focus-visible+.kind-body{outline:3px solid var(--brand);outline-offset:2px}
.fields{display:grid;gap:1rem;grid-template-columns:repeat(auto-fit,minmax(min(100%,15rem),1fr))}
.fields label{display:flex;flex-direction:column;gap:.35rem;font-weight:600;font-size:.92rem}
input[name=name],input[name=target],select{font:inherit;font-weight:400;padding:.6rem .75rem;min-height:2.6rem;
  border:1px solid var(--line);border-radius:var(--radius-sm);background:var(--paper);color:var(--ink);width:100%}
.composer-form>.btn{width:max-content}

/* notices */
.flash{padding:.75rem 1rem;border-radius:var(--radius-sm);margin-bottom:1.25rem;font-weight:600}
.flash.ok{background:var(--ok-bg);color:var(--ok)}
.flash.err{background:var(--fail-bg);color:var(--fail)}

/* automation cards: responsive auto-fill grid */
.grid{display:grid;gap:1.1rem;grid-template-columns:repeat(auto-fill,minmax(min(100%,22rem),1fr))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:1.2rem;
  display:flex;flex-direction:column;gap:.7rem;min-width:0}
.card[data-state=failed]{border-color:var(--fail)}
.card[data-state=paused]{opacity:.82}
.card-head{display:flex;justify-content:space-between;gap:.75rem;align-items:flex-start}
.titles{min-width:0}
h3{font-size:1.1rem;letter-spacing:-.01em;overflow-wrap:anywhere}
.what{color:var(--muted);font-size:.88rem}
.target{font:.82rem/1.3 var(--mono);color:var(--muted);background:var(--paper);padding:.35rem .55rem;
  border-radius:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{font-size:.78rem;font-weight:700;padding:.15rem .6rem;border-radius:999px;white-space:nowrap;
  background:var(--paper);color:var(--muted)}
.pill.ok{background:var(--ok-bg);color:var(--ok)}
.pill.failed{background:var(--fail-bg);color:var(--fail)}
.pill.paused{background:var(--warn-bg);color:var(--warn)}

/* the signature element: a heartbeat strip of the last runs */
.beat{list-style:none;margin:.2rem 0;padding:0;display:grid;grid-template-columns:repeat(24,1fr);gap:3px;height:2.1rem}
.tick{background:var(--idle);border-radius:3px;opacity:.55}
.tick.ok{background:var(--ok);opacity:1}
.tick.failed{background:var(--fail);opacity:1}
.tick:last-child:not(:empty){animation:beat .9s ease-out 1}
@keyframes beat{0%{transform:scaleY(.4)}60%{transform:scaleY(1.12)}100%{transform:scaleY(1)}}
.last{font-size:.92rem;overflow-wrap:anywhere}
.sched{font-size:.85rem;color:var(--muted)}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:auto;padding-top:.6rem;border-top:1px solid var(--line);align-items:flex-start}
.actions form{margin:0}
.danger{margin-left:auto}
.danger[open]{flex:1 1 100%;margin-left:0}
.danger form{display:flex;gap:.75rem;align-items:center;flex-wrap:wrap;margin-top:.5rem;font-size:.9rem}

/* empty state */
.empty{background:var(--surface);border:2px dashed var(--line);border-radius:var(--radius);padding:2.5rem 1.5rem;
  display:grid;gap:.75rem;justify-items:start}
.empty p{color:var(--muted);max-width:52ch}
.empty-note{color:var(--muted)}

/* run log: a table on wide screens, stacked rows on phones */
.log{margin-top:2.5rem}
.log h2{font-size:1.25rem;margin-bottom:.8rem}
.table-wrap{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{text-align:left;font-size:.82rem;color:var(--muted);font-weight:600;padding:.7rem 1rem;border-bottom:1px solid var(--line)}
td{padding:.7rem 1rem;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.msg{display:block;color:var(--muted);margin-top:.25rem;overflow-wrap:anywhere}

@media (max-width:60rem){
  .shell{grid-template-columns:1fr}
  .side{position:static;height:auto;padding:1rem;flex-direction:row;flex-wrap:wrap;align-items:center;gap:.5rem 1rem}
  .tag,.foot{display:none}
  .side nav{flex-direction:row;overflow-x:auto;width:100%;gap:.4rem;padding-bottom:.2rem}
  .side nav a{white-space:nowrap;gap:.5rem}
}
@media (max-width:40rem){
  main{padding-top:1.25rem}
  .composer>summary{margin-left:0;width:100%;justify-content:center}
  .composer-form>.btn{width:100%;justify-content:center}
  thead{position:absolute;left:-9999px}
  table,tbody,tr,td{display:block}
  tr{padding:.6rem 0;border-bottom:1px solid var(--line)}
  tr:last-child{border-bottom:0}
  td{border:0;padding:.2rem 1rem;display:grid;grid-template-columns:6.5rem 1fr;gap:.5rem}
  td::before{content:attr(data-label);color:var(--muted);font-size:.82rem}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "Cadence/1.0"

    # -- helpers
    def send_html(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, kind: str = "", message: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", f"/?{kind}={quote(message)}" if message else "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if url.path == "/healthz":
            return self.send_html("ok")
        if url.path != "/":
            return self.send_html("<h1>Not found</h1><p><a href='/'>Back to Cadence</a></p>", 404)
        show = qs.get("show", ["all"])[0]
        show = show if show in ("all", "ok", "failed", "paused") else "all"
        notice = None
        if "ok" in qs:
            notice = ("ok", qs["ok"][0][:200])
        elif "err" in qs:
            notice = ("err", qs["err"][0][:200])
        self.send_html(page(show, notice))

    def do_POST(self) -> None:
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_html("<h1>Cross-site request blocked</h1>", 403)
        length = min(int(self.headers.get("Content-Length") or 0), 20_000)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode(errors="replace")).items()}
        parts = urlparse(self.path).path.strip("/").split("/")
        action = parts[0]
        aid = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None

        if action == "create":
            data, problem = validate_new(form)
            if problem:
                return self.redirect("err", problem)
            create_automation(data)
            return self.redirect("ok", f"Created \u201c{data['name']}\u201d. Press Run now to try it.")
        if action == "seed":
            seed_demo()
            return self.redirect("ok", "Added four sample automations.")
        if action == "run" and aid:
            started = run_automation(aid, "manual")
            return self.redirect("ok" if started else "err",
                                 "Run finished. Details are in Recent runs." if started else "That automation is already running.")
        if action == "toggle" and aid:
            with connect() as con:
                con.execute("UPDATE automations SET enabled = 1 - enabled WHERE id = ?", (aid,))
            return self.redirect()
        if action == "delete" and aid:
            with connect() as con:
                con.execute("DELETE FROM automations WHERE id = ?", (aid,))
            return self.redirect("ok", "Automation deleted.")
        self.send_html("<h1>Not found</h1>", 404)

    def log_message(self, fmt, *args) -> None:  # quieter console
        print(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}")


# --------------------------------------------------------------------------
# Optional built-in self-check (python automation_project.py --test)
# --------------------------------------------------------------------------
def run_tests() -> None:
    """Optional self-check. Run with:  python automation_project.py --test"""
    import tempfile
    import unittest

    class CadenceTests(unittest.TestCase):
        def setUp(self):
            self._saved = (DB_PATH, BACKUP_DIR)
            self.tmp = Path(tempfile.mkdtemp())
            globals()["DB_PATH"] = self.tmp / "test.db"
            globals()["BACKUP_DIR"] = self.tmp / "backups"
            init_db()

        def tearDown(self):
            globals()["DB_PATH"], globals()["BACKUP_DIR"] = self._saved

        def make(self, kind, target):
            return create_automation({"name": "t", "kind": kind, "target": str(target), "interval": 0})

        def test_validation(self):
            _, err = validate_new({"name": "x", "kind": "site", "target": "https://a.b", "interval": "5"})
            self.assertIsNone(err)
            for bad in ({"name": "", "kind": "site", "target": "u", "interval": "0"},
                        {"name": "x", "kind": "nope", "target": "u", "interval": "0"},
                        {"name": "x", "kind": "site", "target": "", "interval": "0"},
                        {"name": "x", "kind": "site", "target": "u", "interval": "7"}):
                self.assertIsNotNone(validate_new(bad)[1])

        def test_organize_moves_without_overwriting(self):
            d = self.tmp / "inbox"
            d.mkdir()
            (d / "a.pdf").write_text("1")
            (d / "b.png").write_text("2")
            aid = self.make("organize", d)
            run_automation(aid)
            self.assertTrue((d / "Documents" / "a.pdf").exists())
            (d / "a.pdf").write_text("again")
            run_automation(aid)
            self.assertTrue((d / "Documents" / "a (1).pdf").exists())

        def test_backup_and_report(self):
            d = self.tmp / "data"
            d.mkdir()
            (d / "f.txt").write_text("hello")
            run_automation(self.make("backup", d))
            run_automation(self.make("report", d))
            con = connect()
            try:
                statuses = [r["status"] for r in con.execute("SELECT status FROM runs ORDER BY id")]
            finally:
                con.close()
            self.assertEqual(statuses, ["ok", "ok"])
            self.assertEqual(len(list(BACKUP_DIR.glob("*.zip"))), 1)

        def test_failure_is_logged_not_raised(self):
            run_automation(self.make("report", self.tmp / "missing"))
            con = connect()
            try:
                row = con.execute("SELECT * FROM runs").fetchone()
            finally:
                con.close()
            self.assertEqual(row["status"], "failed")

        def test_page_escapes_html(self):
            create_automation({"name": "<script>x</script>", "kind": "report", "target": "/tmp", "interval": 0})
            out = page("all", None)
            self.assertNotIn("<script>x</script>", out)
            self.assertIn("&lt;script&gt;", out)

    suite = unittest.TestLoader().loadTestsFromTestCase(CadenceTests)
    unittest.TextTestRunner(verbosity=2).run(suite)


def main() -> None:
    import sys
    import webbrowser

    if "--test" in sys.argv:
        run_tests()
        return

    init_db()
    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(stop,), daemon=True).start()

    server, port = None, PORT
    for candidate in range(PORT, PORT + 10):  # use the next free port if 8000 is busy
        try:
            server = ThreadingHTTPServer((HOST, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("No free port found between %d and %d." % (PORT, PORT + 9))

    url = "http://%s:%d" % (HOST, port)
    print("Cadence is running at %s  (press Ctrl+C or the Stop button to quit)" % url)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    main()