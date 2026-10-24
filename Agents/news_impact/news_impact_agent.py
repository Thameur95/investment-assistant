#!/usr/bin/env python3
"""News Impact Agent V1.

Lit en lecture seule les événements et classifications sémantiques produits
par Catalyst, puis mesure en mode Shadow la réaction prix/volume du titre.

Aucune écriture dans Catalyst, aucune modification de V1 ou du portefeuille V1.
"""
from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yfinance as yf

from config import (
    AGENT_VERSION,
    CATALYST_EVENTS_FILE,
    CATALYST_SEMANTIC_FILE,
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

REACTION_WINDOWS_MINUTES = (5, 15, 30, 60)
PRICE_PERIOD = "60d"
PRICE_INTERVAL = "5m"
VOLUME_LOOKBACK_BARS = 20


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_utc(value: Any) -> Optional[pd.Timestamp]:
    text = clean(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=EVENT_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def load_tickers() -> set[str]:
    if not STOCKS_FILE.exists():
        raise FileNotFoundError(
            f"Fichier stocks introuvable : {STOCKS_FILE}"
        )
    return {
        line.strip().upper()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def load_semantic_map() -> dict[str, dict[str, str]]:
    """Indexe catalyst_semantic.csv par EventID, sans modifier le fichier."""
    semantic_map: dict[str, dict[str, str]] = {}
    for row in read_csv(CATALYST_SEMANTIC_FILE):
        event_id = clean(row.get("EventID"))
        if event_id:
            semantic_map[event_id] = row
    return semantic_map


def semantic_bool(value: Any) -> bool:
    return clean(value).upper() in {"TRUE", "1", "YES", "OUI"}


def normalize_prices(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data
    result = data.copy()
    if isinstance(result.columns, pd.MultiIndex):
        result.columns = result.columns.get_level_values(0)
    result.index = pd.to_datetime(result.index, errors="coerce", utc=True)
    result = result.loc[result.index.notna()].sort_index()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in result.columns:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    return result


def download_prices(ticker: str) -> pd.DataFrame:
    data = yf.download(
        ticker,
        period=PRICE_PERIOD,
        interval=PRICE_INTERVAL,
        auto_adjust=True,
        prepost=False,
        progress=False,
        threads=False,
    )
    return normalize_prices(data)


def benchmark_for(ticker: str) -> str:
    if ticker.endswith(".PA"):
        return "^FCHI"
    if ticker.endswith((".AS", ".DE", ".BR", ".MI", ".MC", ".SW", ".L")):
        return "^STOXX50E"
    if ticker in {"TTE", "SU", "VLO"}:
        return "XLE"
    return "SPY"


def price_before(data: pd.DataFrame, event_time: pd.Timestamp) -> Optional[float]:
    before = data.loc[data.index <= event_time, "Close"].dropna()
    if before.empty:
        return None
    return float(before.iloc[-1])


def price_after(
    data: pd.DataFrame,
    event_time: pd.Timestamp,
    minutes: int,
) -> Optional[float]:
    target = event_time + pd.Timedelta(minutes=minutes)
    after = data.loc[data.index >= target, "Close"].dropna()
    if after.empty:
        return None
    return float(after.iloc[0])


def pct_change(
    value: Optional[float],
    reference: Optional[float],
) -> Optional[float]:
    if value is None or reference is None or reference == 0:
        return None
    result = (value / reference - 1.0) * 100.0
    return result if math.isfinite(result) else None


def rounded(value: Optional[float], decimals: int = 4) -> Any:
    return "" if value is None else round(value, decimals)


def volume_ratio(
    data: pd.DataFrame,
    event_time: pd.Timestamp,
) -> Optional[float]:
    if "Volume" not in data.columns:
        return None
    after = data.loc[data.index >= event_time, "Volume"].dropna()
    before = (
        data.loc[data.index < event_time, "Volume"]
        .dropna()
        .tail(VOLUME_LOOKBACK_BARS)
    )
    if after.empty or before.empty:
        return None
    baseline = float(before.mean())
    if baseline <= 0:
        return None
    ratio = float(after.iloc[0]) / baseline
    return ratio if math.isfinite(ratio) else None


def event_time_from_catalyst(
    row: dict[str, str],
) -> Optional[pd.Timestamp]:
    return parse_utc(
        row.get("FirstSeenUTC")
        or row.get("LastSeenUTC")
    )


def market_confirmation(
    direction: str,
    relative_reaction_60: Optional[float],
    is_relevant: bool,
) -> str:
    """Compare le sens sémantique à la réaction relative à 60 minutes."""
    if not is_relevant or relative_reaction_60 is None:
        return ""

    normalized = clean(direction).upper()
    if normalized == "POSITIVE":
        return "TRUE" if relative_reaction_60 > 0 else "FALSE"
    if normalized == "NEGATIVE":
        return "TRUE" if relative_reaction_60 < 0 else "FALSE"

    # NEUTRAL ou direction inconnue : aucune direction à confirmer.
    return ""


def base_output_row(
    event: dict[str, str],
    semantic: dict[str, str],
    observed_at: str,
) -> dict[str, Any]:
    published = clean(
        event.get("FirstSeenUTC")
        or event.get("LastSeenUTC")
    )
    return {
        "EventID": clean(event.get("EventID")),
        "AgentVersion": AGENT_VERSION,
        "Ticker": clean(event.get("Ticker")).upper(),
        "ObservedAtUTC": observed_at,
        "PublishedAtUTC": published,
        "Source": clean(event.get("Sources")),
        "Headline": clean(event.get("RepresentativeTitle")),
        "EventType": (
            clean(semantic.get("CatalystType"))
            or clean(event.get("CategoryRaw"))
            or "CATALYST_EVENT"
        ),
        "Direction": clean(semantic.get("Direction")),
        "Importance": clean(semantic.get("Importance")),
        "Confidence": clean(semantic.get("Confidence")),
        "ReferencePrice": "",
        "Reaction5mPct": "",
        "Reaction15mPct": "",
        "Reaction30mPct": "",
        "Reaction60mPct": "",
        "RelativeReaction60mPct": "",
        "VolumeRatio": "",
        "MarketConfirmed": "",
        "DataStatus": "PENDING",
        "ObservationOnly": OBSERVATION_ONLY,
        "V1ExecutionChanged": V1_EXECUTION_CHANGED,
        "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
    }


def evaluate_event(
    event: dict[str, str],
    observed_at: str,
    price_cache: dict[str, pd.DataFrame],
    semantic_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    event_id = clean(event.get("EventID"))
    semantic = semantic_map.get(event_id, {})
    output = base_output_row(event, semantic, observed_at)
    ticker = output["Ticker"]
    event_time = event_time_from_catalyst(event)

    if not event_id or not ticker:
        output["DataStatus"] = "INVALID_CATALYST_EVENT"
        return output
    if event_time is None:
        output["DataStatus"] = "INVALID_EVENT_TIME"
        return output

    prices = price_cache.get(ticker, pd.DataFrame())
    if prices.empty or "Close" not in prices.columns:
        output["DataStatus"] = "PRICE_DATA_UNAVAILABLE"
        return output

    reference = price_before(prices, event_time)
    if reference is None:
        output["DataStatus"] = "REFERENCE_PRICE_UNAVAILABLE"
        return output

    reactions: dict[int, Optional[float]] = {}
    for minutes in REACTION_WINDOWS_MINUTES:
        reactions[minutes] = pct_change(
            price_after(prices, event_time, minutes),
            reference,
        )

    ratio = volume_ratio(prices, event_time)
    benchmark = benchmark_for(ticker)
    benchmark_prices = price_cache.get(benchmark, pd.DataFrame())
    benchmark_60: Optional[float] = None

    if not benchmark_prices.empty and "Close" in benchmark_prices.columns:
        benchmark_reference = price_before(benchmark_prices, event_time)
        benchmark_60 = pct_change(
            price_after(benchmark_prices, event_time, 60),
            benchmark_reference,
        )

    relative_60: Optional[float] = None
    if reactions[60] is not None and benchmark_60 is not None:
        relative_60 = reactions[60] - benchmark_60

    output.update(
        {
            "ReferencePrice": rounded(reference),
            "Reaction5mPct": rounded(reactions[5]),
            "Reaction15mPct": rounded(reactions[15]),
            "Reaction30mPct": rounded(reactions[30]),
            "Reaction60mPct": rounded(reactions[60]),
            "RelativeReaction60mPct": rounded(relative_60),
            "VolumeRatio": rounded(ratio),
            "MarketConfirmed": market_confirmation(
                output["Direction"],
                relative_60,
                semantic_bool(semantic.get("IsRelevant")),
            ),
        }
    )

    available_reactions = sum(
        reactions[minutes] is not None
        for minutes in REACTION_WINDOWS_MINUTES
    )

    if available_reactions == len(REACTION_WINDOWS_MINUTES):
        output["DataStatus"] = (
            "REACTION_COMPLETE_SEMANTIC"
            if semantic
            else "REACTION_COMPLETE_NO_SEMANTIC"
        )
    elif available_reactions > 0:
        output["DataStatus"] = (
            "REACTION_PARTIAL_SEMANTIC"
            if semantic
            else "REACTION_PARTIAL_NO_SEMANTIC"
        )
    else:
        output["DataStatus"] = "REACTION_PENDING"

    return output


def merge_rows(
    existing: list[dict[str, str]],
    current: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    for row in existing:
        event_id = clean(row.get("EventID"))
        if event_id:
            merged[event_id] = row

    for row in current:
        event_id = clean(row.get("EventID"))
        if event_id:
            merged[event_id] = row

    return sorted(
        merged.values(),
        key=lambda row: (
            clean(row.get("PublishedAtUTC")),
            clean(row.get("Ticker")),
            clean(row.get("EventID")),
        ),
        reverse=True,
    )


def main() -> int:
    print("=" * 80)
    print("NEWS IMPACT AGENT")
    print("CATALYST SEMANTIC -> REACTION PRIX / VOLUME")
    print("=" * 80)
    print("AgentVersion :", AGENT_VERSION)
    print("CatalystEventsFile :", CATALYST_EVENTS_FILE)
    print("CatalystSemanticFile :", CATALYST_SEMANTIC_FILE)
    print("EventsFile :", EVENTS_FILE)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)

    if not CATALYST_EVENTS_FILE.exists():
        print(
            "ERREUR : fichier Catalyst introuvable :",
            CATALYST_EVENTS_FILE,
        )
        return 2

    if not CATALYST_SEMANTIC_FILE.exists():
        print(
            "ERREUR : fichier semantique Catalyst introuvable :",
            CATALYST_SEMANTIC_FILE,
        )
        return 2

    watchlist = load_tickers()
    semantic_map = load_semantic_map()

    catalyst_events = [
        row
        for row in read_csv(CATALYST_EVENTS_FILE)
        if clean(row.get("Ticker")).upper() in watchlist
    ]

    print()
    print("Tickers surveilles :", len(watchlist))
    print("Evenements Catalyst lus :", len(catalyst_events))
    print("Evenements semantiques indexes :", len(semantic_map))

    required_symbols = {
        clean(row.get("Ticker")).upper()
        for row in catalyst_events
        if clean(row.get("Ticker"))
    }

    # Copie indispensable : ne jamais modifier un set pendant son itération.
    tracked_tickers = set(required_symbols)
    required_symbols.update(
        benchmark_for(ticker)
        for ticker in tracked_tickers
    )

    price_cache: dict[str, pd.DataFrame] = {}
    download_errors = 0

    for symbol in sorted(required_symbols):
        try:
            price_cache[symbol] = download_prices(symbol)
            print(
                symbol,
                "| bougies 5 min :",
                len(price_cache[symbol]),
            )
        except Exception as exc:
            download_errors += 1
            price_cache[symbol] = pd.DataFrame()
            print(symbol, "| ERREUR PRIX :", exc)

    observed_at = datetime.now(timezone.utc).isoformat()

    current_rows = [
        evaluate_event(
            event,
            observed_at,
            price_cache,
            semantic_map,
        )
        for event in catalyst_events
    ]

    combined = merge_rows(
        read_csv(EVENTS_FILE),
        current_rows,
    )
    write_csv(EVENTS_FILE, combined)

    status_counts: dict[str, int] = {}
    direction_counts: dict[str, int] = {}
    confirmation_counts: dict[str, int] = {}

    for row in current_rows:
        status = clean(row.get("DataStatus")) or "UNKNOWN"
        direction = clean(row.get("Direction")) or "NO_DIRECTION"
        confirmation = clean(row.get("MarketConfirmed")) or "NOT_APPLICABLE"

        status_counts[status] = status_counts.get(status, 0) + 1
        direction_counts[direction] = direction_counts.get(direction, 0) + 1
        confirmation_counts[confirmation] = (
            confirmation_counts.get(confirmation, 0) + 1
        )

    print()
    print("Evenements evalues :", len(current_rows))

    for status, count in sorted(status_counts.items()):
        print(status, ":", count)

    print()
    print("Directions semantiques :")
    for direction, count in sorted(direction_counts.items()):
        print(direction, ":", count)

    print()
    print("Confirmation marche :")
    for confirmation, count in sorted(confirmation_counts.items()):
        print(confirmation, ":", count)

    print()
    print("Total lignes News Impact :", len(combined))
    print("Erreurs telechargement prix :", download_errors)
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("=" * 80)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
