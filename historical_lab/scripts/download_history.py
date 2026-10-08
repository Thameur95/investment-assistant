import yfinance as yf
import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("DOWNLOAD HISTORY")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
TICKERS_FILE = BASE_DIR / "data" / "tickers.txt"
RAW_DIR = BASE_DIR / "data" / "raw"

RAW_DIR.mkdir(parents=True, exist_ok=True)

with open(TICKERS_FILE, "r", encoding="utf-8") as f:
    tickers = [x.strip() for x in f if x.strip()]

print(f"{len(tickers)} tickers à télécharger")
print()

success = 0
failed = 0

for ticker in tickers:

    try:

        print(f"Téléchargement : {ticker}")

        data = yf.download(
            ticker,
            start="2019-01-01",
            auto_adjust=True,
            progress=False
        )

        if data.empty:
            print(f"⚠️ Aucun historique trouvé pour {ticker}")
            failed += 1
            continue

        output_file = RAW_DIR / f"{ticker}.csv"

        data = data.reset_index()

        if "Date" not in data.columns:
            data = data.rename(columns={data.columns[0]: "Date"})

        columns_to_keep = [
            "Date",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]

        available_columns = [
            col for col in columns_to_keep
            if col in data.columns
        ]

        data = data[available_columns]

    if len(data) > 0:
    first_row = data.iloc[0].astype(str)

    if (
        "MSFT" in first_row.values
        or "AAPL" in first_row.values
        or "GOOGL" in first_row.values
        or "META" in first_row.values
        or "NVDA" in first_row.values
    ):
        data = data.iloc[1:].reset_index(drop=True)

data.to_csv(output_file, index=False)

        print(
            f"✅ {ticker} : {len(data)} lignes sauvegardées"
        )

        success += 1

    except Exception as e:

        print(f"❌ {ticker} : {e}")

        failed += 1

print()
print("=" * 50)
print("RÉSUMÉ")
print("=" * 50)
print(f"Succès : {success}")
print(f"Échecs : {failed}")
