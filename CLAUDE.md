# CLAUDE.md — ClaudeUse

Claude Code usage dashboard. See `README.md` for what it does and how to run it;
this file is for things that aren't obvious from the code.

## Tracker

Tracked under epic **[JMR-82] Claude Use Monitor** (Jira, `JMR` project).
File new work as a child of JMR-82 rather than as a loose issue; find open work
with JQL `parent = JMR-82 AND statusCategory != Done`.

Repo: <https://github.com/goldeneye13/ClaudeUse>

## Pricing rates are load-bearing — verify, never recall

`PRICING` in `usage_dashboard.py` is the whole point of the tool, and the rates
change often enough that memory is unreliable. Before touching a rate, load the
`claude-api` skill and check against the published pricing page. Two traps that
have already bitten this file:

- **Cache reads are not a flat ×0.1.** Fable 5.1 / Mythos 5.1 read at ×0.025.
  Cache reads are ~95% of all tokens in a transcript, so a wrong multiplier here
  moves the total more than any other rate.
- **Model ids must be matched most-specific-first.** Sonnet 5 is $2/$10 while
  Sonnet 4.6 is $3/$15, so a bare `"sonnet"` substring test silently misprices.
  Same for `opus-4-1` (retired, $15/$75) ahead of `opus`.

Web fetch is free; only `web_search_requests` is billed. There is no
long-context premium tier on Claude 4.6+ — the full 1M window is standard rate.

## Conventions

- **Standard library only.** No dependencies; keep it that way.
- **Never commit `dashboard.html` or `*.sqlite`.** Generated output that embeds
  real local project paths. Both are gitignored — leave it that way.
- Any rate change should be sanity-checked with a real run
  (`python usage_dashboard.py --no-open`) before committing.
