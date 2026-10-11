#!/usr/bin/env python3
"""V2 Shadow RVOL pour le moteur reel.

Objectif
--------
Observer la regle V2 sans modifier la decision V1, le portefeuille, les ordres,
les stops, les objectifs ou les alertes existantes.

Regle V2 figee
--------------
RVOL_J1 < 0.60 -> V2 aurait rejete le trade.
RVOL_J1 >= 0.60 -> V2 aurait conserve le trade.
RVOL absent      -> observation impossible, V1 reste inchangee.

Utilisation Python
------------------
from v2_shadow_rvol import evaluate_and_log_shadow

shadow = evaluate_and_log_shadow(
    ticker=ticker,
    session_date=session_date,
    v1_decision=decision,
    v1_reason=reason,
    decision_time=decision_time,
)

Utilisation CLI
---------------
python historical_lab/scripts/v2_shadow_rvol.py \
  --ticker AAPL \
  --session-date 2026-10-12 \
  --v1-decision "ACHAT CONFIRME" \
  --v1-reason "OR High + VWAP + volume" \
  --decision-time "2026-10-12T16:10:00+02:00"
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INDICATORS_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
DEFAULT_SHADOW_LOG = BASE_DIR / "results" / "v2_shadow_rvol_log.csv"
DEFAULT_SHADOW_STATE = BASE_DIR / "results" / "v2_shadow_rvol_latest.json"

RVOL_THRESHOLD = 0.60
SHADOW_VERSION = "V2_SHADOW_RVOL_060"

LOG_COLUMNS = [
    "ShadowVersion",
    "EvaluatedAtUTC",
    "Ticker",
    "SessionDate",
    "SignalDate",
    "DecisionTime",
    "V1Decision",
    "V1Reason",
    "RVOL_J1",
    "RVOLThreshold",
    "V2ShadowAction",
    "V2ShadowReason",
    "WouldV2Reject",
    "V1ExecutionChanged",
    "DataStatus",
]


def _normalise_date(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"Date invalide: {value}")
    return ts.strftime("%Y-%m-%d")


def _load_indicators(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Fichier indicateurs introuvable: {path}")

    df = pd.read_csv(path)
    required = {"Date", "Ticker", "RVOL"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            "Colonnes manquantes dans historical_indicators.csv: "
            + ", ".join(sorted(missing))
        )

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df["Ticker"] = df["Ticker"].astype(str)
    df["RVOL"] = pd.to_numeric(df["RVOL"], errors="coerce")

    return (
        df.dropna(subset=["Date", "Ticker"])
        .sort_values(["Ticker", "Date"])
        .drop_duplicates(["Ticker", "Date"], keep="last")
        .reset_index(drop=True)
    )


def find_j1_rvol(
    ticker: str,
    session_date: Any,
    indicators_file: Path | str = DEFAULT_INDICATORS_FILE,
) -> Dict[str, Any]:
    """Retourne la derniere observation disponible strictement avant J."""

    ticker = str(ticker).strip()
    session_date_str = _normalise_date(session_date)
    session_ts = pd.Timestamp(session_date_str)
    path = Path(indicators_file)

    df = _load_indicators(path)
    history = df.loc[
        (df["Ticker"] == ticker)
        & (df["Date"] < session_ts)
    ].copy()

    if history.empty:
        return {
            "SignalDate": None,
            "RVOL_J1": np.nan,
            "DataStatus": "J1_NOT_FOUND",
        }

    row = history.iloc[-1]
    rvol = row["RVOL"]

    return {
        "SignalDate": row["Date"].strftime("%Y-%m-%d"),
        "RVOL_J1": float(rvol) if not pd.isna(rvol) else np.nan,
        "DataStatus": "OK" if not pd.isna(rvol) else "RVOL_J1_MISSING",
    }


def evaluate_shadow(
    ticker: str,
    session_date: Any,
    v1_decision: str,
    v1_reason: str = "",
    decision_time: Optional[Any] = None,
    indicators_file: Path | str = DEFAULT_INDICATORS_FILE,
) -> Dict[str, Any]:
    """Evalue V2 en observation uniquement. Ne modifie jamais V1."""

    session_date_str = _normalise_date(session_date)
    j1 = find_j1_rvol(ticker, session_date_str, indicators_file)
    rvol = j1["RVOL_J1"]

    if j1["DataStatus"] != "OK":
        action = "NON_EVALUABLE"
        reason = j1["DataStatus"]
        would_reject = False
    elif rvol < RVOL_THRESHOLD:
        action = "AURAIT_REJETE"
        reason = f"RVOL_J1={rvol:.6f} < {RVOL_THRESHOLD:.2f}"
        would_reject = True
    else:
        action = "AURAIT_CONSERVE"
        reason = f"RVOL_J1={rvol:.6f} >= {RVOL_THRESHOLD:.2f}"
        would_reject = False

    evaluated_at = pd.Timestamp.now(tz="UTC").isoformat()
    decision_time_text = "" if decision_time is None else str(decision_time)

    return {
        "ShadowVersion": SHADOW_VERSION,
        "EvaluatedAtUTC": evaluated_at,
        "Ticker": str(ticker).strip(),
        "SessionDate": session_date_str,
        "SignalDate": j1["SignalDate"],
        "DecisionTime": decision_time_text,
        "V1Decision": str(v1_decision),
        "V1Reason": str(v1_reason),
        "RVOL_J1": rvol,
        "RVOLThreshold": RVOL_THRESHOLD,
        "V2ShadowAction": action,
        "V2ShadowReason": reason,
        "WouldV2Reject": bool(would_reject),
        "V1ExecutionChanged": False,
        "DataStatus": j1["DataStatus"],
    }


def append_shadow_log(
    record: Dict[str, Any],
    log_file: Path | str = DEFAULT_SHADOW_LOG,
    latest_file: Path | str = DEFAULT_SHADOW_STATE,
) -> None:
    """Journalise de facon idempotente ticker/session/decision/version."""

    log_path = Path(log_file)
    latest_path = Path(latest_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)

    new = pd.DataFrame([{column: record.get(column) for column in LOG_COLUMNS}])

    if log_path.exists() and log_path.stat().st_size > 0:
        old = pd.read_csv(log_path)
        combined = pd.concat([old, new], ignore_index=True)
    else:
        combined = new

    dedupe_keys = [
        "ShadowVersion",
        "Ticker",
        "SessionDate",
        "DecisionTime",
        "V1Decision",
    ]
    combined = combined.drop_duplicates(dedupe_keys, keep="last")
    combined = combined.sort_values(
        ["SessionDate", "DecisionTime", "Ticker"],
        na_position="last",
    )
    combined.to_csv(log_path, index=False, encoding="utf-8")

    serialisable = dict(record)
    if pd.isna(serialisable.get("RVOL_J1", np.nan)):
        serialisable["RVOL_J1"] = None
    latest_path.write_text(
        json.dumps(serialisable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def evaluate_and_log_shadow(
    ticker: str,
    session_date: Any,
    v1_decision: str,
    v1_reason: str = "",
    decision_time: Optional[Any] = None,
    indicators_file: Path | str = DEFAULT_INDICATORS_FILE,
    log_file: Path | str = DEFAULT_SHADOW_LOG,
    latest_file: Path | str = DEFAULT_SHADOW_STATE,
) -> Dict[str, Any]:
    record = evaluate_shadow(
        ticker=ticker,
        session_date=session_date,
        v1_decision=v1_decision,
        v1_reason=v1_reason,
        decision_time=decision_time,
        indicators_file=indicators_file,
    )
    append_shadow_log(record, log_file, latest_file)
    return record


def format_shadow_message(record: Dict[str, Any]) -> str:
    rvol = record.get("RVOL_J1")
    rvol_text = "indisponible" if rvol is None or pd.isna(rvol) else f"{rvol:.3f}"
    return (
        f"V2 SHADOW | {record['Ticker']} | {record['V2ShadowAction']}\n"
        f"V1: {record['V1Decision']} (execution inchangee)\n"
        f"RVOL J-1: {rvol_text} | seuil: {record['RVOLThreshold']:.2f}\n"
        f"Raison: {record['V2ShadowReason']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluation V2 shadow RVOL")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--session-date", required=True)
    parser.add_argument("--v1-decision", required=True)
    parser.add_argument("--v1-reason", default="")
    parser.add_argument("--decision-time", default="")
    parser.add_argument("--indicators-file", default=str(DEFAULT_INDICATORS_FILE))
    parser.add_argument("--log-file", default=str(DEFAULT_SHADOW_LOG))
    parser.add_argument("--latest-file", default=str(DEFAULT_SHADOW_STATE))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    record = evaluate_and_log_shadow(
        ticker=args.ticker,
        session_date=args.session_date,
        v1_decision=args.v1_decision,
        v1_reason=args.v1_reason,
        decision_time=args.decision_time,
        indicators_file=args.indicators_file,
        log_file=args.log_file,
        latest_file=args.latest_file,
    )
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    print()
    print(format_shadow_message(record))
    print()
    print("V1ExecutionChanged = False")


if __name__ == "__main__":
    main()
