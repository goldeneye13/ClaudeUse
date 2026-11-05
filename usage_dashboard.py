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
    claude-use --install [--all]         # (release exe) install it; --all adds the status line
    claude-use --uninstall               # remove it again; keeps history and settings
    claude-use --enable-auto-import      # import whenever a Claude Code session starts

No dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import sqlite3
import sys
import tomllib
import webbrowser
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# Bump in the release PR; the release build fails unless the tag is v<this>.
__version__ = "2.2.1"

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


LEGACY_BIN_DIR = Path.home() / ".local" / "bin"  # no XDG variable for this one
# Windows: the standard per-user install folder. 2.x used ~/.local/bin there
# too, a Unix convention, and an exe running from it is one of Defender's
# persistence signals (JMR-161). Only the exe moves; data stays XDG.
BIN_DIR = (Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
           / "Programs" / APP if os.name == "nt" else LEGACY_BIN_DIR)
EXE_NAME = "claude-use.exe" if os.name == "nt" else "claude-use"
INSTALLED_EXE = BIN_DIR / EXE_NAME
LEGACY_EXE = LEGACY_BIN_DIR / EXE_NAME  # differs from INSTALLED_EXE on Windows only
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

# Cold-cache refire: the request rewrote most of a sizeable context instead of
# reading it from cache. Real requests split cleanly: ~97% rewrite under 10%,
# a separate cluster rewrites 80%+ (JMR-147).
REFIRE_MIN_CONTEXT = 20_000
REFIRE_WRITE_SHARE = 0.8
# Claude Code writes cache with a 1h TTL (5m writes are ~0.1% of the total)
CACHE_TTL_S = 3600
# Breaks histogram (JMR-149): gaps between requests in a transcript, from 5 min
# (shorter is the agent working, or you reading its output). Bucket i holds
# (edge i, edge i+1]; CACHE_TTL_S must be an edge so the expiry line falls between bars.
GAP_EDGES_S = (300, 900, 1800, 2700, 3600, 7200, 14400, 28800, 86400)
GAP_LABELS = ("5-15m", "15-30m", "30-45m", "45-60m", "1-2h", "2-4h", "4-8h", "8-24h", "1d+")
# close call: back within the last 15 minutes of the cache's life, still warm
CLOSE_CALL_S = 2700
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
    imported_at TEXT NOT NULL,
    -- v2: context for cold-cache refire causes, captured while the transcript
    -- still exists (NULL on rows imported before v2 whose transcript is gone)
    session_id    TEXT,
    sidechain     INTEGER,  -- 1 = subagent transcript
    gap_s         REAL,     -- seconds since the previous request in the same transcript
    after_compact INTEGER,  -- 1 = a compaction happened since that previous request
    model_changed INTEGER   -- 1 = that previous request used another model
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
"""
SCHEMA_VERSION = 2
# added in v2; ALTERed onto v1 databases in place
V2_COLUMNS = (("session_id", "TEXT"), ("sidechain", "INTEGER"), ("gap_s", "REAL"),
              ("after_compact", "INTEGER"), ("model_changed", "INTEGER"))
COLUMNS = ("key", "ts", "model", "speed", "project_dir", "cwd", "input", "output",
           "cache_5m", "cache_1h", "cache_read", "web_search", "imported_at",
           *(c for c, _ in V2_COLUMNS))


def scan() -> dict[str, tuple]:
    """Read every transcript into {dedupe key: row} with token counts split out."""
    if not CLAUDE_PROJECTS.is_dir():
        sys.exit(f"Not found: {CLAUDE_PROJECTS} - is Claude Code installed?")

    # dedupe key -> row (same message can be written on several lines while
    # streaming; the last line wins, as before)
    rows: dict[str, tuple] = {}
    searches: dict[str, set] = defaultdict(set)  # dedupe key -> WebSearch tool_use ids

    for f in CLAUDE_PROJECTS.rglob("*.jsonl"):
        # project = top-level folder (subagent transcripts nest deeper)
        parts = f.relative_to(CLAUDE_PROJECTS).parts
        proj_dir = parts[0]
        file_rows: dict[str, tuple] = {}
        compacts: list[str] = []
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"compact_boundary"' in line:
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("subtype") == "compact_boundary" and e.get("timestamp"):
                        compacts.append(e["timestamp"])
                    continue
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
                searches[key].update(
                    b.get("id") for b in msg.get("content") or ()
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "WebSearch")
                file_rows[key] = (
                    key, ts, model, u.get("speed", "standard"), proj_dir, e.get("cwd"),
                    u.get("input_tokens", 0), u.get("output_tokens", 0), c5, c1,
                    u.get("cache_read_input_tokens", 0),
                    st.get("web_search_requests", 0),  # web fetch costs nothing
                    e.get("sessionId"),
                    int(bool(e.get("isSidechain")) or "subagents" in parts),
                )

        # refire context: each request against the previous one in this transcript
        prev = None
        for r in sorted(file_rows.values(), key=lambda r: r[1]):
            if prev is None:
                ctx = (None, 0, 0)
            else:
                gap = (datetime.fromisoformat(r[1].replace("Z", "+00:00"))
                       - datetime.fromisoformat(prev[1].replace("Z", "+00:00"))).total_seconds()
                ctx = (gap, int(any(prev[1] < c <= r[1] for c in compacts)),
                       int(r[2] != prev[2]))
            rows[r[0]] = r + ctx
            prev = r

    # Claude Code runs WebSearch as a separate request that never reaches the
    # transcript, so web_search_requests is always 0: count the tool calls
    # instead. Ids from every line, since a response is written one content
    # block per line and only the last line is kept above.
    for key, ids in searches.items():
        r = rows[key]
        rows[key] = r[:11] + (max(r[11], len(ids)),) + r[12:]
    return rows


# A DB viewer with unsaved changes (DB Browser for SQLite) holds a write lock;
# wait this long for it before giving up. The import is one transaction, so
# giving up leaves the DB untouched.
DB_TIMEOUT_S = 15


class DbLocked(Exception):
    pass


@contextmanager
def friendly_lock_errors(db: Path):
    """Turn SQLite's "database is locked" into a DbLocked with a one-line message."""
    try:
        yield
    except sqlite3.OperationalError as e:
        if "locked" not in str(e):
            raise
        raise DbLocked(f"{db} is locked by another program (DB Browser with unsaved"
                       " changes?). Write or revert the changes there and rerun.") from None


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=DB_TIMEOUT_S)
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    with conn:
        for col, typ in V2_COLUMNS:
            if col not in have:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
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
                f"INSERT OR REPLACE INTO messages ({','.join(COLUMNS)})"
                f" VALUES ({','.join('?' * len(COLUMNS))})",
                (r[:12] + (now,) + r[12:] for r in rows.values()),
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
    conn = sqlite3.connect(live, timeout=DB_TIMEOUT_S)
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
        if _user_path(add=True):
            print(f"Added {BIN_DIR} to your user PATH (new terminals pick it up)")
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
        if _user_path(add=False):
            print(f"Removed {BIN_DIR} from your user PATH")
    elif sys.platform.startswith("linux"):
        desktop = _desktop_file()
        if desktop.exists():
            desktop.unlink()
            print(f"Removed {desktop}")


