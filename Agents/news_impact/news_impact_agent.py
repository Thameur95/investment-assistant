#!/usr/bin/env python3
"""News Impact Agent V1.

Collecte brute et prospective des actualites associees aux tickers surveilles.
Aucune classification semantique et aucun impact sur V1.
"""
from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from typing import Any

import yfinance as yf

from config import (
    AGENT_VERSION,
    EVENTS_FILE,
    OBSERVATION_ONLY,
    PORTFOLIO_V1_CHANGED,
    STOCKS_FILE,
    V1_EXECUTION_CHANGED,
)

EVENT_FIELDS = [
    "EventID",
    "AgentVersion",
    "Ticker",
    "ObservedAtUTC",
    "PublishedAtUTC",
    "Source",
    "Headline",
    "EventType",
    "Direction",
    "Importance",
    "Confidence",
    "ReferencePrice",
    "Reaction5mPct",
    "Reaction15mPct",
    "Reaction30mPct",
    "Reaction60mPct",
    "RelativeReaction60mPct",
    "VolumeRatio",
    "MarketConfirmed",
    "DataStatus",
    "ObservationOnly",
    "V1ExecutionChanged",
    "PortfolioV1Changed",
]

NEWS_COUNT_PER_TICKER = 10


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def load_tickers() -> list[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(f"Fichier stocks introuvable : {STOCKS_FILE}")
    return [
        line.strip()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_existing_events() -> list[dict[str, str]]:
    if not EVENTS_FILE.exists() or EVENTS_FILE.stat().st_size == 0:
        return []
    with EVENTS_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_events(rows: list[dict[str, Any]]) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def publication_time_utc(news: dict[str, Any]) -> str:
    content = news.get("content") or {}
    for value in (
        content.get("pubDate"),
        content.get("displayTime"),
        news.get("providerPublishTime"),
    ):
        if value in (None, ""):
            continue
        try:
            if isinstance(value, (int, float)):
                return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
            text = clean(value)
            if text:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            continue
    return ""


def extract_source(news: dict[str, Any]) -> str:
    content = news.get("content") or {}
    provider = content.get("provider") or {}
    return clean(
        provider.get("displayName")
        or news.get("publisher")
        or content.get("publisher")
    )


def extract_headline(news: dict[str, Any]) -> str:
    content = news.get("content") or {}
    return clean(content.get("title") or news.get("title"))


def make_event_id(ticker: str, published_at: str, source: str, headline: str) -> str:
    raw = "|".join([ticker, published_at, source, headline]).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:20]
    return f"NEWS|{ticker}|{digest}"


def fetch_news(ticker: str) -> list[dict[str, Any]]:
    search = yf.Search(ticker, news_count=NEWS_COUNT_PER_TICKER)
    return list(search.news or [])


def build_event(ticker: str, news: dict[str, Any], observed_at: str) -> dict[str, Any] | None:
    headline = extract_headline(news)
    if not headline:
        return None
    published_at = publication_time_utc(news)
    source = extract_source(news)
    event_id = make_event_id(ticker, published_at, source, headline)
    return {
        "EventID": event_id,
        "AgentVersion": AGENT_VERSION,
        "Ticker": ticker,
        "ObservedAtUTC": observed_at,
        "PublishedAtUTC": published_at,
        "Source": source,
        "Headline": headline,
        "EventType": "RAW_NEWS",
        "Direction": "",
        "Importance": "",
        "Confidence": "",
        "ReferencePrice": "",
        "Reaction5mPct": "",
        "Reaction15mPct": "",
        "Reaction30mPct": "",
        "Reaction60mPct": "",
        "RelativeReaction60mPct": "",
        "VolumeRatio": "",
        "MarketConfirmed": "",
        "DataStatus": "RAW_COLLECTED",
        "ObservationOnly": OBSERVATION_ONLY,
        "V1ExecutionChanged": V1_EXECUTION_CHANGED,
        "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
    }


def main() -> int:
    print("=" * 70)
    print("NEWS IMPACT AGENT")
    print("=" * 70)
    print("AgentVersion :", AGENT_VERSION)
    print("StocksFile :", STOCKS_FILE)
    print("EventsFile :", EVENTS_FILE)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    tickers = load_tickers()
    existing_rows = read_existing_events()
    known_ids = {clean(row.get("EventID")) for row in existing_rows if clean(row.get("EventID"))}
    new_rows: list[dict[str, Any]] = []
    errors = 0
    observed_at = datetime.now(timezone.utc).isoformat()

    print()
    print("Tickers charges :", len(tickers))

    for ticker in tickers:
        try:
            news_items = fetch_news(ticker)
            added = 0
            for item in news_items:
                event = build_event(ticker, item, observed_at)
                if event is None or event["EventID"] in known_ids:
                    continue
                known_ids.add(event["EventID"])
                new_rows.append(event)
                added += 1
            print(ticker, "| news recues :", len(news_items), "| nouvelles :", added)
        except Exception as exc:
            errors += 1
            print(ticker, "| ERREUR NEWS :", exc)

    all_rows = existing_rows + new_rows
    write_events(all_rows)

    print()
    print("Nouvelles actualites enregistrees :", len(new_rows))
    print("Total evenements dans la base :", len(all_rows))
    print("Erreurs collecte :", errors)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
