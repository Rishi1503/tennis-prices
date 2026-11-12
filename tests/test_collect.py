"""The collector, against replies shaped like the documented ones (Kalshi trade API v2,
TheRundown API v2). No network: http_json is replaced."""

import gzip
import json
from datetime import UTC, datetime

import pytest

import collect

TS = datetime(2026, 11, 15, 14, 7, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(collect, "ROOT", tmp_path)
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    return tmp_path


def market(ticker, sub, bid, ask, **extra):
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "status": "active",
        "title": "Will Sinner win?",
        "yes_sub_title": sub,
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "no_bid_dollars": "0.3300",
        "no_ask_dollars": "0.3400",
        "last_price_dollars": "0.6600",
        "volume_fp": "1520.00",
        "close_time": "2026-11-15T20:00:00Z",
        **extra,
    }


def fake_kalshi(url, headers=None, timeout=30):
    if "/series" in url:
        return {
            "series": [
                {"ticker": "KXATPMATCH", "tags": ["Tennis"]},
                {"ticker": "KXATPSETWINNER", "tags": ["Tennis"]},
                {"ticker": "KXNFLGAME", "tags": ["Football"]},
            ]
        }, {}
    if "series_ticker=KXATPMATCH" in url and "cursor=" not in url:
        return {
            "markets": [
                market("KXATPMATCH-26NOV15SINALC-SIN", "Jannik Sinner", "0.6600", "0.6700")
            ],
            "cursor": "page2",
        }, {}
    if "series_ticker=KXATPMATCH" in url:
        return {
            "markets": [
                market("KXATPMATCH-26NOV15SINALC-ALC", "Carlos Alcaraz", "0.3300", "0.3400")
            ],
            "cursor": "",
        }, {}
    if "series_ticker=KXATPSETWINNER" in url:
        return {
            "markets": [
                market(
                    "KXATPSETWINNER-26NOV15SINALC1-SIN",
                    "Sinner",
                    "0.6100",
                    "0.6300",
                    floor_strike=None,
                )
            ]
        }, {}
    raise AssertionError(url)


def read(path):
    with gzip.open(path, "rt") as f:
        return f.read().splitlines()


def test_kalshi_snapshot_reads_every_page_and_dollar_prices(isolated):
    n, problems = collect.collect_kalshi(TS, fake_kalshi)
    assert n == 3 and problems == []
    (path,) = list((isolated / "data/kalshi/2026/11/15").glob("*.csv.gz"))
    assert path.name == "140700.csv.gz"
    lines = read(path)
    assert lines[0].split(",")[:4] == ["snapshot_ts", "series_ticker", "event_ticker", "ticker"]
    first = dict(zip(collect.KALSHI_COLS, lines[1].split(","), strict=False))
    assert first["yes_bid"] == "0.66" and first["volume"] == "1520.0"
    state = json.loads((isolated / "state/kalshi_series.json").read_text())
    assert state["series"] == ["KXATPMATCH", "KXATPSETWINNER"]  # football left out


def test_the_series_list_falls_back_when_the_catalog_fails(isolated):
    def broken(url, headers=None, timeout=30):
        if "/series" in url:
            raise RuntimeError("503")
        return {"markets": []}, {}

    assert collect.kalshi_series(TS, broken) == collect.KALSHI_FALLBACK


EVENT = {
    "event_id": "ev1",
    "event_date": "2026-11-15T19:00:00Z",
    "teams": [{"name": "Jannik Sinner"}, {"name": "Carlos Alcaraz"}],
    "markets": [
        {
            "market_id": 1,
            "period_id": 0,
            "participants": [
                {
                    "name": "Jannik Sinner",
                    "lines": [
                        {
                            "value": "",
                            "prices": {
                                "19": {"price": -180, "updated_at": "t"},
                                "23": {"price": -175},
                            },
                        }
                    ],
                },
                {
                    "name": "Carlos Alcaraz",
                    "lines": [{"value": "", "prices": {"19": {"price": 150}, "3": {"price": 160}}}],
                },
            ],
        },  # 3 is no free book: dropped
        {
            "market_id": 3,
            "period_id": 0,
            "participants": [
                {"name": "Over", "lines": [{"value": "22.5", "prices": {"22": {"price": -110}}}]}
            ],
        },
        {
            "market_id": 1,
            "period_id": 1,
            "participants": [  # first set only: dropped
                {
                    "name": "Jannik Sinner",
                    "lines": [{"value": "", "prices": {"19": {"price": -150}}}],
                }
            ],
        },
    ],
}


def fake_rundown(url, headers=None, timeout=30):
    assert headers["X-TheRundown-Key"] == "k"
    assert "affiliate_ids=19%2C22%2C23" in url and "main_line=true" in url
    events = [EVENT] if "/sports/38/" in url and "2026-11-15" in url else []
    return {"events": events}, {"x-datapoints": "40" if events else "0"}


def test_sportsbook_snapshot_keeps_free_books_and_full_match_markets(isolated, monkeypatch):
    monkeypatch.setenv("THERUNDOWN_API_KEY", "k")
    n, problems = collect.collect_rundown(TS, fake_rundown)
    assert n == 4 and problems == []
    (path,) = list((isolated / "data/sportsbooks/2026/11/15").glob("*.csv.gz"))
    rows = [
        dict(zip(collect.RUNDOWN_COLS, line.split(","), strict=False)) for line in read(path)[1:]
    ]
    assert {(r["participant"], r["book"], r["price"]) for r in rows} == {
        ("Jannik Sinner", "draftkings", "-180"),
        ("Jannik Sinner", "fanduel", "-175"),
        ("Carlos Alcaraz", "draftkings", "150"),
        ("Over", "betmgm", "-110"),
    }
    assert all(r["tour"] == "ATP" and r["player_2"] == "Carlos Alcaraz" for r in rows)
    budget = json.loads((isolated / "state/rundown_budget.json").read_text())
    assert budget["days"]["2026-11-15"] == 40 and budget["last_snapshot"] == TS.isoformat()


def test_the_budget_spaces_snapshots_and_respects_the_month():
    day = TS.date().isoformat()
    ok, _ = collect.budget_allows(TS, {})
    assert ok  # nothing used yet
    recent = {"last_snapshot": "2026-11-15T13:00:00+00:00", "days": {day: 100}, "last_cost": 100}
    assert not collect.budget_allows(TS, recent)[0]  # an hour ago: too soon
    spent = {"days": {"2026-11-01": 185_000}, "last_cost": 1000}
    ok, why = collect.budget_allows(TS, spent)
    assert not ok and "share" in why  # 5,000 left over 16 days: about 300 a day
    today_full = {"days": {day: 11_000}, "last_cost": 1000}
    assert not collect.budget_allows(TS, today_full)[0]  # (190,000 - 11,000) / 16 days: ~11,200


def test_no_key_means_no_sportsbook_calls(monkeypatch):
    monkeypatch.delenv("THERUNDOWN_API_KEY", raising=False)

    def never(*a, **k):
        raise AssertionError("called")

    assert collect.collect_rundown(TS, never) == (0, [])


def test_a_run_with_no_open_markets_is_not_a_failure(monkeypatch, isolated):
    monkeypatch.setattr(collect, "now_utc", lambda: TS)
    monkeypatch.setattr(
        collect,
        "http_json",
        lambda url, headers=None, timeout=30: ({"series": [], "markets": []}, {}),
    )
    assert collect.main(["--kalshi-only"]) == 0  # the off-season: nothing listed, nothing wrong
    assert not (isolated / "data/kalshi").exists()