def _user_path(add: bool) -> bool:
    """Windows: add BIN_DIR to, or remove it from, the user PATH (HKCU\\Environment).
    True if PATH changed. Other entries, their order and the value's type are kept."""
    import ctypes
    import winreg

    def same(entry: str) -> bool:
        norm = lambda p: os.path.normcase(os.path.normpath(os.path.expandvars(p)))  # noqa: E731
        return norm(entry) == norm(str(BIN_DIR))

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            value, kind = winreg.QueryValueEx(k, "Path")
        except FileNotFoundError:
            value, kind = "", winreg.REG_EXPAND_SZ
        # empty parts kept, so a trailing ";" survives a round trip
        entries = value.split(";") if value else []
        kept = [e for e in entries if not (e and same(e))]
        if add:
            if len(kept) != len(entries):
                return False  # already there
            kept.insert(len(kept) - 1 if kept and not kept[-1] else len(kept), str(BIN_DIR))
        elif len(kept) == len(entries):
            return False  # wasn't there
        winreg.SetValueEx(k, "Path", 0, kind, ";".join(kept))
    # tell Explorer, so terminals it starts from now on see the new PATH
    HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
    ctypes.windll.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
                                             SMTO_ABORTIFHUNG, 5000, None)
    return True


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
                  " Start-Sleep -Milliseconds 500 };"
                  " if (-not (Get-ChildItem -LiteralPath $env:CU_DIR -Force -ErrorAction SilentlyContinue))"
                  " { Remove-Item -LiteralPath $env:CU_DIR -ErrorAction SilentlyContinue }")
        pids = [os.getpid()]
        # one-file build: the parent is the launcher, unpacked to a temp dir
        if Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) != Path(sys.executable).parent:
            pids.append(os.getppid())
        subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         env={**os.environ, "CU_EXE": str(INSTALLED_EXE), "CU_DIR": str(BIN_DIR),
                              "CU_PIDS": ",".join(map(str, pids))},
                         close_fds=True, creationflags=subprocess.CREATE_NO_WINDOW)
        print(f"Removed {INSTALLED_EXE} (deleted once this window closes)")
    else:
        INSTALLED_EXE.unlink()
        print(f"Removed {INSTALLED_EXE}")
        if sys.platform == "win32":  # the folder is ours there, not a shared bin
            try:
                BIN_DIR.rmdir()
            except OSError:  # not empty
                pass


