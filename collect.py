"""Collect tennis prices, a snapshot at a time, into small compressed CSV files.

    python collect.py                 one snapshot (Kalshi; sportsbooks when the budget allows)
    python collect.py --kalshi-only

Two free sources:

Kalshi (no key): every open market in each tennis series (match winners, set winners, game
totals, exact set scores and the rest), with the best bid and ask in dollars. Series are found
from Kalshi's own catalog once a day; a fixed list covers the main ones if that call fails.

TheRundown (free key in THERUNDOWN_API_KEY): pre-match moneyline, game spread and total games
for ATP (sport 38) and WTA (sport 39) at DraftKings (19), BetMGM (22) and FanDuel (23), five
minutes delayed. The free plan allows 20,000 data points a day and 200,000 a month, about 20
points per match per snapshot, so a budget decides when to take one: today's share of what's
left this month, spaced out over the day. Each response's X-Datapoints header is the bill.

Files: data/<source>/YYYY/MM/DD/HHMMSS.csv.gz, one per snapshot, never rewritten.
State (budget, series list): state/*.json. Standard library only.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENT = "tennis-prices/0.1 (personal research; github.com, scheduled every 30 minutes)"

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_FALLBACK = [  # the main tennis series, used when the catalog call fails
    "KXATPMATCH", "KXWTAMATCH", "KXATPSETWINNER", "KXWTASETWINNER", "KXATPGAMETOTAL",
    "KXATPEXACTMATCH", "KXATPTOTALSETS", "KXATPTIEBREAK", "KXATPCHALLENGERMATCH",
    "KXWTACHALLENGERMATCH",
]  # fmt: skip
KALSHI_PREFIXES = ("KXATP", "KXWTA", "KXITF")
# Match-level series only. November 12, 2026: a "tennis" tag also matched table tennis
# (KXLIGAPROMATCH), and the catalog includes tournament futures (KXATP, KXWTA, KXATPWTA) and
# doubles, about 640 of 1,081 markets in the first snapshot, none of use to the model.
KALSHI_KINDS = ("MATCH", "SET", "GAME", "TOTAL", "SPREAD", "TIEBREAK", "EXACT", "SCORE")
SERIES_RULE = 2  # bump when the rule above changes, so a list saved under the old rule is redone


def tennis_series(ticker: str) -> bool:
    t = str(ticker).upper()
    return (t.startswith(KALSHI_PREFIXES) and "DOUBLES" not in t
            and any(k in t for k in KALSHI_KINDS))  # fmt: skip


KALSHI_COLS = [
    "snapshot_ts", "series_ticker", "event_ticker", "ticker", "status", "title", "subtitle",
    "yes_sub_title", "no_sub_title", "strike_type", "floor_strike", "cap_strike", "open_time",
    "close_time", "yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume",
    "open_interest",
]  # fmt: skip

RUNDOWN = "https://therundown.io/api/v2"
RUNDOWN_SPORTS = {38: "ATP", 39: "WTA"}
RUNDOWN_BOOKS = {19: "draftkings", 22: "betmgm", 23: "fanduel"}
RUNDOWN_MARKETS = {1: "moneyline", 2: "spread", 3: "total"}
RUNDOWN_COLS = [
    "snapshot_ts", "tour", "event_id", "event_date", "player_1", "player_2", "market",
    "participant", "line", "book", "price", "updated_at", "closed_at",
]  # fmt: skip
DAY_CAP, MONTH_CAP = 18_000, 190_000  # below the free plan's 20,000 and 200,000, for safety
MIN_GAP = timedelta(hours=2)  # sportsbook snapshots at most this often


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def http_json(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[dict, dict]:
    """GET a JSON document; returns (body, lower-cased headers). Retries twice on errors."""
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, **(headers or {})})
    last: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return body, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (400, 401, 403, 404):
                break  # retrying won't help
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def write_snapshot(source: str, cols: list[str], rows: list[dict], ts: datetime) -> Path | None:
    if not rows:
        return None
    path = ROOT / "data" / source / ts.strftime("%Y/%m/%d") / f"{ts:%H%M%S}.csv.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write(buf.getvalue())
    return path


def load_state(name: str) -> dict:
    path = ROOT / "state" / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def save_state(name: str, data: dict) -> None:
    path = ROOT / "state" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


# ---- Kalshi -------------------------------------------------------------------------------


def _num(v):
    if v is None or v == "":
        return ""
    try:
        return float(v)
    except (TypeError, ValueError):
        return ""


def kalshi_series(ts: datetime, get=None) -> list[str]:
    """Tennis series tickers, from Kalshi's catalog at most once a day."""
    get = get or http_json
    state = load_state("kalshi_series")
    fresh = state.get("day") == ts.date().isoformat() and state.get("rule") == SERIES_RULE
    if fresh and state.get("series"):
        return state["series"]
    try:
        body, _ = get(f"{KALSHI}/series?{urllib.parse.urlencode({'category': 'Sports'})}")
        found = sorted(
            s["ticker"] for s in body.get("series", []) if tennis_series(s.get("ticker"))
        )
    except RuntimeError as e:
        print(f"  Kalshi catalog unavailable ({e}); using the fixed list")
        found = []
    series = sorted(set(found) | set(KALSHI_FALLBACK))  # the fixed list is always included
    save_state(
        "kalshi_series", {"day": ts.date().isoformat(), "rule": SERIES_RULE, "series": series}
    )
    return series


