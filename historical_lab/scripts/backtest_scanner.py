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

# Performances futures par ticker
for n in [1, 2, 5]:
    future_close = df.groupby("Ticker")["Close"].shift(-n)
    df[f"Return_J{n}"] = (future_close / df["Close"] - 1) * 100

# On conserve uniquement les vrais signaux ACHAT
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
print("PERFORMANCE MOYENNE APRÈS SIGNAL")
print("=" * 50)

for n in [1, 2, 5]:
    column = f"Return_J{n}"
    valid = signals[column].dropna()

    print(
        f"J+{n} : moyenne {valid.mean():+.2f} % | "
        f"médiane {valid.median():+.2f} % | "
        f"positifs {(valid > 0).mean() * 100:.1f} %"
    )

print()
print("PERFORMANCE PAR SCORE")
print("=" * 50)

for score in sorted(signals["Score"].dropna().unique()):
    subset = signals[signals["Score"] == score]

    print()
    print(f"Score {int(score)}/100 | {len(subset)} signaux")

    for n in [1, 2, 5]:
        column = f"Return_J{n}"
        valid = subset[column].dropna()

        if len(valid) > 0:
            print(
                f"  J+{n} : {valid.mean():+.2f} % | "
                f"positifs {(valid > 0).mean() * 100:.1f} %"
            )

print()
print(f"Fichier : {OUTPUT_FILE}")