# Leftovers from 2.x, which scheduled the import with the OS scheduler and
# set a one-off alarm task after every Claude response. Windows Defender
# flagged that as persistence (JMR-161), so 3.x schedules nothing: --install
# and --uninstall only remove what 2.x left, found by fixed name.

LEGACY_JOB = f"{APP} import"                    # Windows task name, crontab marker
LEGACY_UNIT = f"{APP}-import"                   # systemd units, launchd label
LEGACY_CLOCK_TASKS = rf"\{APP}\clock" "\\"      # Windows task folder
LEGACY_CLOCK_UNIT = f"{APP}-clock"              # transient systemd timers
LEGACY_CLOCK_PIDS = _xdg("XDG_STATE_HOME", ".local/state") / "clock"  # sleeping alarms
# the cache clock's entries in ~/.claude/settings.json: status line and hooks
LEGACY_SETTINGS_FLAGS = ("--statusline", "--arm-clock", "--cancel-clock")


def _windows_tasks() -> list[str]:
    """Scheduled tasks whose definition mentions the app."""
    import csv
    import subprocess

    out = subprocess.run(["schtasks", "/query", "/fo", "csv", "/v"],
                         capture_output=True, text=True).stdout
    rows = list(csv.reader(out.splitlines()))
    if not rows:
        return []
    header = rows[0]
    col = header.index("TaskName") if "TaskName" in header else 1
    return list(dict.fromkeys(r[col] for r in rows[1:] if r != header and len(r) > col
                              and APP in " ".join(r).lower()))


def _systemd_dir() -> Path:
    """Where the user manager looks for units: its own XDG_CONFIG_HOME, which
    needn't match this process's (set in a shell profile, say), else ~/.config."""
    import subprocess

    base = Path.home() / ".config"
    if shutil.which("systemctl"):
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("XDG_CONFIG_HOME=") and Path(line[16:]).is_absolute():
                base = Path(line[16:])
    return base / "systemd" / "user"


