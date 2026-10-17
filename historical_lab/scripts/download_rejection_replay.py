#!/usr/bin/env python3
"""
download_rejection_replay.py

Télécharge et archive les bougies 5 minutes pour les rejets V1 ciblés,
de la séance du rejet J jusqu'à J+4 séances boursières.

OBSERVATION UNIQUEMENT :
- aucun impact sur V1 ;
- aucun impact sur V2 Shadow ;
- aucun impact sur le portefeuille ;
- aucun fichier de intraday_v1_frozen n'est modifié.

Sorties :
  historical_lab/data/rejection_replay/
    VLO_2026-10-06_J0_J4_5m.csv
    VLO_2026-10-12_J0_J4_5m.csv
    AAPL_2026-10-09_J0_J4_5m.csv
    TSLA_2026-10-06_J0_J4_5m.csv
    rejection_replay_download_summary.csv
"""

from pathlib import Path
from typing import Dict, List

import pandas as pd
import yfinance as yf


print("=" * 88)
print("REJECTION REPLAY DATASET - J À J+4")
print("DOWNLOAD INTRADAY 5 MINUTES")
print("MODE OBSERVATION UNIQUEMENT - AUCUN IMPACT V1")
print("=" * 88)

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "data" / "rejection_replay"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Un événement par rejet. VLO possède deux événements distincts.
EVENTS = [
    {
        "event_id": "VLO_2026-10-06",
        "ticker": "VLO",
        "rejection_date": "2026-10-06",
        "decision_time_new_york": "10:25:00",
        "reason": "Aucune confirmation pendant une heure",
        "role": "SUSPECT",
    },
    {
        "event_id": "VLO_2026-10-12",
        "ticker": "VLO",
        "rejection_date": "2026-10-12",
        "decision_time_new_york": "10:20:00",
        "reason": "Cassure baissiere Opening Range",
        "role": "SUSPECT",
    },
    {
        "event_id": "AAPL_2026-10-09",
        "ticker": "AAPL",
        "rejection_date": "2026-10-09",
        "decision_time_new_york": "10:25:00",
        "reason": "Aucune confirmation pendant une heure",
        "role": "SUSPECT",
    },
    {
        "event_id": "TSLA_2026-10-06",
        "ticker": "TSLA",
        "rejection_date": "2026-10-06",
        "decision_time_new_york": "10:25:00",
        "reason": "Aucune confirmation pendant une heure",
        "role": "CONTROLE",
    },
]

DOWNLOAD_PERIOD = "60d"
INTERVAL = "5m"
SESSIONS_TO_KEEP = 5  # J, J+1, J+2, J+3, J+4


