import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("BACKTEST SCANNER - PERFORMANCE FUTURE")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "scanner_buy_signals.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

# Prix de clôture futurs pour chaque ticker
df["Close_J1"] = df.groupby("Ticker")["Close"].shift(-1)
df["Close_J2"] = df.groupby("Ticker")["Close"].shift(-2)
df["Close_J5"] = df.groupby("Ticker")["Close"].shift(-5)

# Performance future en pourcentage
df["Perf_J1"] = (df["Close_J1"] / df["Close"] - 1) * 100
df["Perf_J2"] = (df["Close_J2"] / df["Close"] - 1) * 100
df["Perf_J5"] = (df["Close_J5"] / df["Close"] - 1) * 100

# Uniquement les signaux ACHAT du scanner réel
signals = df[df["Signal"] == "ACHAT"].copy()

signals = signals.sort_values(
    ["Date", "Score", "Ticker"],
    ascending=[True, False, True]
)

signals.to_csv(OUTPUT_FILE, index=False)

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
print("PERFORMANCE MOYENNE APRÈS SIGNAL")
print(f"J+1 : {signals['Perf_J1'].mean():+.2f} %")
print(f"J+2 : {signals['Perf_J2'].mean():+.2f} %")
print(f"J+5 : {signals['Perf_J5'].mean():+.2f} %")

print()
print("TAUX DE PERFORMANCE POSITIVE")
print(f"J+1 : {(signals['Perf_J1'] > 0).mean() * 100:.2f} %")
print(f"J+2 : {(signals['Perf_J2'] > 0).mean() * 100:.2f} %")
print(f"J+5 : {(signals['Perf_J5'] > 0).mean() * 100:.2f} %")

print()
print(f"Fichier : {OUTPUT_FILE}")
