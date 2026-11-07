
#!/usr/bin/env python3
"""Integration live du Shadow V2 dans le moteur reel.

Le script lit intraday_decisions.csv APRES l'execution du moniteur reel,
evalue uniquement les decisions d'achat V1 non encore traitees, puis ecrit
le journal Shadow.

Il ne modifie jamais intraday_decisions.csv, le portefeuille, les ordres
ou la decision V1.
"""
from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parent
HISTORICAL_SCRIPTS = ROOT_DIR / "historical_lab" / "scripts"

if str(HISTORICAL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(HISTORICAL_SCRIPTS))

from v2_shadow_rvol import evaluate_and_log_shadow, format_shadow_message


ACTIONABLE_NORMALIZED = {
    "ACHAT CONFIRME",
    "ACHAT REACTIVE",
    "ACHAT REACTIF",
}


def normalize_label(value) -> str:
    text = "" if value is None else str(value).strip().upper()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )
    return " ".join(text.split())


def is_actionable(value) -> bool:
    return normalize_label(value) in ACTIONABLE_NORMALIZED


def parse_args():
    parser = argparse.ArgumentParser(description="V2 shadow live")

    parser.add_argument(
        "--decisions",
        default="intraday_decisions.csv",
    )

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

            if (
                pd.notna(value)
                and str(value).strip() != ""
            ):
                return value

    return default


def event_key(
    ticker,
    session_date,
    decision_time,
    v1_decision,
) -> tuple[str, str, str, str]:
    """Cle fonctionnelle d'une decision V1 actionable."""

    return (
        str(ticker).strip(),
        str(session_date).strip(),
        str(decision_time).strip(),
        normalize_label(v1_decision),
    )


def load_already_evaluated(
    log_path: Path,
) -> set[tuple[str, str, str, str]]:
    """Charge les decisions V1 deja presentes dans le journal V2.

    Le journal historique reste la source de verite.
    Aucune observation existante n'est reecrite par cette fonction.
    """

    if (
        not log_path.exists()
        or log_path.stat().st_size == 0
    ):
        return set()

    log = pd.read_csv(log_path)

    required = {
        "Ticker",
        "SessionDate",
        "DecisionTime",
        "V1Decision",
    }

    missing = required - set(log.columns)

    if missing:
        raise ValueError(
            "Colonnes manquantes dans le journal V2: "
            + ", ".join(sorted(missing))
        )

    evaluated = set()

    for _, row in log.iterrows():
        ticker = row.get("Ticker", "")
        session_date = row.get("SessionDate", "")
        decision_time = row.get("DecisionTime", "")
        decision = row.get("V1Decision", "")

        if pd.isna(ticker):
            ticker = ""

        if pd.isna(session_date):
            session_date = ""

        if pd.isna(decision_time):
            decision_time = ""

        if pd.isna(decision):
            decision = ""

        key = event_key(
            ticker=ticker,
            session_date=session_date,
            decision_time=decision_time,
            v1_decision=decision,
        )

        evaluated.add(key)

    return evaluated


def write_summary(
    summary_path: Path,
    status: str,
    records,
    skipped: int,
) -> None:
    summary = {
        "Status": status,
        "Evaluated": len(records),
        "AlreadyEvaluatedSkipped": skipped,
        "WouldReject": sum(
            r["V2ShadowAction"] == "AURAIT_REJETE"
            for r in records
        ),
        "WouldKeep": sum(
            r["V2ShadowAction"] == "AURAIT_CONSERVE"
            for r in records
        ),
        "NonEvaluable": sum(
            r["V2ShadowAction"] == "NON_EVALUABLE"
            for r in records
        ),
        "V1ExecutionChanged": False,
    }

    summary_path.write_text(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("V2 SHADOW LIVE SUMMARY")
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            indent=2,
        )
    )
    print("V1ExecutionChanged = False")


def main():
    args = parse_args()

    decisions_path = Path(args.decisions)
    indicators_path = Path(args.indicators)
    log_path = Path(args.log)
    latest_path = Path(args.latest)
    summary_path = Path(args.summary)

    summary_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not decisions_path.exists():
        summary = {
            "Status": "NO_DECISIONS_FILE",
            "Evaluated": 0,
            "AlreadyEvaluatedSkipped": 0,
            "WouldReject": 0,
            "WouldKeep": 0,
            "NonEvaluable": 0,
            "V1ExecutionChanged": False,
        }

        summary_path.write_text(
            json.dumps(
                summary,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            "V2 SHADOW LIVE: "
            "intraday_decisions.csv absent, aucune evaluation."
        )
        print("V1ExecutionChanged = False")
        return

    if not indicators_path.exists():
        raise FileNotFoundError(
            "Indicateurs introuvables pour le Shadow V2: "
            f"{indicators_path}"
        )

    decisions = pd.read_csv(decisions_path)

    required = {
        "ticker",
        "decision",
    }

    missing = required - set(decisions.columns)

    if missing:
        raise ValueError(
            "Colonnes manquantes dans "
            "intraday_decisions.csv: "
            + ", ".join(sorted(missing))
        )

    actionable_mask = decisions["decision"].map(
        is_actionable
    )

    actionable = decisions.loc[
        actionable_mask
    ].copy()

    print(
        "V2 SHADOW LIVE: decisions lues =",
        len(decisions),
    )

    print(
        "V2 SHADOW LIVE: decisions achat evaluables =",
        len(actionable),
    )

    already_evaluated = load_already_evaluated(
        log_path
    )

    print(
        "V2 SHADOW LIVE: evaluations deja journalisees =",
        len(already_evaluated),
    )

    records = []
    skipped = 0

    for _, row in actionable.iterrows():

        ticker = str(
            row.get("ticker", "")
        ).strip()

        session_date = first_value(
            row,
            [
                "session_date",
                "date",
                "Date_Paris",
            ],
            default="",
        )

        decision_time = first_value(
            row,
            [
                "decision_time",
                "datetime",
                "Datetime_Paris",
            ],
            default="",
        )

        reason = first_value(
            row,
            [
                "reason",
                "motif",
                "decision_reason",
            ],
            default="",
        )

        if not ticker or not session_date:
            print(
                "V2 SHADOW LIVE: ligne ignoree, "
                "ticker ou session_date absent."
            )
            continue

        key = event_key(
            ticker=ticker,
            session_date=session_date,
            decision_time=decision_time,
            v1_decision=row["decision"],
        )

        if key in already_evaluated:
            skipped += 1
            continue

        record = evaluate_and_log_shadow(
            ticker=ticker,
            session_date=session_date,
            v1_decision=str(row["decision"]),
            v1_reason=str(reason),
            decision_time=str(decision_time),
            indicators_file=indicators_path,
            log_file=log_path,
            latest_file=latest_path,
        )

        records.append(record)

        # Evite aussi un doublon si intraday_decisions.csv
        # contient deux fois la meme decision dans ce run.
        already_evaluated.add(key)

        print(
            format_shadow_message(record)
        )
        print("-" * 80)

    write_summary(
        summary_path=summary_path,
        status="OK",
        records=records,
        skipped=skipped,
    )


if __name__ == "__main__":
    main()