def remove_legacy_jobs() -> None:
    """Remove the 2.x scheduled import and any pending cache clock alarms."""
    import subprocess

    if sys.platform == "win32":
        for name in _windows_tasks():
            if name.lstrip("\\") == LEGACY_JOB or name.startswith(LEGACY_CLOCK_TASKS):
                if subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                                  capture_output=True).returncode == 0:
                    print(f'Removed scheduled task "{name}" (from 2.x)')
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_UNIT}.plist"
        if plist.exists():
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_UNIT}"],
                           capture_output=True)
            plist.unlink()
            print(f"Removed {plist} (from 2.x)")
    else:
        if shutil.which("systemctl"):
            timer = _systemd_dir() / f"{LEGACY_UNIT}.timer"
            if timer.exists():
                subprocess.run(["systemctl", "--user", "disable", "--now", timer.name],
                               capture_output=True)
                for f in (timer, timer.with_suffix(".service")):
                    f.unlink(missing_ok=True)
                    print(f"Removed {f} (from 2.x)")
                subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            subprocess.run(["systemctl", "--user", "stop", f"{LEGACY_CLOCK_UNIT}-*.timer"],
                           capture_output=True)
        if shutil.which("crontab"):
            out = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
            keep = [line for line in out.splitlines() if not line.endswith(f"# {LEGACY_JOB}")]
            if len(keep) != len(out.splitlines()):
                subprocess.run(["crontab", "-"], input="".join(f"{line}\n" for line in keep),
                               text=True, check=True)
                print("Removed the claude-use import line from crontab (from 2.x)")
    if LEGACY_CLOCK_PIDS.is_dir():
        import signal

        for f in LEGACY_CLOCK_PIDS.glob("*.pid"):
            try:
                os.kill(int(f.read_text()), signal.SIGTERM)
            except (OSError, ValueError):  # already gone
                pass
            f.unlink(missing_ok=True)
        try:
            LEGACY_CLOCK_PIDS.rmdir()
        except OSError:
            pass


def remove_legacy_exe() -> None:
    """Windows: 2.x installed to ~/.local/bin, 3.x to the standard per-user folder."""
    if sys.platform != "win32" or not LEGACY_EXE.exists():
        return
    if Path(sys.executable).resolve() == LEGACY_EXE.resolve():
        print(f"Left {LEGACY_EXE}: it's the copy running now. Delete it once this closes.")
        return
    try:
        LEGACY_EXE.unlink()
        print(f"Removed {LEGACY_EXE} (2.x location)")
    except PermissionError:
        print(f"Couldn't remove {LEGACY_EXE}: it's running. Delete it by hand.")


def warn_scheduled_tasks() -> None:
    """List scheduled jobs that mention the app. Run after remove_legacy_jobs(),
    so anything left was made by hand: never delete it."""
    import subprocess

    hits = []
    if sys.platform == "win32":
        hits = _windows_tasks()
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


# Auto-import (JMR-161): one async SessionStart hook in ~/.claude/settings.json
# runs the import whenever a Claude Code session starts, resumes or clears.
# Claude Code doesn't wait for it, and an import takes seconds, so there's no
# throttle. Our entries are found by the flag they run, so nothing else in
# settings.json (your own status line and hooks) is ever touched.

CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_EVENT = "SessionStart"
HOOK_FLAG = "--auto-import"


def _hook_cmd() -> str:
    # Claude Code runs it in Git Bash on Windows: forward slashes, quoted
    return f'"{INSTALLED_EXE.as_posix()}" {HOOK_FLAG}'


def _is_ours(cmd: str, flags: tuple[str, ...]) -> bool:
    return APP in cmd and any(cmd.endswith(f) for f in flags)


def _load_claude_settings() -> dict:
    if not CLAUDE_SETTINGS.exists():
        return {}
    try:
        return json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"{CLAUDE_SETTINGS}: {e}; fix it first, nothing changed")


def _save_claude_settings(s: dict) -> None:
    CLAUDE_SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    tmp = CLAUDE_SETTINGS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, CLAUDE_SETTINGS)  # Claude Code watches this file: never half-written