def kalshi_rows(series: str, ts: datetime, get=None) -> list[dict]:
    """Every open market in one series, across pages."""
    get = get or http_json
    rows, cursor = [], ""
    for _ in range(20):  # a page holds 1,000 markets; 20 pages is far more than tennis needs
        q = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        body, _ = get(f"{KALSHI}/markets?{urllib.parse.urlencode(q)}")
        for m in body.get("markets", []):
            rows.append({
                "snapshot_ts": ts.isoformat(),
                "series_ticker": series,
                **{k: m.get(k, "") for k in ("event_ticker", "ticker", "status", "title",
                                              "subtitle", "yes_sub_title", "no_sub_title",
                                              "strike_type", "open_time", "close_time")},
                "floor_strike": _num(m.get("floor_strike")),
                "cap_strike": _num(m.get("cap_strike")),
                "yes_bid": _num(m.get("yes_bid_dollars")),
                "yes_ask": _num(m.get("yes_ask_dollars")),
                "no_bid": _num(m.get("no_bid_dollars")),
                "no_ask": _num(m.get("no_ask_dollars")),
                "last_price": _num(m.get("last_price_dollars")),
                "volume": _num(m.get("volume_fp", m.get("volume"))),
                "open_interest": _num(m.get("open_interest_fp", m.get("open_interest"))),
            })  # fmt: skip
        cursor = body.get("cursor") or ""
        if not cursor:
            break
        time.sleep(0.2)
    return rows


def collect_kalshi(ts: datetime, get=None) -> tuple[int, list[str]]:
    get = get or http_json
    rows, problems = [], []
    for s in kalshi_series(ts, get):
        try:
            rows += kalshi_rows(s, ts, get)
        except RuntimeError as e:
            problems.append(f"{s}: {e}")
        time.sleep(0.1)
    path = write_snapshot("kalshi", KALSHI_COLS, rows, ts)
    print(f"Kalshi: {len(rows)} open markets" + (f" -> {path.relative_to(ROOT)}" if path else ""))
    return len(rows), problems


# ---- TheRundown ---------------------------------------------------------------------------


def budget_allows(ts: datetime, state: dict) -> tuple[bool, str]:
    """Take a sportsbook snapshot only if it fits today's fair share of the month and the
    last one was at least MIN_GAP ago."""
    day, month = ts.date().isoformat(), ts.strftime("%Y-%m")
    used_day = state.get("days", {}).get(day, 0)
    used_month = sum(v for d, v in state.get("days", {}).items() if d.startswith(month))
    cost = state.get("last_cost", 1500)  # a guess until the first bill
    days_left = calendar.monthrange(ts.year, ts.month)[1] - ts.day + 1
    share = min(DAY_CAP, (MONTH_CAP - used_month) / days_left)
    last = state.get("last_snapshot")
    if last and ts - datetime.fromisoformat(last) < MIN_GAP:
        return False, f"last snapshot {last}, under {MIN_GAP} ago"
    if used_day + cost > share:
        return False, f"today's share is {share:,.0f} points; used {used_day:,}, next ~{cost:,}"
    return True, f"used {used_day:,} today and {used_month:,} this month"