def normalize_yfinance(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Normalise la sortie yfinance et ajoute les fuseaux utiles."""
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    data = data.reset_index()
    datetime_column = data.columns[0]
    data = data.rename(columns={datetime_column: "Datetime"})

    data["Datetime"] = pd.to_datetime(data["Datetime"], utc=True, errors="coerce")
    data = data.dropna(subset=["Datetime"]).copy()

    data["Datetime_NewYork"] = data["Datetime"].dt.tz_convert("America/New_York")
    data["Date_NewYork"] = data["Datetime_NewYork"].dt.strftime("%Y-%m-%d")
    data["Time_NewYork"] = data["Datetime_NewYork"].dt.strftime("%H:%M:%S")

    data["Datetime_Paris"] = data["Datetime"].dt.tz_convert("Europe/Paris")
    data["Date_Paris"] = data["Datetime_Paris"].dt.strftime("%Y-%m-%d")
    data["Time_Paris"] = data["Datetime_Paris"].dt.strftime("%H:%M:%S")
    data["Ticker"] = ticker

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in required if column not in data.columns]
    if missing:
        raise ValueError(f"Colonnes OHLCV manquantes pour {ticker}: {', '.join(missing)}")

    return data


def choose_session_window(data: pd.DataFrame, rejection_date: str) -> List[str]:
    """Sélectionne J puis les quatre séances NY suivantes réellement présentes."""
    available_dates = sorted(data["Date_NewYork"].dropna().unique().tolist())

    if rejection_date not in available_dates:
        return []

    start_index = available_dates.index(rejection_date)
    return available_dates[start_index : start_index + SESSIONS_TO_KEEP]


# Un seul téléchargement par ticker, puis découpage par événement.
ticker_cache: Dict[str, pd.DataFrame] = {}
summary_rows = []
complete_events = 0
failed_events = 0

for event in EVENTS:
    event_id = event["event_id"]
    ticker = event["ticker"]
    rejection_date = event["rejection_date"]

    print()
    print("-" * 88)
    print(f"Événement : {event_id}")
    print(f"Ticker    : {ticker}")
    print(f"Rejet     : {rejection_date} {event['decision_time_new_york']} America/New_York")
    print(f"Rôle      : {event['role']}")

    try:
        if ticker not in ticker_cache:
            print(f"Téléchargement yfinance : {ticker}")
            downloaded = yf.download(
                ticker,
                period=DOWNLOAD_PERIOD,
                interval=INTERVAL,
                auto_adjust=True,
                prepost=False,
                progress=False,
                threads=False,
            )

            if downloaded.empty:
                raise RuntimeError(f"Aucune donnée 5 minutes reçue pour {ticker}")

            ticker_cache[ticker] = normalize_yfinance(downloaded, ticker)
            print(f"Bougies téléchargées : {len(ticker_cache[ticker])}")
        else:
            print(f"Réutilisation du téléchargement déjà chargé pour {ticker}")

        data = ticker_cache[ticker]
        session_dates = choose_session_window(data, rejection_date)

        if not session_dates:
            raise RuntimeError(f"Séance de rejet absente : {rejection_date}")

        selected = data[data["Date_NewYork"].isin(session_dates)].copy()
        selected = selected.sort_values("Datetime")

        # Numéro de séance relatif : 0=J, 1=J+1, ... 4=J+4.
        session_number = {date_value: index for index, date_value in enumerate(session_dates)}
        selected["SessionOffset"] = selected["Date_NewYork"].map(session_number)
        selected["SessionLabel"] = selected["SessionOffset"].map(
            lambda value: "J" if value == 0 else f"J+{int(value)}"
        )

        selected["EventID"] = event_id
        selected["RejectionDate"] = rejection_date
        selected["DecisionTime_NewYork"] = event["decision_time_new_york"]
        selected["RejectionReason"] = event["reason"]
        selected["EventRole"] = event["role"]

        selected = selected[
            [
                "EventID",
                "EventRole",
                "Ticker",
                "RejectionDate",
                "DecisionTime_NewYork",
                "RejectionReason",
                "SessionOffset",
                "SessionLabel",
                "Datetime",
                "Datetime_NewYork",
                "Date_NewYork",
                "Time_NewYork",
                "Datetime_Paris",
                "Date_Paris",
                "Time_Paris",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
            ]
        ]

        output_file = OUTPUT_DIR / f"{event_id}_J0_J4_5m.csv"
        selected.to_csv(output_file, index=False, encoding="utf-8")

        counts = selected.groupby("Date_NewYork").size().to_dict()
        incomplete_sessions = [
            date_value for date_value in session_dates if counts.get(date_value, 0) != 78
        ]

        enough_sessions = len(session_dates) == SESSIONS_TO_KEEP
        status = "OK"
        if not enough_sessions:
            status = "PARTIAL_WINDOW"
        elif incomplete_sessions:
            status = "INCOMPLETE_SESSIONS"

        if status == "OK":
            complete_events += 1
            print("Statut     : OK")
        else:
            failed_events += 1
            print(f"Statut     : {status}")

        print(f"Séances    : {' | '.join(session_dates)}")
        print(f"Bougies    : {len(selected)} (attendu si complet : {SESSIONS_TO_KEEP * 78})")
        print(f"Fichier    : {output_file}")

        summary_rows.append(
            {
                "EventID": event_id,
                "EventRole": event["role"],
                "Ticker": ticker,
                "RejectionDate": rejection_date,
                "DecisionTime_NewYork": event["decision_time_new_york"],
                "RejectionReason": event["reason"],
                "SelectedSessionCount": len(session_dates),
                "ExpectedSessionCount": SESSIONS_TO_KEEP,
                "SelectedBars": len(selected),
                "ExpectedBars": SESSIONS_TO_KEEP * 78,
                "SessionDates": "|".join(session_dates),
                "IncompleteSessions": "|".join(incomplete_sessions),
                "OutputFile": str(output_file),
                "Status": status,
            }
        )

    except Exception as error:
        failed_events += 1
        print(f"ERREUR : {error}")
        summary_rows.append(
            {
                "EventID": event_id,
                "EventRole": event["role"],
                "Ticker": ticker,
                "RejectionDate": rejection_date,
                "DecisionTime_NewYork": event["decision_time_new_york"],
                "RejectionReason": event["reason"],
                "SelectedSessionCount": 0,
                "ExpectedSessionCount": SESSIONS_TO_KEEP,
                "SelectedBars": 0,
                "ExpectedBars": SESSIONS_TO_KEEP * 78,
                "SessionDates": "",
                "IncompleteSessions": "",
                "OutputFile": "",
                "Status": f"ERROR: {error}",
            }
        )

summary_columns = [
    "EventID",
    "EventRole",
    "Ticker",
    "RejectionDate",
    "DecisionTime_NewYork",
    "RejectionReason",
    "SelectedSessionCount",
    "ExpectedSessionCount",
    "SelectedBars",
    "ExpectedBars",
    "SessionDates",
    "IncompleteSessions",
    "OutputFile",
    "Status",
]

summary = pd.DataFrame(summary_rows, columns=summary_columns)
summary_file = OUTPUT_DIR / "rejection_replay_download_summary.csv"
summary.to_csv(summary_file, index=False, encoding="utf-8")

print()
print("=" * 88)
print("RÉSUMÉ")
print("=" * 88)
print(f"Événements complets : {complete_events}")
print(f"Événements incomplets/échoués : {failed_events}")
print(f"Résumé : {summary_file}")
print()
print("DOSSIER V1 FIGÉ : NON MODIFIÉ")
print("PORTEFEUILLE     : NON MODIFIÉ")
print("V2 SHADOW        : NON MODIFIÉ")
print("V1ExecutionChanged : False")
print("=" * 88)

if failed_events > 0:
    print("ERREUR : au moins un événement ne possède pas une fenêtre 5 minutes complète J à J+4.")
    raise SystemExit(1)

print("TÉLÉCHARGEMENT J À J+4 TERMINÉ AVEC SUCCÈS")
raise SystemExit(0)