def _our_hooks(s: dict, flags: tuple[str, ...]) -> list[str]:
    return [h.get("command", "") for groups in (s.get("hooks") or {}).values()
            if isinstance(groups, list) for g in groups for h in g.get("hooks", [])
            if _is_ours(h.get("command", ""), flags)]


def _strip(s: dict, flags: tuple[str, ...]) -> bool:
    """Drop our hooks and status line that run any of flags. True if there were any."""
    found = False
    hooks = s.get("hooks") or {}
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups
                if not any(_is_ours(h.get("command", ""), flags) for h in g.get("hooks", []))]
        if len(kept) != len(groups):
            found = True
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    if found and not hooks:
        del s["hooks"]
    if _is_ours((s.get("statusLine") or {}).get("command", ""), flags):
        del s["statusLine"]
        found = True
    return found


def _add_hook(s: dict) -> None:
    s.setdefault("hooks", {}).setdefault(HOOK_EVENT, []).append(
        {"hooks": [{"type": "command", "command": _hook_cmd(), "async": True, "timeout": 120}]})


def migrate_claude_settings() -> None:
    """On install: drop the 2.x cache clock, and point our hook at this install."""
    if not CLAUDE_SETTINGS.exists():
        return
    s = _load_claude_settings()
    changed = _strip(s, LEGACY_SETTINGS_FLAGS)
    if changed:
        print(f"Removed the 2.x cache clock from {CLAUDE_SETTINGS}")
    if _our_hooks(s, (HOOK_FLAG,)) not in ([], [_hook_cmd()]):
        _strip(s, (HOOK_FLAG,))
        _add_hook(s)
        changed = True
        print(f"Pointed the auto-import hook in {CLAUDE_SETTINGS} at {INSTALLED_EXE}")
    if changed:
        _save_claude_settings(s)


def enable_auto_import() -> None:
    if not INSTALLED_EXE.exists():
        sys.exit(f"{INSTALLED_EXE} not found: run claude-use --install first.")
    s = _load_claude_settings()
    _strip(s, LEGACY_SETTINGS_FLAGS + (HOOK_FLAG,))
    _add_hook(s)
    _save_claude_settings(s)
    print(f"Auto-import on in {CLAUDE_SETTINGS}:\n"
          f"  a {HOOK_EVENT} hook updates the history in the background"
          " whenever a Claude Code session starts.")


def disable_auto_import() -> bool:
    """Remove our hook, and any 2.x cache clock. True if there was either."""
    s = _load_claude_settings()
    if not _strip(s, LEGACY_SETTINGS_FLAGS + (HOOK_FLAG,)):
        return False
    _save_claude_settings(s)
    print(f"Removed the claude-use auto-import from {CLAUDE_SETTINGS}")
    return True


def auto_import() -> None:
    """The hook itself: import, silently. Claude Code can add a SessionStart
    hook's output to the session's context, and an error is nothing it can act on."""
    import contextlib
    import io

    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            owned = owned_snapshot()
            seed_db(DEFAULT_DB, owned)
            update_history(DEFAULT_DB, owned)
    except (Exception, SystemExit):  # DB locked by a viewer, bad config: next session retries
        pass


def uninstall() -> None:
    remove_entry_points()
    if OUT_HTML.parent.exists():
        shutil.rmtree(OUT_HTML.parent)
        print(f"Removed {OUT_HTML.parent}")
    remove_installed_exe()
    remove_legacy_exe()
    remove_legacy_jobs()
    disable_auto_import()
    if LEGACY_CLOCK_PIDS.parent.exists():  # only ever held the 2.x clock's state
        shutil.rmtree(LEGACY_CLOCK_PIDS.parent)
        print(f"Removed {LEGACY_CLOCK_PIDS.parent}")
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
                     " (and Claude Code, whose hook runs it), then install again.")
        print(f"Installed {target}")

    remove_legacy_exe()
    create_entry_points()
    remove_legacy_jobs()
    migrate_claude_settings()

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
    if not on_path and sys.platform != "win32":  # Windows: create_entry_points adds it
        print(f"Note: {BIN_DIR} is not on PATH; add it to run claude-use by name.")


