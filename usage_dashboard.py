#!/usr/bin/env python3
"""Claude Code usage dashboard.

Copies the local Claude Code transcripts (~/.claude/projects/**/*.jsonl) into
a permanent SQLite history, aggregates token usage per model / project / day,
prices it at API rates (including prompt-cache write/read pricing), and renders
a self-contained HTML dashboard.

Usage:
    python usage_dashboard.py            # generate dashboard.html and open it
    python usage_dashboard.py --no-open  # just generate
    python usage_dashboard.py --serve    # serve on http://localhost:8377 (regenerates per request)
    python usage_dashboard.py --import-only  # just update the history DB
    claude-use --install                 # (release exe) install into ~/.local/bin
    claude-use --uninstall               # remove it again; keeps history and settings

No dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tomllib
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Bump in the release PR; the release build fails unless the tag is v<this>.
__version__ = "2.0.0"

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# template.html is bundled inside a PyInstaller build, so this works frozen too.
TEMPLATE = Path(__file__).parent / "template.html"

# XDG layout on every OS, Windows included (~ is %USERPROFILE%).
APP = "claude-use"


def _xdg_base(var: str, default: str) -> Path:
    base = os.environ.get(var, "")
    return Path(base) if base and Path(base).is_absolute() else Path.home() / default


def _xdg(var: str, default: str) -> Path:
    return _xdg_base(var, default) / APP


BIN_DIR = Path.home() / ".local" / "bin"  # no XDG variable for this one
INSTALLED_EXE = BIN_DIR / ("claude-use.exe" if os.name == "nt" else "claude-use")
CONFIG_FILE = _xdg("XDG_CONFIG_HOME", ".config") / "config.toml"
# Permanent history: Claude Code deletes transcripts after cleanupPeriodDays
# (default 30), so every run copies them into this DB first. Override with --db.
DB_NAME = "usage.sqlite"
DEFAULT_DB = _xdg("XDG_DATA_HOME", ".local/share") / DB_NAME
# Regenerated on every run, so it's cache.
OUT_HTML = _xdg("XDG_CACHE_HOME", ".cache") / "dashboard.html"

# What you actually pay for the subscription, USD/month. Change if you're on Max.
SUBSCRIPTION_MONTHLY = 20.00
SUBSCRIPTION_NAME = "Pro"

# API prices, USD per million tokens: (input, output, cache-read multiplier).
# Matched as substrings against the model id, first hit wins, so the more
# specific key must come first ("opus-4-1" before "opus").
# Cache writes multiply the input rate: 5m x1.25, 1h x2. Cache reads are x0.1
# everywhere except Fable/Mythos 5.1, which read at x0.025 ($0.25/MTok).
PRICING = (
    ("fable-5-1",  10.0, 50.0, 0.025),
    ("mythos-5-1", 10.0, 50.0, 0.025),
    ("fable",      10.0, 50.0, 0.10),
    ("mythos",     10.0, 50.0, 0.10),
    ("opus-4-1",   15.0, 75.0, 0.10),  # retired; only in old transcripts
    ("opus",        5.0, 25.0, 0.10),
    ("sonnet-5",    2.0, 10.0, 0.10),
    ("sonnet",      3.0, 15.0, 0.10),
    ("haiku-3-5",   0.8,  4.0, 0.10),  # retired
    ("haiku",       1.0,  5.0, 0.10),
)
UNKNOWN_RATES = (5.0, 25.0, 0.10)  # unknown model: assume Opus-tier

# Fast mode (research preview) bills Opus 5 / 4.8 at premium rates; the cache
# multipliers above stack on top of these. Transcripts record usage.speed.
FAST_RATES = (10.0, 50.0)
FAST_MODELS = ("opus-5", "opus-4-8")

CACHE_W5, CACHE_W1 = 1.25, 2.0
WEB_SEARCH_PER_1K = 10.0  # $10 per 1,000 searches. Web fetch is free.


def rates_for(model: str, speed: str = "standard") -> tuple[float, float, float]:
    """(input, output, cache-read multiplier) per MTok for a model id."""
    m = model.lower()
    rin, rout, cache_r = next(
        ((i, o, c) for key, i, o, c in PRICING if key in m), UNKNOWN_RATES
    )
    if speed == "fast" and any(k in m for k in FAST_MODELS):
        rin, rout = FAST_RATES
    return rin, rout, cache_r


def local_time(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()


def short_path(p: str) -> str:
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home):]
    return p


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    key         TEXT PRIMARY KEY,  -- requestId:message.id (dedupes streamed lines)
    ts          TEXT NOT NULL,     -- ISO-8601 UTC, as written in the transcript
    model       TEXT NOT NULL,
    speed       TEXT NOT NULL,     -- standard | fast
    project_dir TEXT NOT NULL,     -- folder name under ~/.claude/projects
    cwd         TEXT,
    input       INTEGER NOT NULL,
    output      INTEGER NOT NULL,
    cache_5m    INTEGER NOT NULL,
    cache_1h    INTEGER NOT NULL,
    cache_read  INTEGER NOT NULL,
    web_search  INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
"""
SCHEMA_VERSION = 1


