# tennis-prices

A scheduled collector of public tennis prices, for personal research. Every 30 minutes a
GitHub Actions run saves one snapshot:

- **Kalshi:** every open market in each tennis series (match winners, set winners, game
  totals, exact set scores), best bid and ask in dollars. Public data, no key.
- **Sportsbooks, via TheRundown's free plan:** pre-match moneyline, game spread and total
  games at DraftKings, BetMGM and FanDuel, five minutes delayed. A budget keeps usage under
  the free plan's limits (20,000 data points a day, 200,000 a month), so these snapshots are
  spaced out over the day.

Snapshots are compressed CSV files under `data/<source>/YYYY/MM/DD/HHMMSS.csv.gz`, one per
run, never rewritten. `state/` holds the budget and the list of tennis series.

This repository collects prices only. It holds no model or strategy, and no betting advice.

## Setup

1. Add the repository secret `THERUNDOWN_API_KEY` (Settings → Secrets and variables →
   Actions). Without it, the collector takes Kalshi snapshots only.
2. Actions → collect prices → Run workflow, to take the first snapshot by hand.

GitHub can delay or skip scheduled runs when it's busy, and pauses schedules in a repository
with no activity for 60 days; the collector's own commits count as activity.

## Run locally

```
THERUNDOWN_API_KEY=... python collect.py
python collect.py --kalshi-only
python -m pytest -q
```

Standard library only (tests need `pytest`).
