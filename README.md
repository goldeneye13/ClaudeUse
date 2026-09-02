# Claude Code usage dashboard

Parses the local Claude Code transcripts (`~/.claude/projects/**/*.jsonl` — kept
for roughly the last 30 days), aggregates token usage per **model**, **project**
and **day**, and prices it at **API rates** so you can see what your Pro
subscription usage would have cost on the API plan.

No dependencies — Python 3.9+ standard library only.

## Usage

```powershell
python usage_dashboard.py            # print summary, write dashboard.html, open browser
python usage_dashboard.py --no-open  # just generate dashboard.html
python usage_dashboard.py --serve    # live server on http://localhost:8377 (re-parses on refresh)
```

`dashboard.html` is fully self-contained (data embedded, no CDN, works offline)
and supports light/dark mode. It shows:

- stat tiles: estimated API cost, prorated Pro cost, API÷Pro ratio, tokens, messages
- daily API-equivalent cost stacked by model
- cost by model and by project (bars + detail tables)
- date-range filter (7 / 30 / 90 days / all) that re-scopes everything

## How costs are computed

Each assistant message in the transcripts carries a `usage` block. Duplicated
streaming lines are deduped on `(requestId, message.id)`; subagent transcripts
(`<session>/subagents/*.jsonl`) are included. Per model, per MTok:

| Model              | Input | Output | Cache write 5m (×1.25) | Cache write 1h (×2) | Cache read |
|--------------------|------:|-------:|-----------------------:|--------------------:|-----------:|
| Fable / Mythos 5.1 | $10   | $50    | $12.50                 | $20                 | $0.25¹     |
| Fable / Mythos 5   | $10   | $50    | $12.50                 | $20                 | $1.00      |
| Opus 5 / 4.8 / 4.7 | $5    | $25    | $6.25                  | $10                 | $0.50      |
| Sonnet 5           | $2    | $10    | $2.50                  | $4                  | $0.20      |
| Sonnet 4.6         | $3    | $15    | $3.75                  | $6                  | $0.30      |
| Haiku 4.5          | $1    | $5     | $1.25                  | $2                  | $0.10      |

¹ Cache reads are ×0.1 of the input rate on every model **except** Fable 5.1 and
Mythos 5.1, which read at ×0.025. This matters: cache reads are ~95% of all
tokens Claude Code sends, so applying the wrong multiplier skews the total more
than any other rate here.

Rates are version-aware — the model id is matched most-specific-first, so
`claude-sonnet-5` prices at $2/$10 while `claude-sonnet-4-6` prices at $3/$15.
Retired models (Opus 4.1, Haiku 3.5) keep their own rates for older transcripts;
anything unrecognized falls back to Opus-tier.

**Fast mode** (`usage.speed == "fast"`, research preview on Opus 5 / 4.8) bills
at $10/$50 with the cache multipliers stacked on top; those rows are labelled
`<model> (fast)` in the output.

**Web search** costs $10 per 1,000 searches. **Web fetch is free** — you only pay
for the fetched content as input tokens, so `web_fetch_requests` is not billed.

The full 1M-token context window is charged at standard rates on Claude 4.6 and
later, so there is no long-context premium tier to account for. Estimates use
standard (non-batch) rates and assume global routing (no `inference_geo: "us"`
1.1× data-residency multiplier).

The subscription comparison assumes **Pro at $20/month**, prorated over the
selected range — edit `SUBSCRIPTION_MONTHLY` / `SUBSCRIPTION_NAME` at the top of
`usage_dashboard.py` if you're on Max ($100/$200).

## Files

- `usage_dashboard.py` — parser, pricing, CLI, tiny HTTP server
- `template.html` — dashboard template (data injected at `/*__DATA__*/`)
- `dashboard.html` — generated output (safe to delete/regenerate)