def scan() -> dict[str, tuple]:
    """Read every transcript into {dedupe key: row} with token counts split out."""
    if not CLAUDE_PROJECTS.is_dir():
        sys.exit(f"Not found: {CLAUDE_PROJECTS} - is Claude Code installed?")

    # dedupe key -> row (same message can be written on several lines while
    # streaming; the last line wins, as before)
    rows: dict[str, tuple] = {}

    for f in CLAUDE_PROJECTS.rglob("*.jsonl"):
        # project = top-level folder (subagent transcripts nest deeper)
        proj_dir = f.relative_to(CLAUDE_PROJECTS).parts[0]
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"type":"assistant"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = e.get("message") or {}
                u = msg.get("usage")
                model = msg.get("model", "")
                ts = e.get("timestamp", "")
                if not u or not model or model == "<synthetic>" or not ts:
                    continue
                cc = u.get("cache_creation") or {}
                c5 = cc.get("ephemeral_5m_input_tokens")
                c1 = cc.get("ephemeral_1h_input_tokens", 0)
                if c5 is None:  # no breakdown: bill the lump sum as 5m writes
                    c5, c1 = u.get("cache_creation_input_tokens", 0), 0
                st = u.get("server_tool_use") or {}
                key = f'{e.get("requestId", "")}:{msg.get("id") or e.get("uuid")}'
                rows[key] = (
                    key, ts, model, u.get("speed", "standard"), proj_dir, e.get("cwd"),
                    u.get("input_tokens", 0), u.get("output_tokens", 0), c5, c1,
                    u.get("cache_read_input_tokens", 0),
                    st.get("web_search_requests", 0),  # web fetch costs nothing
                )
    return rows


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def import_transcripts(db: Path) -> None:
    """Upsert everything the transcripts still hold into the history DB.

    Messages whose transcripts Claude Code has since deleted stay in the DB -
    that's the point. Messages still on disk are replaced, so re-runs are safe.
    """
    rows = scan()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = open_db(db)
    try:
        before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r + (now,) for r in rows.values()),
            )
        after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    print(f"{db}: {after - before:,} new messages, {after:,} stored")


# Owned data: the DB holds history Claude Code has already deleted, so it's
# the only copy. The live DB stays in the data dir (Drive can upload a file
# mid-write); a snapshot goes to `data_dir` from config.toml after each import.

def owned_snapshot() -> Path | None:
    """Snapshot path from config.toml's data_dir, or None if not configured."""
    if not CONFIG_FILE.exists():
        return None
    try:
        data_dir = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("data_dir")
    except tomllib.TOMLDecodeError as e:
        sys.exit(f"{CONFIG_FILE}: {e}")
    if not data_dir:
        return None
    # never create it: a typo or a missing drive would split the history
    if not Path(data_dir).is_dir():
        sys.exit(f"{CONFIG_FILE}: data_dir {data_dir} does not exist")
    return Path(data_dir) / DB_NAME


def seed_db(live: Path, owned: Path | None) -> None:
    """Fill a missing live DB from the snapshot (new or rebuilt machine)."""
    if live.exists() or not owned or not owned.exists():
        return
    live.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(owned, live)
    print(f"Copied history {owned} -> {live}")


def snapshot(live: Path, owned: Path) -> None:
    tmp = owned.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)                    # VACUUM INTO refuses an existing file
    conn = sqlite3.connect(live)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))  # consistent copy, even mid-write
    finally:
        conn.close()
    os.replace(tmp, owned)                         # atomic: Drive never sees half a file


