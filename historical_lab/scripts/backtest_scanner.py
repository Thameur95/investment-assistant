import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("BACKTEST SCANNER")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "scanner_buy_signals.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)

df["Date"] = pd.to_datetime(df["Date"])

signals = df[
    df["Signal"] == "ACHAT"
].copy()

signals = signals.sort_values(
    ["Date", "Score", "Ticker"],
    ascending=[True, False, True]
)

signals.to_csv(
    OUTPUT_FILE,
    index=False
)

print()
print("=" * 50)
print("SIGNAUX ACHAT HISTORIQUES")
print("=" * 50)
print(f"Nombre de signaux : {len(signals)}")
print(f"Nombre de tickers : {signals['Ticker'].nunique()}")
print(f"Première date : {signals['Date'].min().date()}")
print(f"Dernière date : {signals['Date'].max().date()}")

print()
print("Répartition des scores :")
print(signals["Score"].value_counts().sort_index())

print()
print(f"Fichier : {OUTPUT_FILE}")
