#!/usr/bin/env python3
"""Challenger Factory V1 - transforme les observations Analyste en hypothèses testables.

Shadow only. Ne modifie jamais le Champion V1, le portefeuille ni l'exécution.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent
RESULTS = BASE / "results"
RESULTS.mkdir(parents=True, exist_ok=True)

ANALYST_FILE = ROOT / "Agents" / "analyst" / "results" / "analyst_observations.csv"
OUT = RESULTS / "challenger_hypotheses.csv"
SUMMARY = RESULTS / "challenger_factory_summary.json"

VERSION = "CHALLENGER_FACTORY_V1"
OBSERVATION_ONLY = True
V1_EXECUTION_CHANGED = False
PORTFOLIO_V1_CHANGED = False

MIN_SAMPLE_FOR_TEST = 5
MIN_SAMPLE_FOR_PROMOTION_REVIEW = 20

COLUMNS = [
    "FactoryVersion", "GeneratedAtUTC", "HypothesisID", "Ticker",
    "TriggerType", "TriggerEvidence", "Hypothesis", "VariableUnderTest",
    "ChampionRuleChanged", "ExperimentMode", "RequiredSample",
    "CurrentSample", "ExperimentStatus", "EvaluationMetric",
    "RiskFlags", "MissingInformation", "AnalystAssessment",
    "AnalystConfidence", "NeedsReview", "ObservationOnly",
    "V1ExecutionChanged", "PortfolioV1Changed",
]


def clean(value: Any) -> str:
    return "" if value is None else " ".join(str(value).split()).strip()


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def tokens(value: Any) -> set[str]:
    return {part for part in clean(value).split("|") if part}


def slug(value: str) -> str:
    return "_".join("".join(ch if ch.isalnum() else " " for ch in value.upper()).split())


def propose(row: dict[str, str]) -> list[dict[str, str]]:
    ticker = clean(row.get("Ticker")).upper()
    bears = tokens(row.get("BearFactors"))
    risks = tokens(row.get("RiskFlags"))
    missing = tokens(row.get("MissingInformation"))
    contradictions = tokens(row.get("Contradictions"))
    overall = clean(row.get("OverallAssessment")).upper()
    confidence = clean(row.get("Confidence")).upper()
    proposals: list[dict[str, str]] = []

    def add(trigger: str, evidence: str, hypothesis: str, variable: str, metric: str) -> None:
        proposals.append({
            "Ticker": ticker,
            "TriggerType": trigger,
            "TriggerEvidence": evidence,
            "Hypothesis": hypothesis,
            "VariableUnderTest": variable,
            "EvaluationMetric": metric,
            "RiskFlags": clean(row.get("RiskFlags")),
            "MissingInformation": clean(row.get("MissingInformation")),
            "AnalystAssessment": overall,
            "AnalystConfidence": confidence,
        })

    if "KPI_DECLINING" in risks or "KPI_WEAKENING" in bears:
        add(
            "KPI_WEAKENING",
            "KPI_DECLINING/KPI_WEAKENING",
            "Tester si un KPI métier en baisse ou en ralentissement dégrade le rendement post-signal par rapport aux cas sans affaiblissement KPI.",
            "KPI_TREND_FILTER",
            "ForwardReturn|WinRate|MaxAdverseExcursion",
        )

    if "AFTERMARKET_REPORTED_RATE_DECELERATION" in risks:
        add(
            "REPORTED_RATE_DECELERATION",
            "AFTERMARKET_REPORTED_RATE_DECELERATION",
            "Tester si une séquence décroissante de taux KPI publiés améliore l'identification des publications dont la réaction de marché est défavorable malgré des fondamentaux solides.",
            "REPORTED_RATE_DECELERATION_FLAG",
            "PostEarningsReturn|RelativeReturn|VolumeReaction",
        )

    if "POSITIVE_RESULTS_NEGATIVE_REACTION" in risks or "POSITIVE_FUNDAMENTALS_VS_NEGATIVE_MARKET" in contradictions:
        add(
            "MARKET_DIVERGENCE",
            "POSITIVE_RESULTS_NEGATIVE_REACTION",
            "Tester si une divergence fondamentaux positifs / réaction marché négative prédit une sous-performance à court terme.",
            "MARKET_DIVERGENCE_FLAG",
            "ForwardReturn1D|ForwardReturn3D|RelativeReturn",
        )

    if "REVENUE_MISS" in bears:
        add(
            "REVENUE_MISS",
            "REVENUE_MISS",
            "Tester si un Revenue MISS combiné au contexte technique actuel réduit la qualité des entrées V1.",
            "REVENUE_MISS_CONTEXT",
            "TradePnL|WinRate|MAE|MFE",
        )

    if overall == "UNKNOWN" and missing:
        add(
            "INSUFFICIENT_INFORMATION",
            "|".join(sorted(missing)),
            "Mesurer si les verdicts UNKNOWN sont principalement dus à des sources absentes plutôt qu'à des signaux réellement neutres.",
            "MISSING_INFORMATION_COVERAGE",
            "CoverageRate|UnknownRate",
        )

    return proposals


def main() -> int:
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    analyst_rows = read_csv(ANALYST_FILE)
    if not analyst_rows:
        raise RuntimeError(f"Aucune observation Analyste disponible: {ANALYST_FILE}")

    raw: list[dict[str, str]] = []
    for row in analyst_rows:
        raw.extend(propose(row))

    # Déduplication par hypothèse/variable. Un même mécanisme observé sur plusieurs
    # tickers reste une hypothèse unique, avec CurrentSample égal au nombre de cas.
    groups: dict[tuple[str, str], list[dict[str, str]]] = {}
    for item in raw:
        key = (item["TriggerType"], item["VariableUnderTest"])
        groups.setdefault(key, []).append(item)

    output: list[dict[str, Any]] = []
    for index, ((trigger, variable), items) in enumerate(sorted(groups.items()), 1):
        sample = len(items)
        tickers = ",".join(sorted({item["Ticker"] for item in items}))
        missing = "|".join(sorted(set().union(*(tokens(item["MissingInformation"]) for item in items))))
        risks = "|".join(sorted(set().union(*(tokens(item["RiskFlags"]) for item in items))))
        if sample >= MIN_SAMPLE_FOR_PROMOTION_REVIEW:
            status = "PROMOTION_REVIEW_ELIGIBLE"
        elif sample >= MIN_SAMPLE_FOR_TEST:
            status = "TESTABLE"
        else:
            status = "MORE_DATA"
        output.append({
            "FactoryVersion": VERSION,
            "GeneratedAtUTC": generated,
            "HypothesisID": f"CFV1-{index:03d}-{slug(variable)}",
            "Ticker": tickers,
            "TriggerType": trigger,
            "TriggerEvidence": "|".join(sorted({item["TriggerEvidence"] for item in items})),
            "Hypothesis": items[0]["Hypothesis"],
            "VariableUnderTest": variable,
            "ChampionRuleChanged": False,
            "ExperimentMode": "SHADOW_OOS",
            "RequiredSample": MIN_SAMPLE_FOR_TEST,
            "CurrentSample": sample,
            "ExperimentStatus": status,
            "EvaluationMetric": items[0]["EvaluationMetric"],
            "RiskFlags": risks,
            "MissingInformation": missing,
            "AnalystAssessment": "MULTI" if len({i['AnalystAssessment'] for i in items}) > 1 else items[0]["AnalystAssessment"],
            "AnalystConfidence": "MULTI" if len({i['AnalystConfidence'] for i in items}) > 1 else items[0]["AnalystConfidence"],
            "NeedsReview": True,
            "ObservationOnly": OBSERVATION_ONLY,
            "V1ExecutionChanged": V1_EXECUTION_CHANGED,
            "PortfolioV1Changed": PORTFOLIO_V1_CHANGED,
        })

    with OUT.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(output)

    counts: dict[str, int] = {}
    for row in output:
        counts[row["ExperimentStatus"]] = counts.get(row["ExperimentStatus"], 0) + 1
    SUMMARY.write_text(json.dumps({
        "GeneratedAtUTC": generated,
        "FactoryVersion": VERSION,
        "AnalystRowsRead": len(analyst_rows),
        "HypothesesGenerated": len(output),
        "StatusCounts": counts,
        "MinimumSampleForTest": MIN_SAMPLE_FOR_TEST,
        "MinimumSampleForPromotionReview": MIN_SAMPLE_FOR_PROMOTION_REVIEW,
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    print("CHALLENGER FACTORY V1")
    print("Analyst rows read :", len(analyst_rows))
    print("Hypotheses generated :", len(output))
    for row in output:
        print(row["HypothesisID"], "|", row["TriggerType"], "| sample=", row["CurrentSample"], "|", row["ExperimentStatus"])
    print("ObservationOnly :", OBSERVATION_ONLY)
    print("V1ExecutionChanged :", V1_EXECUTION_CHANGED)
    print("PortfolioV1Changed :", PORTFOLIO_V1_CHANGED)
    print("Results :", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