# Installer (single exe): copy into BIN_DIR, create the XDG folders, write a
# default config only if none exists. Never touches data or the dashboard.

CONFIG_TEMPLATE = """\
# claude-use settings. --install writes this once and never overwrites it;
# only `claude-use --install --data-dir PATH` changes data_dir later.

# Owned data: after every import a snapshot of the history DB goes to this
# folder, so it survives losing the laptop. It must exist.
{data_dir_line}
"""


def _toml_path(p: Path) -> str:
    # forward slashes: no TOML escaping needed, and Windows accepts them
    return json.dumps(p.as_posix())


def ask_data_dir() -> Path | None:
    """First-install prompt. Blank means no backup, unless a default is proposed."""
    base = os.environ.get("OWNED_DATA_HOME", "")
    proposal = Path(base) / APP if base and Path(base).is_absolute() else None
    prompt = f"Backup folder [{proposal}]: " if proposal else "Backup folder (blank for none): "
    try:
        answer = input(prompt).strip().strip('"')
    except EOFError:  # not interactive: take the default
        answer = ""
    return Path(answer).expanduser().absolute() if answer else proposal


def set_data_dir(data_dir: Path) -> None:
    """Point data_dir in an existing config at data_dir; leave the rest alone."""
    text = CONFIG_FILE.read_text(encoding="utf-8")
    line = f"data_dir = {_toml_path(data_dir)}"
    text, n = re.subn(r"(?m)^data_dir\s*=.*$", lambda _: line, text, count=1)
    if not n:
        text = text.rstrip("\n") + f"\n{line}\n"
    tomllib.loads(text)  # never write a config the app can't read
    CONFIG_FILE.write_text(text, encoding="utf-8")


# Entry points: a launcher plus, on Windows, an entry in Settings > Installed
# apps. All found by fixed name, so --uninstall never has to guess.

DISPLAY_NAME = "Claude Use"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP}"


def _start_menu_lnk() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / f"{DISPLAY_NAME}.lnk")


def _desktop_file() -> Path:
    return _xdg_base("XDG_DATA_HOME", ".local/share") / "applications" / f"{APP}.desktop"


def _running_installed() -> bool:
    return (getattr(sys, "frozen", False)
            and Path(sys.executable).resolve() == INSTALLED_EXE.resolve())


