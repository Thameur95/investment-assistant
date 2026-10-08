import pandas as pd
import yfinance as yf
from pathlib import Path

print("=" * 60)
print("HISTORICAL LAB")
print("DOWNLOAD INTRADAY 5 MINUTES")
print("=" * 60)

BASE_DIR = Path(__file__).resolve().parent.parent

TICKERS_FILE = (
    BASE_DIR
    / "data"
    / "tickers.txt"
)

OUTPUT_DIR = (
    BASE_DIR
    / "data"
    / "intraday"
)

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True
)

with open(
    TICKERS_FILE,
    "r",
    encoding="utf-8"
) as f:

    tickers = [
        x.strip()
        for x in f
        if x.strip()
    ]

print()
print(
    f"{len(tickers)} tickers à télécharger"
)

success = 0
failed = 0

for ticker in tickers:

    try:

        print()
        print(
            f"Téléchargement : {ticker}"
        )

        data = yf.download(
            ticker,
            period="60d",
            interval="5m",
            auto_adjust=True,
            prepost=False,
            progress=False,
            threads=False
        )

        if data.empty:
            print(
                f"⚠️ Aucune donnée pour {ticker}"
            )
            failed += 1
            continue

        if isinstance(
            data.columns,
            pd.MultiIndex
        ):
            data.columns = (
                data.columns
                .get_level_values(0)
            )

        data = data.reset_index()

        datetime_column = (
            data.columns[0]
        )

        data = data.rename(
            columns={
                datetime_column:
                "Datetime"
            }
        )

        data["Datetime"] = pd.to_datetime(
            data["Datetime"],
            utc=True,
            errors="coerce"
        )

        data = data.dropna(
            subset=["Datetime"]
        )

        data["Datetime_Paris"] = (
            data["Datetime"]
            .dt.tz_convert(
                "Europe/Paris"
            )
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

        data = data[
            [
                "Ticker",
                "Datetime",
                "Datetime_Paris",
                "Date_Paris",
                "Time_Paris",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume"
            ]
        ]

        output_file = (
            OUTPUT_DIR
            / f"{ticker}_5m.csv"
        )

        data.to_csv(
            output_file,
            index=False
        )

        print(
            f"✅ {ticker} : "
            f"{len(data)} bougies"
        )

        success += 1

    except Exception as e:

        print(
            f"❌ {ticker} : {e}"
        )

        failed += 1

print()
print("=" * 60)
print("RÉSUMÉ")
print("=" * 60)

print(f"Succès : {success}")
print(f"Échecs : {failed}")
