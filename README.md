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

| Model  | Input | Output | Cache write 5m (×1.25) | Cache write 1h (×2) | Cache read (×0.1) |
|--------|------:|-------:|-----------------------:|--------------------:|------------------:|
| Fable  | $10   | $50    | $12.50                 | $20                 | $1.00             |
| Opus   | $5    | $25    | $6.25                  | $10                 | $0.50             |
| Sonnet | $3    | $15    | $3.75                  | $6                  | $0.30             |
| Haiku  | $1    | $5     | $1.25                  | $2                  | $0.10             |

Web search/fetch requests are billed at $10 per 1,000. Estimates use standard
(non-batch) rates; unknown models fall back to Opus pricing.

The subscription comparison assumes **Pro at $20/month**, prorated over the
selected range — edit `SUBSCRIPTION_MONTHLY` / `SUBSCRIPTION_NAME` at the top of
`usage_dashboard.py` if you're on Max ($100/$200).

## Files

- `usage_dashboard.py` — parser, pricing, CLI, tiny HTTP server
- `template.html` — dashboard template (data injected at `/*__DATA__*/`)
- `dashboard.html` — generated output (safe to delete/regenerate)