def create_entry_points() -> None:
    if sys.platform == "win32":
        import subprocess
        import winreg

        lnk = _start_menu_lnk()
        # paths go in through env vars, so no PowerShell quoting to get wrong;
        # WindowStyle 7 = minimized: the console only flashes in the taskbar
        script = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CU_LNK);"
                  " $s.TargetPath = $env:CU_EXE; $s.WindowStyle = 7;"
                  " $s.Description = 'Claude Code usage dashboard'; $s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       env={**os.environ, "CU_LNK": str(lnk), "CU_EXE": str(INSTALLED_EXE)},
                       check=True)
        print(f"Created {lnk}")

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, value in (
                ("DisplayName", DISPLAY_NAME),
                ("DisplayVersion", __version__),
                ("DisplayIcon", str(INSTALLED_EXE)),
                ("InstallLocation", str(BIN_DIR)),
                # --pause: Settings opens a console that would close before
                # the left-behind list can be read
                ("UninstallString", f'"{INSTALLED_EXE}" --uninstall --pause'),
            ):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, value)
            for name, value in (("NoModify", 1), ("NoRepair", 1),
                                ("EstimatedSize", INSTALLED_EXE.stat().st_size // 1024)):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, value)
        print(rf"Registered HKCU\{UNINSTALL_KEY} (Settings > Installed apps)")
    elif sys.platform.startswith("linux"):
        desktop = _desktop_file()
        desktop.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={DISPLAY_NAME}\n"
            "Comment=Claude Code usage dashboard\n"
            f'Exec="{INSTALLED_EXE}"\n'
            "Terminal=false\n"
            "Categories=Utility;Development;\n"
            "Actions=uninstall;\n"
            "\n"
            "[Desktop Action uninstall]\n"
            "Name=Uninstall\n"
            f'Exec="{INSTALLED_EXE}" --uninstall\n',
            encoding="utf-8")
        print(f"Created {desktop}")
    # macOS: no launcher without an .app bundle; run it from the terminal


def remove_entry_points() -> None:
    if sys.platform == "win32":
        import winreg

        lnk = _start_menu_lnk()
        if lnk.exists():
            lnk.unlink()
            print(f"Removed {lnk}")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
            print(rf"Removed HKCU\{UNINSTALL_KEY}")
        except FileNotFoundError:
            pass
    elif sys.platform.startswith("linux"):
        desktop = _desktop_file()
        if desktop.exists():
            desktop.unlink()
            print(f"Removed {desktop}")


def remove_installed_exe() -> None:
    if not INSTALLED_EXE.exists():
        return
    if sys.platform == "win32" and _running_installed():
        import subprocess

        # Windows won't delete a running exe, so a hidden PowerShell waits for
        # this process and PyInstaller's launcher (our parent) to exit, however
        # long --pause keeps them open, then deletes it. No rename first:
        # the launcher aborts if its exe moves while running. CREATE_NO_WINDOW
        # alone, not DETACHED_PROCESS: a detached parent makes Windows open a
        # visible console for every console program it starts.
        script = ("Wait-Process -Id ($env:CU_PIDS -split ',') -ErrorAction SilentlyContinue;"
                  " foreach ($i in 1..20) {"
                  " Remove-Item -LiteralPath $env:CU_EXE -Force -ErrorAction SilentlyContinue;"
                  " if (-not (Test-Path -LiteralPath $env:CU_EXE)) { break };"
                  " Start-Sleep -Milliseconds 500 }")
        pids = [os.getpid()]
        # one-file build: the parent is the launcher, unpacked to a temp dir
        if Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) != Path(sys.executable).parent:
            pids.append(os.getppid())
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         env={**os.environ, "CU_EXE": str(INSTALLED_EXE),
                              "CU_PIDS": ",".join(map(str, pids))},
                         close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        print(f"Removed {INSTALLED_EXE} (deleted once this window closes)")
    else:
        INSTALLED_EXE.unlink()
        print(f"Removed {INSTALLED_EXE}")


def warn_scheduled_tasks() -> None:
    """List scheduled jobs that mention the app. Never delete them: I made them."""
    import subprocess

    hits = []
    if sys.platform == "win32":
        import csv
        out = subprocess.run(["schtasks", "/query", "/fo", "csv", "/v"],
                             capture_output=True, text=True).stdout
        rows = list(csv.reader(out.splitlines()))
        if rows:
            header = rows[0]
            col = header.index("TaskName") if "TaskName" in header else 1
            hits = [r[col] for r in rows[1:] if r != header and len(r) > col
                    and APP in " ".join(r).lower()]
    else:
        try:
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        except OSError:  # no cron installed
            out = ""
        hits = [f"crontab: {line}" for line in out.splitlines()
                if APP in line and not line.lstrip().startswith("#")]
        for d in (Path.home() / ".config" / "systemd" / "user",
                  Path.home() / "Library" / "LaunchAgents"):
            if d.is_dir():
                hits += [str(f) for f in d.iterdir()
                         if f.is_file() and APP in f.read_text(errors="ignore")]
    if hits:
        print("\nScheduled jobs still pointing at claude-use (remove them yourself):")
        for h in dict.fromkeys(hits):
            print(f"  {h}")


def uninstall() -> None:
    remove_entry_points()
    if OUT_HTML.parent.exists():
        shutil.rmtree(OUT_HTML.parent)
        print(f"Removed {OUT_HTML.parent}")
    remove_installed_exe()
    warn_scheduled_tasks()

    left = [d for d in (CONFIG_FILE.parent, DEFAULT_DB.parent) if d.exists()]
    try:
        data_dir = tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("data_dir")
    except (OSError, tomllib.TOMLDecodeError):
        data_dir = None
    if data_dir and Path(data_dir).exists():
        left.append(Path(data_dir))
    if left:
        print("\nLeft behind (delete by hand if you're done with them):")
        labels = {CONFIG_FILE.parent: "settings", DEFAULT_DB.parent: "history DB"}
        for d in left:
            print(f"  {d}  ({labels.get(d, 'backup snapshot')})")


