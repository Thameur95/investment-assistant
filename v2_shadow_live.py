#!/usr/bin/env python3
"""Integration live du Shadow V2 dans le moteur reel.

Le script lit intraday_decisions.csv APRES l'execution du moniteur reel,
evalue uniquement les decisions ACHAT CONFIRME / ACHAT REACTIVE, puis ecrit
le journal Shadow. Il ne modifie jamais intraday_decisions.csv, le portefeuille,
les ordres ou la decision V1.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent
HISTORICAL_SCRIPTS = ROOT_DIR / "historical_lab" / "scripts"
if str(HISTORICAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(HISTORICAL_SCRIPTS))

from v2_shadow_rvol import evaluate_and_log_shadow, format_shadow_message

ACTIONABLE = {"ACHAT CONFIRME", "ACHAT REACTIVE"}


def parse_args():
    parser = argparse.ArgumentParser(description="V2 shadow live")
    parser.add_argument("--decisions", default="intraday_decisions.csv")
    parser.add_argument(
        "--indicators",
        default="historical_lab/data/processed/historical_indicators.csv",
    )
    parser.add_argument(
        "--log",
        default="historical_lab/results/v2_shadow_rvol_log.csv",
    )
    parser.add_argument(
        "--latest",
        default="historical_lab/results/v2_shadow_rvol_latest.json",
    )
    parser.add_argument(
        "--summary",
        default="historical_lab/results/v2_shadow_live_summary.json",
    )
    return parser.parse_args()


def first_value(row, names, default=""):
    for name in names:
        if name in row.index:
            value = row.get(name)
            if pd.notna(value) and str(value).strip() != "":
                return value
    return default


def main():
    args = parse_args()
    decisions_path = Path(args.decisions)
    indicators_path = Path(args.indicators)
    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    if not decisions_path.exists():
        summary = {
            "Status": "NO_DECISIONS_FILE",
            "Evaluated": 0,
            "WouldReject": 0,
            "WouldKeep": 0,
            "NonEvaluable": 0,
            "V1ExecutionChanged": False,
        }
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print("V2 SHADOW LIVE: intraday_decisions.csv absent, aucune evaluation.")
        print("V1ExecutionChanged = False")
        return

    if not indicators_path.exists():
        raise FileNotFoundError(
            f"Indicateurs introuvables pour le Shadow V2: {indicators_path}"
        )

    decisions = pd.read_csv(decisions_path)
    required = {"ticker", "decision"}
    missing = required - set(decisions.columns)
    if missing:
        raise ValueError(
            "Colonnes manquantes dans intraday_decisions.csv: "
            + ", ".join(sorted(missing))
        )

    actionable = decisions.loc[
        decisions["decision"].astype(str).isin(ACTIONABLE)
    ].copy()

    records = []
    for _, row in actionable.iterrows():
        ticker = str(row.get("ticker", "")).strip()
        session_date = first_value(
            row,
            ["session_date", "date", "Date_Paris"],
            default="",
        )
        decision_time = first_value(
            row,
            ["decision_time", "datetime", "Datetime_Paris"],
            default="",
        )
        reason = first_value(
            row,
            ["reason", "motif", "decision_reason"],
            default="",
        )

        if not ticker or not session_date:
            print(
                "V2 SHADOW LIVE: ligne ignoree, ticker ou session_date absent."
            )
            continue

        record = evaluate_and_log_shadow(
            ticker=ticker,
            session_date=session_date,
            v1_decision=str(row["decision"]),
            v1_reason=str(reason),
            decision_time=str(decision_time),
            indicators_file=indicators_path,
            log_file=Path(args.log),
            latest_file=Path(args.latest),
        )
        records.append(record)
        print(format_shadow_message(record))
        print("-" * 80)

    summary = {
        "Status": "OK",
        "Evaluated": len(records),
        "WouldReject": sum(r["V2ShadowAction"] == "AURAIT_REJETE" for r in records),
        "WouldKeep": sum(r["V2ShadowAction"] == "AURAIT_CONSERVE" for r in records),
        "NonEvaluable": sum(r["V2ShadowAction"] == "NON_EVALUABLE" for r in records),
        "V1ExecutionChanged": False,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("V2 SHADOW LIVE SUMMARY")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("V1ExecutionChanged = False")


if __name__ == "__main__":
    main()
