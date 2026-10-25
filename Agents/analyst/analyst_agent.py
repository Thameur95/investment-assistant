#!/usr/bin/env python3
"""Analyst Agent V1.1 - multi-agent synthesis, Shadow only.

Corrections V1.1:
- selects the newest row per ticker by explicit timestamps, never by CSV order;
- discovers existing agent outputs defensively and prints source coverage;
- does not count an Earnings MIXED verdict again as a negative factor;
- separates source availability from genuinely UNKNOWN analytical output;
- refuses HIGH confidence when key sources are unavailable.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = Path(__file__).resolve().parent / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
OUT = RESULTS_DIR / "analyst_observations.csv"
SUMMARY = RESULTS_DIR / "analyst_summary.json"

VERSION = "ANALYST_V1_1"
OBSERVATION_ONLY = True
V1_EXECUTION_CHANGED = False
PORTFOLIO_V1_CHANGED = False

COLS = [
    "AnalystVersion", "ObservedAtUTC", "Ticker",
    "EarningsAssessment", "NewsAssessment", "CatalystAssessment",
    "TechnicalAssessment", "V1Context", "BullFactors", "BearFactors",
    "RiskFlags", "MissingInformation", "Contradictions",
    "OverallAssessment", "Confidence", "NeedsReview",
    "ObservationOnly", "V1ExecutionChanged", "PortfolioV1Changed",
]

SOURCE_CANDIDATES = {
    "earnings": [
        "Agents/earnings_company/results/earnings_company_events.csv",
    ],
    "news": [
        "Agents/news_impact/results/news_impact_events.csv",
        "Agents/news_impact/results/news_impact.csv",
        "Agents/news_impact/news_impact_events.csv",
    ],
    "catalyst": [
        "catalyst_semantic.csv",
        "results/catalyst_semantic.csv",
        "data/catalyst_semantic.csv",
        "Agents/catalyst/results/catalyst_semantic.csv",
    ],
    "trade": [
        "trade_intelligence_log.csv",
        "results/trade_intelligence_log.csv",
        "data/trade_intelligence_log.csv",
        "Agents/trade_intelligence/results/trade_intelligence_log.csv",
    ],
    "structure": [
        "Agents/structure/results/structure_shadow.csv",
        "Agents/structure_shadow/results/structure_shadow.csv",
        "structure_shadow.csv",
        "structure_shadow_log.csv",
    ],
}

TIMESTAMP_FIELDS = (
    "PublishedAtUTC", "ObservedAtUTC", "EvaluatedAtUTC", "FirstSeenUTC",
    "TimestampUTC", "DecisionTime", "SessionDate", "SignalDate", "Date",
)


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def read_csv(path: Path | None) -> list[dict[str, str]]:
    if path is None or not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def locate_source(kind: str) -> Path | None:
    for relative in SOURCE_CANDIDATES[kind]:
        path = ROOT / relative
        if path.exists() and path.is_file():
            return path
    # Fallback by distinctive filename fragments. Read-only discovery.
    patterns = {
        "earnings": "*earnings*events*.csv",
        "news": "*news*impact*.csv",
        "catalyst": "*catalyst*semantic*.csv",
        "trade": "*trade*intelligence*.csv",
        "structure": "*structure*shadow*.csv",
    }
    matches = sorted(
        p for p in ROOT.rglob(patterns[kind])
        if p.is_file() and ".git" not in p.parts and "analyst" not in p.parts
    )
    return matches[0] if len(matches) == 1 else None


def time_key(row: dict[str, Any]) -> tuple[str, ...]:
    # ISO dates sort correctly lexicographically. Use every explicit timestamp
    # as deterministic fallbacks. Missing timestamps never beat present ones.
    return tuple(clean(row.get(field)) for field in TIMESTAMP_FIELDS)


def latest_by_ticker(data: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in data:
        ticker = clean(row.get("Ticker") or row.get("ticker") or row.get("Symbol")).upper()
        if not ticker:
            continue
        previous = output.get(ticker)
        if previous is None or time_key(row) > time_key(previous):
            output[ticker] = row
    return output


def uniq(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def split_flags(value: Any) -> list[str]:
    return [item for item in clean(value).split("|") if item]


def first_value(row: dict[str, Any], keys: Iterable[str], default: str = "UNKNOWN") -> str:
    for key in keys:
        value = clean(row.get(key))
        if value:
            return value
    return default


def earnings_factors(row: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    bull: list[str] = []
    bear: list[str] = []
    risk = split_flags(row.get("RiskFlags"))
    missing = split_flags(row.get("MissingInformation"))
    contradictions: list[str] = []

    verdict = clean(row.get("OverallVerdict")).upper()
    expectation = clean(row.get("ExpectationAssessment")).upper()
    trend = clean(row.get("KeyKPITrend")).upper()
    market = clean(row.get("MarketReactionAssessment")).upper()

    if verdict == "POSITIVE":
        bull.append("EARNINGS_POSITIVE")
    elif verdict == "NEGATIVE":
        bear.append("EARNINGS_NEGATIVE")
    # MIXED is context, not a second bearish vote.

    if expectation == "BEAT":
        bull.append("REVENUE_BEAT")
    elif expectation == "MISS":
        bear.append("REVENUE_MISS")

    if trend == "GROWING":
        bull.append("KPI_GROWING")
    elif trend in {"DECLINING", "DECELERATING_REPORTED_RATES"}:
        bear.append("KPI_WEAKENING")

    if market == "NEGATIVE":
        bear.append("MARKET_REACTION_NEGATIVE")
    elif market == "POSITIVE":
        bull.append("MARKET_REACTION_POSITIVE")

    if verdict == "POSITIVE" and market == "NEGATIVE":
        contradictions.append("POSITIVE_FUNDAMENTALS_VS_NEGATIVE_MARKET")

    return bull, bear, risk, missing, contradictions


def main() -> int:
    observed = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    paths = {kind: locate_source(kind) for kind in SOURCE_CANDIDATES}
    datasets = {kind: latest_by_ticker(read_csv(path)) for kind, path in paths.items()}

    print("ANALYST AGENT V1.1")
    print("SOURCE COVERAGE")
    for kind, path in paths.items():
        print(f"{kind}: {path if path else 'NOT_FOUND'} | tickers={len(datasets[kind])}")

    tickers = sorted(set().union(*(set(dataset) for dataset in datasets.values())))
    output: list[dict[str, Any]] = []

    for ticker in tickers:
        e = datasets["earnings"].get(ticker)
        n = datasets["news"].get(ticker)
        c = datasets["catalyst"].get(ticker)
        tr = datasets["trade"].get(ticker)
        st = datasets["structure"].get(ticker)

        if e:
            bull, bear, risk, missing, contradictions = earnings_factors(e)
            earnings_assessment = first_value(e, ("OverallVerdict",))
        else:
            bull, bear, risk, missing, contradictions = [], [], [], ["EARNINGS_UNAVAILABLE"], []
            earnings_assessment = "UNKNOWN"

        if n:
            news_assessment = first_value(n, ("OverallVerdict", "ImpactAssessment", "Sentiment", "NewsImpact"))
            if news_assessment.upper() in {"POSITIVE", "BULLISH"}:
                bull.append("NEWS_POSITIVE")
            elif news_assessment.upper() in {"NEGATIVE", "BEARISH"}:
                bear.append("NEWS_NEGATIVE")
        else:
            news_assessment = "UNKNOWN"
            missing.append("NEWS_SOURCE_UNAVAILABLE")

        if c:
            catalyst_assessment = first_value(c, ("CatalystType", "Category", "Sentiment"))
        else:
            catalyst_assessment = "UNKNOWN"
            missing.append("CATALYST_SOURCE_UNAVAILABLE")

        structure_row = st or tr
        if structure_row:
            technical_assessment = first_value(
                structure_row,
                ("OverallAssessment", "StructureState", "StructureSignalType", "StructureStatus", "Decision"),
            )
        else:
            technical_assessment = "UNKNOWN"
            missing.append("TECHNICAL_SOURCE_UNAVAILABLE")

        if tr:
            v1_context = first_value(tr, ("V1Decision", "V1Status", "Decision", "Action"))
        else:
            v1_context = "UNKNOWN"
            missing.append("TRADE_INTELLIGENCE_SOURCE_UNAVAILABLE")

        bull = uniq(bull)
        bear = uniq(bear)
        risk = uniq(risk)
        missing = uniq(missing)
        contradictions = uniq(contradictions)

        # No double voting: MIXED Earnings remains MIXED unless independent
        # sources add directional evidence.
        if bull and bear:
            overall = "MIXED"
        elif bull:
            overall = "POSITIVE"
        elif bear:
            overall = "NEGATIVE"
        elif earnings_assessment == "MIXED":
            overall = "MIXED"
        else:
            overall = "UNKNOWN"

        independent_available = sum(path is not None and ticker in datasets[kind] for kind, path in paths.items())
        if overall == "UNKNOWN":
            confidence = "LOW"
        elif independent_available >= 3 and not missing:
            confidence = "HIGH"
        else:
            confidence = "MEDIUM"

        output.append({
            "AnalystVersion": VERSION,
            "ObservedAtUTC": observed,
            "Ticker": ticker,
            "EarningsAssessment": earnings_assessment,
            "NewsAssessment": news_assessment,
            "CatalystAssessment": catalyst_assessment,
            "TechnicalAssessment": technical_assessment,
            "V1Context": v1_context,
            "BullFactors": "|".join(bull),
            "BearFactors": "|".join(bear),
            "RiskFlags": "|".join(risk),
            "MissingInformation": "|".join(missing),
            "Contradictions": "|".join(contradictions),
            "OverallAssessment": overall,
            "Confidence": confidence,
            "NeedsReview": bool(missing or contradictions),
            "ObservationOnly": OBSERVATION_ONLY,
            "V1ExecutionChanged": V1_EXECUTION_CHANGED,
            "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
        })

    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLS)
        writer.writeheader()
        writer.writerows(output)

    summary = {
        "GeneratedAtUTC": observed,
        "TickersAnalyzed": len(output),
        "Sources": {kind: str(path.relative_to(ROOT)) if path else "NOT_FOUND" for kind, path in paths.items()},
        "SourceTickerCounts": {kind: len(dataset) for kind, dataset in datasets.items()},
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
    }
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Tickers analyses :", len(output))
    print("Resultats :", OUT)
    print("ObservationOnly :", OBSERVATION_ONLY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