def install(data_dir: Path | None) -> None:
    if not getattr(sys, "frozen", False):
        sys.exit("--install runs from the release executable."
                 " From source, run usage_dashboard.py directly.")
    exe = Path(sys.executable)
    target = INSTALLED_EXE
    if exe.resolve() == target.resolve():
        print(f"Already installed: {target}")
    else:
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(exe, target)
        except PermissionError:
            sys.exit(f"Can't replace {target}: close the running claude-use"
                     " (and any scheduled run), then install again.")
        print(f"Installed {target}")

    create_entry_points()

    for d in (CONFIG_FILE.parent, DEFAULT_DB.parent, OUT_HTML.parent):
        if not d.exists():
            d.mkdir(parents=True)
            print(f"Created {d}")

    if CONFIG_FILE.exists():
        if data_dir:
            data_dir.mkdir(parents=True, exist_ok=True)
            set_data_dir(data_dir)
            print(f"Set data_dir = {data_dir} in {CONFIG_FILE}")
        else:
            print(f"Kept {CONFIG_FILE}")
    else:
        data_dir = data_dir or ask_data_dir()
        if data_dir:
            data_dir.mkdir(parents=True, exist_ok=True)
            line = f"data_dir = {_toml_path(data_dir)}"
        else:
            line = '# data_dir = "/path/to/synced/folder/claude-use"'
        CONFIG_FILE.write_text(CONFIG_TEMPLATE.format(data_dir_line=line), encoding="utf-8")
        print(f"Wrote {CONFIG_FILE}")
        if not data_dir:
            print(f"No backup: history lives only in {DEFAULT_DB}.\n"
                  f"To back it up later: claude-use --install --data-dir PATH")
    if data_dir:
        print(f"Backup snapshot: {data_dir / DB_NAME} (written after each import)")

    on_path = any(Path(p).resolve() == BIN_DIR.resolve()
                  for p in os.environ.get("PATH", "").split(os.pathsep) if p)
    if not on_path:
        print(f"Note: {BIN_DIR} is not on PATH; add it to run claude-use by name.")


def aggregate(db: Path) -> dict:
    """Aggregate every stored message per (day, model, project) and price it."""
    conn = open_db(db)
    try:
        # project folder -> most common cwd seen inside it (nicer display name);
        # ascending count, so the most common one is written last and wins
        proj_names = {
            proj_dir: short_path(cwd)
            for proj_dir, cwd, _ in conn.execute(
                "SELECT project_dir, cwd, COUNT(*) AS n FROM messages"
                " WHERE cwd IS NOT NULL GROUP BY project_dir, cwd ORDER BY n"
            )
        }
        msgs = conn.execute(
            "SELECT ts, model, speed, project_dir, input, output,"
            " cache_5m, cache_1h, cache_read, web_search FROM messages"
        ).fetchall()
    finally:
        conn.close()

    def sums() -> dict:
        return {"n": 0, "ti": 0, "to": 0, "c5": 0, "c1": 0, "cr": 0, "ws": 0}

    def add(a: dict, ti, to, c5, c1, cr, ws) -> None:
        a["n"] += 1
        a["ti"] += ti
        a["to"] += to
        a["c5"] += c5
        a["c1"] += c1
        a["cr"] += cr
        a["ws"] += ws

    def priced(model: str, speed: str, a: dict) -> dict:
        rin, rout, cache_r = rates_for(model, speed)
        cost = (
            a["ti"] * rin
            + a["to"] * rout
            + a["c5"] * rin * CACHE_W5
            + a["c1"] * rin * CACHE_W1
            + a["cr"] * rin * cache_r
        ) / 1e6 + a["ws"] * WEB_SEARCH_PER_1K / 1000
        label = f"{model} (fast)" if speed == "fast" else model
        return {"m": label, **a, "cost": round(cost, 4)}

    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    # (date, model, speed, project) -> sums; today also per hour for the Today chart
    agg: dict[tuple, dict] = defaultdict(sums)
    hourly: dict[tuple, dict] = defaultdict(sums)
    for ts, model, speed, proj, *tok in msgs:
        t = local_time(ts)
        d = t.strftime("%Y-%m-%d")
        add(agg[(d, model, speed, proj_names.get(proj, proj))], *tok)
        if d == today:
            add(hourly[(t.hour, model, speed)], *tok)

    records = [{"d": d, "p": proj, **priced(model, speed, a)}
               for (d, model, speed, proj), a in sorted(agg.items())]
    hours = [{"h": h, **priced(model, speed, a)}
             for (h, model, speed), a in sorted(hourly.items())]

    return {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "today": today,
        "subMonthly": SUBSCRIPTION_MONTHLY,
        "subName": SUBSCRIPTION_NAME,
        "records": records,
        "hourly": hours,
    }


