#!/usr/bin/env python3
"""Claude Code usage dashboard.

Parses the local Claude Code transcripts (~/.claude/projects/**/*.jsonl),
aggregates token usage per model / project / day, prices it at API rates
(including prompt-cache write/read pricing), and renders a self-contained
HTML dashboard.

Usage:
    python usage_dashboard.py            # generate dashboard.html and open it
    python usage_dashboard.py --no-open  # just generate
    python usage_dashboard.py --serve    # serve on http://localhost:8377 (regenerates per request)

No dependencies beyond the standard library.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OUT_HTML = Path(__file__).parent / "dashboard.html"
TEMPLATE = Path(__file__).parent / "template.html"

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


def local_date(ts: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%Y-%m-%d")


def short_path(p: str) -> str:
    home = str(Path.home())
    if p.startswith(home):
        p = "~" + p[len(home):]
    return p


def parse() -> dict:
    """Scan every transcript, dedupe streamed duplicates, aggregate per (day, model, project)."""
    if not CLAUDE_PROJECTS.is_dir():
        sys.exit(f"Not found: {CLAUDE_PROJECTS} - is Claude Code installed?")

    # project folder -> most common cwd seen inside it (nicer display name)
    cwd_votes: dict[str, Counter] = defaultdict(Counter)
    # dedupe key -> parsed entry (same message can be written on several lines while streaming)
    entries: dict[str, dict] = {}

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
                usage = msg.get("usage")
                model = msg.get("model", "")
                if not usage or not model or model == "<synthetic>":
                    continue
                if e.get("cwd"):
                    cwd_votes[proj_dir][e["cwd"]] += 1
                key = f'{e.get("requestId", "")}:{msg.get("id") or e.get("uuid")}'
                entries[key] = {
                    "ts": e.get("timestamp", ""),
                    "model": model,
                    "proj": proj_dir,
                    "usage": usage,
                }

    proj_names = {
        d: short_path(votes.most_common(1)[0][0]) if votes else d
        for d, votes in cwd_votes.items()
    }

    # aggregate: (date, model, project) -> sums
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"n": 0, "ti": 0, "to": 0, "c5": 0, "c1": 0, "cr": 0, "ws": 0}
    )
    for ent in entries.values():
        if not ent["ts"]:
            continue
        u = ent["usage"]
        cc = u.get("cache_creation") or {}
        c5 = cc.get("ephemeral_5m_input_tokens")
        c1 = cc.get("ephemeral_1h_input_tokens", 0)
        if c5 is None:  # no breakdown: bill the lump sum as 5m writes
            c5, c1 = u.get("cache_creation_input_tokens", 0), 0
        st = u.get("server_tool_use") or {}
        speed = u.get("speed", "standard")
        a = agg[(local_date(ent["ts"]), ent["model"], speed,
                 proj_names.get(ent["proj"], ent["proj"]))]
        a["n"] += 1
        a["ti"] += u.get("input_tokens", 0)
        a["to"] += u.get("output_tokens", 0)
        a["c5"] += c5
        a["c1"] += c1
        a["cr"] += u.get("cache_read_input_tokens", 0)
        a["ws"] += st.get("web_search_requests", 0)  # web fetch costs nothing

    records = []
    for (d, model, speed, proj), a in sorted(agg.items()):
        rin, rout, cache_r = rates_for(model, speed)
        cost = (
            a["ti"] * rin
            + a["to"] * rout
            + a["c5"] * rin * CACHE_W5
            + a["c1"] * rin * CACHE_W1
            + a["cr"] * rin * cache_r
        ) / 1e6 + a["ws"] * WEB_SEARCH_PER_1K / 1000
        label = f"{model} (fast)" if speed == "fast" else model
        records.append({"d": d, "m": label, "p": proj, **a, "cost": round(cost, 4)})

    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "subMonthly": SUBSCRIPTION_MONTHLY,
        "subName": SUBSCRIPTION_NAME,
        "records": records,
    }


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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--serve", action="store_true", help="serve instead of writing a file")
    ap.add_argument("--port", type=int, default=8377)
    args = ap.parse_args()

    if args.serve:
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = render(parse()).encode("utf-8")
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
        data = parse()
        OUT_HTML.write_text(render(data), encoding="utf-8")
        print_summary(data)
        print(f"\nWrote {OUT_HTML}")
        if not args.no_open:
            webbrowser.open(OUT_HTML.as_uri())


if __name__ == "__main__":
    main()
