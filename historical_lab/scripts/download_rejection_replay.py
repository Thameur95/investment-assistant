#!/usr/bin/env python3

"""
download_rejection_replay.py

Téléchargement ciblé des données intraday 5 minutes nécessaires
à l'analyse des rejets V1.

IMPORTANT :
- Mode observation uniquement.
- Aucun impact sur V1.
- Aucun impact sur V2 Shadow.
- Aucun impact sur le portefeuille virtuel.
- Aucun fichier du dataset intraday_v1_frozen n'est modifié.
- Les fichiers sont enregistrés dans un dossier indépendant :
  historical_lab/data/rejection_replay/
"""

from pathlib import Path

import pandas as pd
import yfinance as yf


print("=" * 80)
print("REJECTION REPLAY DATASET")
print("DOWNLOAD INTRADAY 5 MINUTES")
print("MODE OBSERVATION UNIQUEMENT - AUCUN IMPACT V1")
print("=" * 80)


# =============================================================================
# CONFIGURATION
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent.parent

OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "rejection_replay"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)


# Cas identifiés par l'analyse Daily.
#
# VLO 06/10 :
#   rejet suspect après absence de confirmation.
#
# VLO 12/10 :
#   rejet après cassure baissière de l'Opening Range,
#   puis forte reprise ultérieure.
#
# AAPL 09/10 :
#   rejet suspect après absence de confirmation.
#
# TSLA 06/10 :
#   cas de contrôle avec MFE légèrement supérieur à +2 %,
#   mais trajectoire ultérieure plus risquée.

TARGETS = {
    "VLO": [
        "2026-10-06",
        "2026-10-12",
    ],
    "AAPL": [
        "2026-10-09",
    ],
    "TSLA": [
        "2026-10-06",
    ],
}


DOWNLOAD_PERIOD = "60d"
INTERVAL = "5m"

success = 0
failed = 0

summary_rows = []


# =============================================================================
# TÉLÉCHARGEMENT
# =============================================================================

