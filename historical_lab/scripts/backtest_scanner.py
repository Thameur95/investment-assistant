import pandas as pd
from pathlib import Path

print("=" * 50)
print("HISTORICAL LAB")
print("BACKTEST STOP -2% / OBJECTIF +3%")
print("=" * 50)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "scanner_stop_target.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

results = []

for ticker, data in df.groupby("Ticker"):

    data = data.copy().sort_values("Date").reset_index(drop=True)

    buy_indexes = data.index[data["Signal"] == "ACHAT"]

    for i in buy_indexes:

        # On prend la clôture du jour du signal comme prix de référence
        entry = float(data.loc[i, "Close"])
        score = float(data.loc[i, "Score"])
        signal_date = data.loc[i, "Date"]

        stop_price = entry * 0.98
        target_price = entry * 1.03

        outcome = "NON RESOLU"
        exit_date = None
        exit_price = None
        days_to_exit = None

        max_gain = None
        max_loss = None
        stop_then_target = False

        future = data.iloc[i + 1:i + 6].copy()

        if len(future) == 0:
            continue

        max_high = float(future["High"].max())
        min_low = float(future["Low"].min())

        max_gain = (max_high / entry - 1) * 100
        max_loss = (min_low / entry - 1) * 100

        for n, (_, row) in enumerate(future.iterrows(), start=1):

            low = float(row["Low"])
            high = float(row["High"])

            stop_hit = low <= stop_price
            target_hit = high >= target_price

            if stop_hit and target_hit:
                outcome = "STOP ET OBJECTIF MEME JOUR"
                exit_date = row["Date"]
                days_to_exit = n
                break

            if stop_hit:
                outcome = "STOP -2%"
                exit_date = row["Date"]
                exit_price = stop_price
                days_to_exit = n

                remaining = future.iloc[n:]

                if len(remaining) > 0 and float(remaining["High"].max()) >= target_price:
                    stop_then_target = True

                break

            if target_hit:
                outcome = "OBJECTIF +3%"
                exit_date = row["Date"]
                exit_price = target_price
                days_to_exit = n
                break

        if outcome == "NON RESOLU":
            exit_date = future.iloc[-1]["Date"]
            exit_price = float(future.iloc[-1]["Close"])
            days_to_exit = len(future)

        results.append({
            "Ticker": ticker,
            "SignalDate": signal_date,
            "Score": score,
            "Entry": entry,
            "Stop": stop_price,
            "Target": target_price,
            "Outcome": outcome,
            "ExitDate": exit_date,
            "ExitPrice": exit_price,
            "DaysToExit": days_to_exit,
            "MaxGain5dPct": max_gain,
            "MaxLoss5dPct": max_loss,
            "StopThenTarget": stop_then_target
        })

result = pd.DataFrame(results)

result.to_csv(OUTPUT_FILE, index=False)

print()
print("=" * 50)
print("RESULTATS")
print("=" * 50)

print(f"Signaux testés : {len(result)}")

print()
print("Issues :")
print(result["Outcome"].value_counts())

print()

resolved = result[
    result["Outcome"].isin([
        "STOP -2%",
        "OBJECTIF +3%"
    ])
]

if len(resolved) > 0:
    targets = (resolved["Outcome"] == "OBJECTIF +3%").sum()
    stops = (resolved["Outcome"] == "STOP -2%").sum()

    print(f"Objectifs +3% : {targets}")
    print(f"Stops -2% : {stops}")
    print(f"Taux objectif parmi cas résolus : {targets / len(resolved) * 100:.1f} %")

print()

shakeouts = result["StopThenTarget"].sum()

print(f"Stop -2% puis objectif +3% plus tard : {shakeouts}")

stop_rows = result[result["Outcome"] == "STOP -2%"]

if len(stop_rows) > 0:
    print(
        f"Part des stops qui auraient ensuite atteint +3% : "
        f"{shakeouts / len(stop_rows) * 100:.1f} %"
    )

print()
print("Gain maximum moyen sur 5 séances : "
      f"{result['MaxGain5dPct'].mean():+.2f} %")

print("Perte maximum moyenne sur 5 séances : "
      f"{result['MaxLoss5dPct'].mean():+.2f} %")

print()
print(f"Fichier : {OUTPUT_FILE}")