def refire_cause(ti, c5, c1, cr, session_id, sidechain, gap, after_compact,
                 model_changed) -> str | None:
    """Why a request rebuilt its cache, or None if it didn't."""
    ctx = ti + c5 + c1 + cr
    if ctx < REFIRE_MIN_CONTEXT or (c5 + c1) < REFIRE_WRITE_SHARE * ctx:
        return None
    if session_id is None:  # imported before v2, transcript since deleted
        return "unknown"
    if gap is None:  # first request in its transcript
        return "subagent start" if sidechain else "session start"
    if after_compact:
        return "compaction"
    if model_changed:
        return "model switch"
    if gap > CACHE_TTL_S:
        return "idle > 1h"
    if gap > 300:
        return "idle 5m-1h"
    return "other"


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
            " cache_5m, cache_1h, cache_read, web_search,"
            " session_id, sidechain, gap_s, after_compact, model_changed FROM messages"
        ).fetchall()
    finally:
        conn.close()

    def sums() -> dict:
        # rw: cache writes that rebuilt a cold cache (part of c5 + c1)
        return {"n": 0, "ti": 0, "to": 0, "c5": 0, "c1": 0, "cr": 0, "ws": 0, "rw": 0}

    def add(a: dict, ti, to, c5, c1, cr, ws, rw) -> None:
        a["n"] += 1
        a["ti"] += ti
        a["to"] += to
        a["c5"] += c5
        a["c1"] += c1
        a["cr"] += cr
        a["ws"] += ws
        a["rw"] += rw

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
    refires = []
    gaps: dict[tuple, int] = defaultdict(int)  # (date, bucket, rebuilt) -> requests
    close_calls = []
    for ts, model, speed, proj, *tok, sid, side, gap, compact, switched in msgs:
        t = local_time(ts)
        d = t.strftime("%Y-%m-%d")
        ti, to, c5, c1, cr, ws = tok
        cause = refire_cause(ti, c5, c1, cr, sid, side, gap, compact, switched)
        rw = c5 + c1 if cause else 0
        add(agg[(d, model, speed, proj_names.get(proj, proj))], *tok, rw)
        if d == today:
            add(hourly[(t.hour, model, speed)], *tok, rw)
        if cause:
            rin, _, cache_r = rates_for(model, speed)
            extra = (c5 * (CACHE_W5 - cache_r) + c1 * (CACHE_W1 - cache_r)) * rin / 1e6
            refires.append({"d": d, "h": t.hour, "m": f"{model} (fast)" if speed == "fast" else model,
                            "p": proj_names.get(proj, proj), "cause": cause,
                            "tok": rw, "extra": round(extra, 4),
                            "gap": None if gap is None else round(gap)})
        if gap is not None and gap >= GAP_EDGES_S[0]:
            b = max(0, bisect.bisect_left(GAP_EDGES_S, gap) - 1)
            gaps[(d, b, int(bool(cause)))] += 1
            if not cause and CLOSE_CALL_S <= gap <= CACHE_TTL_S:
                # what rebuilding that context would have cost: 1h writes, not reads
                rin, _, cache_r = rates_for(model, speed)
                close_calls.append({"d": d, "h": t.hour, "p": proj_names.get(proj, proj),
                                    "gap": round(gap), "tok": cr,
                                    "saved": round(cr * rin * (CACHE_W1 - cache_r) / 1e6, 4)})

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
        "refires": sorted(refires, key=lambda r: (r["d"], r["h"])),
        "cacheTtlMin": CACHE_TTL_S // 60,
        "gapLabels": GAP_LABELS,
        "gapTtlBucket": GAP_EDGES_S.index(CACHE_TTL_S),  # first bucket past expiry
        "gaps": [{"d": d, "b": b, "rebuilt": rebuilt, "n": n}
                 for (d, b, rebuilt), n in sorted(gaps.items())],
        "closeCalls": sorted(close_calls, key=lambda e: (e["d"], e["h"])),
        "closeCallMin": CLOSE_CALL_S // 60,
    }


