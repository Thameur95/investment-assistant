import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("CALCULATE INDICATORS")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_database.csv"
OUTPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"

df = pd.read_csv(INPUT_FILE)

df["Date"] = pd.to_datetime(df["Date"])

df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

# Variation sur 5 séances
df["Var5j"] = (
    df.groupby("Ticker")["Close"]
    .pct_change(5)
    .mul(100)
)

# RSI 14
def calculate_rsi(series, period=14):
    delta = series.diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period
    ).mean()

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))

df["RSI14"] = (
    df.groupby("Ticker", group_keys=False)["Close"]
    .apply(calculate_rsi)
)

# Volume moyen sur les 20 séances précédentes
df["AvgVolume20"] = (
    df.groupby("Ticker")["Volume"]
    .transform(lambda x: x.shift(1).rolling(20).mean())
)

# Relative Volume
df["RVOL"] = df["Volume"] / df["AvgVolume20"]

df.to_csv(OUTPUT_FILE, index=False)

print()
print("=" * 50)
print("INDICATEURS CALCULÉS")
print("=" * 50)
print(f"Lignes : {len(df)}")
print(f"Tickers : {df['Ticker'].nunique()}")
print(f"Début : {df['Date'].min().date()}")
print(f"Fin : {df['Date'].max().date()}")
print(f"Fichier : {OUTPUT_FILE}")