def update_history(db: Path, owned: Path | None) -> None:
    import_transcripts(db)
    if owned:
        snapshot(db, owned)


def collect(db: Path, owned: Path | None) -> dict:
    """Copy new transcript data into the history DB, then aggregate all of it."""
    update_history(db, owned)
    return aggregate(db)


def render(data: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":"))
    return html.replace("/*__DATA__*/null", payload)


def print_summary(data: dict) -> None:
    by_model: dict[str, dict] = defaultdict(lambda: {"cost": 0.0, "n": 0, "tok": 0})
    total = 0.0
    days = set()
    for r in data["records"]:
        b = by_model[r["m"]]
        b["cost"] += r["cost"]
        b["n"] += r["n"]
        b["tok"] += r["ti"] + r["to"] + r["c5"] + r["c1"] + r["cr"]
        total += r["cost"]
        days.add(r["d"])
    span = f'{min(days)} .. {max(days)}' if days else "no data"
    print(f"\nClaude Code usage  ({span}, {len(days)} active days)")
    print(f'{"model":<32}{"msgs":>7}{"tokens":>15}{"API cost":>12}')
    for m, b in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
        print(f'{m:<32}{b["n"]:>7,}{b["tok"]:>15,}{b["cost"]:>11,.2f}$')
    print(f'{"TOTAL":<32}{"":>7}{"":>15}{total:>11,.2f}$')
    if days:
        months = len(days) / 30.44
        sub = months * data["subMonthly"]
        print(f'\n{data["subName"]} cost for the same span: ~${sub:,.2f}'
              f'  ->  API would cost {total / sub:,.1f}x more' if sub else "")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--serve", action="store_true", help="serve instead of writing a file")
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--db", type=Path,
                    help=f"use this history database instead of {DEFAULT_DB}"
                         " (no snapshot to data_dir)")
    ap.add_argument("--import-only", action="store_true",
                    help="copy new transcript data into the DB and exit (for a scheduled task)")
    ap.add_argument("--install", action="store_true",
                    help=f"copy this executable to {BIN_DIR} and set up its folders")
    ap.add_argument("--data-dir", type=Path, metavar="PATH",
                    help="with --install: back up the history DB to this folder")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the installed executable and its shortcuts;"
                         " keeps settings, history and backup")
    ap.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"claude-use {__version__}")
    args = ap.parse_args()

    if args.data_dir and not args.install:
        ap.error("--data-dir only works with --install")
    if args.install:
        install(args.data_dir.expanduser().absolute() if args.data_dir else None)
        return
    if args.uninstall:
        uninstall()
        if args.pause:  # run from Settings > Installed apps
            try:
                input("\nPress Enter to close.")
            except EOFError:
                pass
        return

    # --db is for one-off use, so only the default DB is seeded and snapshotted
    owned = None
    if args.db is None:
        args.db = DEFAULT_DB
        owned = owned_snapshot()
        seed_db(args.db, owned)

    if args.import_only:
        update_history(args.db, owned)
        return

    if args.serve:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                # only the page itself triggers an import; the browser's
                # automatic /favicon.ico request would otherwise redo it
                if self.path.split("?")[0] != "/":
                    self.send_error(404)
                    return
                body = render(collect(args.db, owned)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # quiet
                pass

        print(f"Serving on http://localhost:{args.port}  (Ctrl+C to stop)")
        if not args.no_open:
            webbrowser.open(f"http://localhost:{args.port}")
        HTTPServer(("127.0.0.1", args.port), H).serve_forever()
    else:
        data = collect(args.db, owned)
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUT_HTML.write_text(render(data), encoding="utf-8")
        print_summary(data)
        print(f"\nWrote {OUT_HTML}")
        if not args.no_open:
            webbrowser.open(OUT_HTML.as_uri())


if __name__ == "__main__":
    main()
