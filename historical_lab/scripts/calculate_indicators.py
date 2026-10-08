import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("INDICATEURS + SCORE DU MOTEUR REEL")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_database.csv"
OUTPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"

df = pd.read_csv(INPUT_FILE)

df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

results = []

for ticker, data in df.groupby("Ticker"):

    data = data.copy().sort_values("Date").reset_index(drop=True)

    # Variation sur 5 séances, identique au scanner réel
    data["Var5j"] = (data["Close"] / data["Close"].shift(5) - 1) * 100

    # Moyenne des 20 dernières clôtures
    data["MM20"] = data["Close"].rolling(20).mean()

    # Volume moyen des 20 séances précédentes
    data["AvgVolume20"] = data["Volume"].shift(1).rolling(20).mean()

    # RVOL identique au scanner réel
    data["RVOL"] = data["Volume"] / data["AvgVolume20"]

    # RSI14 identique au scanner réel
    delta = data["Close"].diff()

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.rolling(14).mean()
    avg_loss = losses.rolling(14).mean()

    rs = avg_gain / avg_loss

    data["RSI14"] = 100 - (100 / (1 + rs))

    data.loc[
        (avg_loss == 0) & avg_gain.notna(),
        "RSI14"
    ] = 100

    # Score exactement basé sur scanner.py
    def calculate_score(row):

        if pd.isna(row["Var5j"]) or pd.isna(row["MM20"]) or pd.isna(row["RVOL"]) or pd.isna(row["RSI14"]):
            return None

        score = 0

        # Prix > moyenne 20 jours
        if row["Close"] > row["MM20"\]:
            score += 35
        else:
            score -= 20

        # Variation 5 jours
        if 3 < row["Var5j"] < 8:
            score += 25
        elif 0 <= row["Var5j"] <= 3:
            score += 10
        elif row["Var5j"] <= -3:
            score -= 25

        # RVOL
        if row["RVOL"] >= 1.5:
            score += 20
        elif row["RVOL"] >= 1:
            score += 10

        # RSI14
        if 50 <= row["RSI14"] <= 65:
            score += 20
        elif 45 <= row["RSI14"] < 75:
            score += 10
        else:
            score -= 30

        return max(0, min(100, score))

    data["Score"] = data.apply(calculate_score, axis=1)

    # Signal exactement basé sur scanner.py
    def calculate_signal(row):

        if pd.isna(row["Score"]):
            return ""

        p = row["Close"]
        mm = row["MM20"]
        var = row["Var5j"]
        rsi = row["RSI14"]

        if rsi >= 75 and var > 3:
            return "SURCHAUFFE"

        if rsi >= 75:
            return "SURVEILLANCE HAUTE"

        if var <= -5:
            return "FAIBLESSE FORTE"

        if p > mm and 3 < var < 8 and 50 <= rsi < 70:
            return "ACHAT"

        if p > mm and rsi >= 40:
            return "SURVEILLANCE"

        return "AUCUN SIGNAL"

    data["Signal"] = data.apply(calculate_signal, axis=1)

    results.append(data)

final_df = pd.concat(results, ignore_index=True)

final_df.to_csv(OUTPUT_FILE, index=False)

valid = final_df["Score"].notna().sum()

print()
print("=" * 50)
print("CALCUL TERMINE")
print("=" * 50)
print(f"Lignes totales : {len(final_df)}")
print(f"Tickers : {final_df['Ticker'].nunique()}")
print(f"Lignes avec score : {valid}")
print(f"Début : {final_df['Date'].min().date()}")
print(f"Fin : {final_df['Date'].max().date()}")
print(f"Fichier : {OUTPUT_FILE}")