for ticker, target_dates in TARGETS.items():

    try:

        print()
        print("-" * 80)
        print(f"Téléchargement : {ticker}")
        print(
            "Dates recherchées : "
            + ", ".join(target_dates)
        )

        data = yf.download(
            ticker,
            period=DOWNLOAD_PERIOD,
            interval=INTERVAL,
            auto_adjust=True,
            prepost=False,
            progress=False,
            threads=False,
        )

        if data.empty:

            print(
                f"ERREUR : aucune donnée 5 minutes reçue pour {ticker}"
            )

            failed += 1

            summary_rows.append(
                {
                    "Ticker": ticker,
                    "TargetDates": "|".join(target_dates),
                    "DownloadedBars": 0,
                    "SelectedBars": 0,
                    "AvailableDates": "",
                    "MissingDates": "|".join(target_dates),
                    "OutputFile": "",
                    "Status": "NO_DATA",
                }
            )

            continue

        # yfinance peut retourner des colonnes MultiIndex.
        if isinstance(
            data.columns,
            pd.MultiIndex,
        ):

            data.columns = (
                data.columns
                .get_level_values(0)
            )

        data = data.reset_index()

        # La première colonne correspond normalement au Datetime.
        datetime_column = data.columns[0]

        data = data.rename(
            columns={
                datetime_column: "Datetime"
            }
        )

        # Conversion vers UTC pour obtenir une référence temporelle stable.
        data["Datetime"] = pd.to_datetime(
            data["Datetime"],
            utc=True,
            errors="coerce",
        )

        data = data.dropna(
            subset=["Datetime"]
        )

        # Heure du marché américain.
        data["Datetime_NewYork"] = (
            data["Datetime"]
            .dt.tz_convert("America/New_York")
        )

        data["Date_NewYork"] = (
            data["Datetime_NewYork"]
            .dt.strftime("%Y-%m-%d")
        )

        data["Time_NewYork"] = (
            data["Datetime_NewYork"]
            .dt.strftime("%H:%M:%S")
        )

        # Heure de Paris pour faciliter la lecture des rapports.
        data["Datetime_Paris"] = (
            data["Datetime"]
            .dt.tz_convert("Europe/Paris")
        )

        data["Date_Paris"] = (
            data["Datetime_Paris"]
            .dt.strftime("%Y-%m-%d")
        )

        data["Time_Paris"] = (
            data["Datetime_Paris"]
            .dt.strftime("%H:%M:%S")
        )

        data["Ticker"] = ticker

        available_dates = sorted(
            data["Date_NewYork"]
            .dropna()
            .unique()
            .tolist()
        )

        # Conservation uniquement des journées ciblées.
        selected = data[
            data["Date_NewYork"].isin(target_dates)
        ].copy()

        selected = selected[
            [
                "Ticker",
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

        selected = selected.sort_values(
            by="Datetime"
        )

        downloaded_bars = len(data)
        selected_bars = len(selected)

        output_file = (
            OUTPUT_DIR
            / f"{ticker}_rejection_replay_5m.csv"
        )

        selected.to_csv(
            output_file,
            index=False,
            encoding="utf-8",
        )

        selected_dates = set(
            selected["Date_NewYork"]
            .dropna()
            .unique()
            .tolist()
        )

        missing_dates = [
            target_date
            for target_date in target_dates
            if target_date not in selected_dates
        ]

        if selected.empty:

            status = "TARGET_DATES_NOT_FOUND"

            print(
                "ATTENTION : aucune bougie trouvée "
                f"pour les dates ciblées de {ticker}"
            )

            failed += 1

        elif missing_dates:

            status = "PARTIAL"

            print(
                "ATTENTION : dates manquantes : "
                + ", ".join(missing_dates)
            )

            failed += 1

        else:

            status = "OK"

            print(
                "OK : toutes les dates ciblées ont été trouvées."
            )

            success += 1

        print(
            f"{ticker} : "
            f"{downloaded_bars} bougies téléchargées"
        )

        print(
            f"{ticker} : "
            f"{selected_bars} bougies conservées"
        )

        print(
            f"Fichier : {output_file}"
        )

        summary_rows.append(
            {
                "Ticker": ticker,
                "TargetDates": "|".join(target_dates),
                "DownloadedBars": downloaded_bars,
                "SelectedBars": selected_bars,
                "AvailableDates": "|".join(available_dates),
                "MissingDates": "|".join(missing_dates),
                "OutputFile": str(output_file),
                "Status": status,
            }
        )

    except Exception as error:

        print(
            f"ERREUR {ticker} : {error}"
        )

        failed += 1

        summary_rows.append(
            {
                "Ticker": ticker,
                "TargetDates": "|".join(target_dates),
                "DownloadedBars": 0,
                "SelectedBars": 0,
                "AvailableDates": "",
                "MissingDates": "|".join(target_dates),
                "OutputFile": "",
                "Status": f"ERROR: {error}",
            }
        )


# =============================================================================
# RAPPORT DE TÉLÉCHARGEMENT
# =============================================================================

summary_columns = [
    "Ticker",
    "TargetDates",
    "DownloadedBars",
    "SelectedBars",
    "AvailableDates",
    "MissingDates",
    "OutputFile",
    "Status",
]

summary = pd.DataFrame(
    summary_rows,
    columns=summary_columns,
)

summary_file = (
    OUTPUT_DIR
    / "rejection_replay_download_summary.csv"
)

summary.to_csv(
    summary_file,
    index=False,
    encoding="utf-8",
)


# =============================================================================
# CONTRÔLES FINAUX
# =============================================================================

print()
print("=" * 80)
print("RÉSUMÉ")
print("=" * 80)

print(f"Tickers complets : {success}")
print(f"Tickers échoués  : {failed}")
print(f"Résumé           : {summary_file}")

print()
print("DOSSIER V1 FIGÉ : NON MODIFIÉ")
print("PORTEFEUILLE     : NON MODIFIÉ")
print("V2 SHADOW        : NON MODIFIÉ")
print("V1ExecutionChanged : False")

print("=" * 80)


if failed > 0:

    print()
    print(
        "ERREUR : certaines données ciblées sont absentes. "
        "Consulte le fichier de résumé."
    )

    raise SystemExit(1)


print()
print(
    "TÉLÉCHARGEMENT CIBLÉ TERMINÉ AVEC SUCCÈS"
)

raise SystemExit(0)