def update_history(db: Path, owned: Path | None) -> None:
    with friendly_lock_errors(db):
        import_transcripts(db)
        if owned:
            snapshot(db, owned)


def collect(db: Path, owned: Path | None) -> dict:
    """Copy new transcript data into the history DB, then aggregate all of it."""
    update_history(db, owned)
    with friendly_lock_errors(db):
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


def install_auto_import() -> None:
    """--install --all: the auto-import too. If it can't be added, the install
    still stands, and the exit code says something was skipped."""
    print()
    try:
        enable_auto_import()
    except (SystemExit, OSError) as e:
        if isinstance(e, SystemExit) and not e.code:
            return
        print(f"Skipped: {e.code if isinstance(e, SystemExit) else e}")
        print("\nInstalled, but without the auto-import. Once fixed, run:\n"
              "  claude-use --enable-auto-import")
        sys.exit(1)


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
                    help="copy new transcript data into the DB and exit")
    ap.add_argument("--install", action="store_true",
                    help=f"copy this executable to {BIN_DIR} and set up its folders")
    ap.add_argument("--data-dir", type=Path, metavar="PATH",
                    help="with --install: back up the history DB to this folder")
    ap.add_argument("--all", action="store_true",
                    help="with --install: also --enable-auto-import")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove the installed executable, its shortcuts and its"
                         " auto-import; keeps settings, history and backup")
    ap.add_argument("--enable-auto-import", action="store_true",
                    help="add a SessionStart hook to ~/.claude/settings.json that"
                         " imports in the background whenever a Claude Code session starts")
    ap.add_argument("--disable-auto-import", action="store_true",
                    help="remove that hook")
    # run by Claude Code, not by hand
    ap.add_argument(HOOK_FLAG, action="store_true", help=argparse.SUPPRESS)
    # 2.x status line, hooks and alarms that --install hasn't removed yet: ignored
    ap.add_argument("--statusline", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--arm-clock", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--cancel-clock", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--clock-fire", nargs=2, help=argparse.SUPPRESS)
    ap.add_argument("--clock-delay", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--pause", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"claude-use {__version__}")
    args = ap.parse_args()

    # the hook first: it runs on every session start, so skip everything else
    if args.auto_import:
        auto_import()
        return
    if args.statusline or args.arm_clock or args.cancel_clock or args.clock_fire:
        return
    if args.disable_auto_import:
        if not disable_auto_import():
            print("The claude-use auto-import isn't on.")
        return

    for flag in ("data_dir", "all"):
        if getattr(args, flag) and not args.install:
            ap.error(f"--{flag.replace('_', '-')} only works with --install")
    if args.install:
        install(args.data_dir.expanduser().absolute() if args.data_dir else None)
        if args.enable_auto_import or args.all:
            install_auto_import()
        return
    if args.enable_auto_import:
        enable_auto_import()
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
        try:
            update_history(args.db, owned)
        except DbLocked as e:
            sys.exit(str(e))  # non-zero exit: Task Scheduler shows it as Last Run Result
        return

    if args.serve:
        import html
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                # only the page itself triggers an import; the browser's
                # automatic /favicon.ico request would otherwise redo it
                if self.path.split("?")[0] != "/":
                    self.send_error(404)
                    return
                try:
                    body, status = render(collect(args.db, owned)).encode("utf-8"), 200
                except DbLocked as e:
                    body, status = html.escape(str(e)).encode("utf-8"), 503
                self.send_response(status)
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
        try:
            data = collect(args.db, owned)
        except DbLocked as e:
            sys.exit(str(e))
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUT_HTML.write_text(render(data), encoding="utf-8")
        print_summary(data)
        print(f"\nWrote {OUT_HTML}")
        if not args.no_open:
            webbrowser.open(OUT_HTML.as_uri())


if __name__ == "__main__":
    main()
