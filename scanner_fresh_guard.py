#!/usr/bin/env python3
"""Exécute le scanner V1 existant et contrôle la fraîcheur de ses résultats.

Ce script ne modifie aucune règle de score ou de sélection V1.
Il exécute scanner.py, puis vérifie que tous les tickers de stocks.txt
possèdent une ligne correspondant à la séance attendue.

Le résultat est écrit dans scanner_freshness_status.json.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
SCANNER = ROOT / "scanner.py"
STOCKS_FILE = ROOT / "stocks.txt"
HISTORY_FILE = ROOT / "signals_history.csv"
STATUS_FILE = ROOT / "scanner_freshness_status.json"

PARIS = ZoneInfo("Europe/Paris")
NOW_PARIS = datetime.now(PARIS)


def load_tickers() -> list[str]:
    return [
        line.strip()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_latest_dates() -> dict[str, str]:
    latest: dict[str, str] = {}

    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return latest

    with HISTORY_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        for row in csv.DictReader(handle):
            ticker = str(row.get("ticker", "")).strip()
            session_date = str(row.get("date", "")).strip()

            if not ticker or not session_date:
                continue

            if ticker not in latest or session_date > latest[ticker]:
                latest[ticker] = session_date

    return latest


def write_status(payload: dict[str, object]) -> None:
    STATUS_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def expected_session_date(now_paris: datetime) -> date:
    candidate = now_paris.date()

    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)

    return candidate

def main() -> int:
    expected_date = expected_session_date(NOW_PARIS).isoformat()
    tickers = load_tickers()

    if not tickers:
        payload = {
            "status": "CONFIG_ERROR",
            "checked_at_paris": NOW_PARIS.isoformat(timespec="seconds"),
            "expected_session_date": expected_date,
            "reason": "stocks.txt ne contient aucun ticker",
            "fresh_tickers": [],
            "stale_tickers": [],
            "missing_tickers": [],
            "scanner_return_code": None,
        }
        write_status(payload)
        print("SCANNER FRESHNESS : CONFIG_ERROR")
        print("stocks.txt ne contient aucun ticker")
        return 3

    scanner_result = subprocess.run(
        [sys.executable, str(SCANNER)],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    if scanner_result.stdout:
        print(scanner_result.stdout, end="")

    if scanner_result.stderr:
        print(scanner_result.stderr, file=sys.stderr, end="")

    if scanner_result.returncode != 0:
        payload = {
            "status": "SCANNER_ERROR",
            "checked_at_paris": NOW_PARIS.isoformat(timespec="seconds"),
            "expected_session_date": expected_date,
            "reason": "scanner.py a retourné une erreur",
            "fresh_tickers": [],
            "stale_tickers": [],
            "missing_tickers": [],
            "scanner_return_code": scanner_result.returncode,
        }
        write_status(payload)
        print("SCANNER FRESHNESS : SCANNER_ERROR")
        return scanner_result.returncode

    latest_dates = load_latest_dates()

    missing_tickers = [
        ticker
        for ticker in tickers
        if ticker not in latest_dates
    ]

    stale_tickers = [
        {
            "ticker": ticker,
            "latest_date": latest_dates[ticker],
        }
        for ticker in tickers
        if ticker in latest_dates
        and latest_dates[ticker] != expected_date
    ]

    fresh_tickers = [
        ticker
        for ticker in tickers
        if latest_dates.get(ticker) == expected_date
    ]

    if missing_tickers or stale_tickers:
        status = "STALE_DATA"
        reason = (
            "La totalité de l'univers Champion ne possède pas encore "
            "la clôture attendue."
        )
        return_code = 2
    else:
        status = "FRESH"
        reason = (
            "Tous les tickers de l'univers Champion possèdent "
            "la clôture attendue."
        )
        return_code = 0

    payload = {
        "status": status,
        "checked_at_paris": NOW_PARIS.isoformat(timespec="seconds"),
        "expected_session_date": expected_date,
        "reason": reason,
        "ticker_count_expected": len(tickers),
        "ticker_count_fresh": len(fresh_tickers),
        "fresh_tickers": fresh_tickers,
        "stale_tickers": stale_tickers,
        "missing_tickers": missing_tickers,
        "scanner_return_code": scanner_result.returncode,
        "v1_rules_changed": False,
    }

    write_status(payload)

    print()
    print("SCANNER FRESHNESS :", status)
    print("Date attendue :", expected_date)
    print(
        "Tickers frais :",
        f"{len(fresh_tickers)}/{len(tickers)}",
    )

    if stale_tickers:
        print("Tickers périmés :")
        for item in stale_tickers:
            print(
                "-",
                item["ticker"],
                "| dernière date :",
                item["latest_date"],
            )

    if missing_tickers:
        print("Tickers absents :", ", ".join(missing_tickers))

    print("V1RulesChanged : False")

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