def rundown_rows(sport: int, date: str, ts: datetime, key: str, get=None):
    get = get or http_json
    q = {
        "market_ids": ",".join(map(str, RUNDOWN_MARKETS)),
        "affiliate_ids": ",".join(map(str, RUNDOWN_BOOKS)),
        "main_line": "true",
        "hide_closed": "true",
    }
    body, headers = get(
        f"{RUNDOWN}/sports/{sport}/events/{date}?{urllib.parse.urlencode(q)}",
        {"X-TheRundown-Key": key},
    )
    points = int(headers.get("x-datapoints", "0") or 0)
    rows = []
    for ev in body.get("events") or []:
        names = [t.get("name", "") for t in ev.get("teams") or []] + ["", ""]
        for mk in ev.get("markets") or []:
            market = RUNDOWN_MARKETS.get(mk.get("market_id"))
            if market is None or mk.get("period_id", 0) not in (0, None):
                continue  # full match only
            for part in mk.get("participants") or []:
                for line in part.get("lines") or []:
                    for book_id, quote in (line.get("prices") or {}).items():
                        book = RUNDOWN_BOOKS.get(int(book_id))
                        if book is None or not isinstance(quote, dict):
                            continue
                        rows.append({
                            "snapshot_ts": ts.isoformat(),
                            "tour": RUNDOWN_SPORTS[sport],
                            "event_id": ev.get("event_id", ""),
                            "event_date": ev.get("event_date", ""),
                            "player_1": names[0],
                            "player_2": names[1],
                            "market": market,
                            "participant": part.get("name", ""),
                            "line": line.get("value", ""),
                            "book": book,
                            "price": quote.get("price", ""),
                            "updated_at": quote.get("updated_at", ""),
                            "closed_at": quote.get("closed_at") or "",
                        })  # fmt: skip
    return rows, points


def collect_rundown(ts: datetime, get=None) -> tuple[int, list[str]]:
    get = get or http_json
    key = os.environ.get("THERUNDOWN_API_KEY", "").strip()
    if not key:
        print("Sportsbooks: skipped (no THERUNDOWN_API_KEY)")
        return 0, []
    state = load_state("rundown_budget")
    ok, why = budget_allows(ts, state)
    if not ok:
        print(f"Sportsbooks: skipped this run ({why})")
        return 0, []
    rows, points, problems = [], 0, []
    dates = [ts.date().isoformat(), (ts + timedelta(days=1)).date().isoformat()]
    for sport in RUNDOWN_SPORTS:
        for date in dates:
            try:
                got, cost = rundown_rows(sport, date, ts, key, get)
                rows += got
                points += cost
            except RuntimeError as e:
                problems.append(f"sport {sport} {date}: {e}")
            time.sleep(1.1)  # the free plan allows one request a second
    day = ts.date().isoformat()
    days = state.get("days", {})
    days[day] = days.get(day, 0) + points
    cutoff = (ts - timedelta(days=62)).date().isoformat()
    state["days"] = {d: v for d, v in days.items() if d >= cutoff}
    state["last_cost"] = max(points, 200)
    state["last_snapshot"] = ts.isoformat()
    save_state("rundown_budget", state)
    path = write_snapshot("sportsbooks", RUNDOWN_COLS, rows, ts)
    print(f"Sportsbooks: {len(rows)} prices for {points:,} data points"
          + (f" -> {path.relative_to(ROOT)}" if path else "") + f"; {why}")  # fmt: skip
    return len(rows), problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--kalshi-only", action="store_true")
    args = ap.parse_args(argv)
    ts = now_utc()
    print(f"snapshot {ts.isoformat()}")
    n_k, problems = collect_kalshi(ts)
    if not args.kalshi_only:
        _, more = collect_rundown(ts)
        problems += more
    for p in problems:
        print(f"  problem: {p}")
    # a run that got nothing from Kalshi fails, so a broken collector shows up in red
    return 1 if n_k == 0 and problems else 0


if __name__ == "__main__":
    sys.exit(main())
