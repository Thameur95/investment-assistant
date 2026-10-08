import pandas as pd
from pathlib import Path

print("=" * 60)
print("HISTORICAL LAB")
print("COMPARAISON STOP -2% VS -3% | OBJECTIF +3%")
print("=" * 60)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "stop_comparison.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

results = []

def test_strategy(data, i, stop_pct):
    entry = float(data.loc[i, "Close"])
    target_price = entry * 1.03
    stop_price = entry * (1 - stop_pct / 100)

    future = data.iloc[i + 1:i + 6].copy()

    if len(future) == 0:
        return None

    outcome = "NON RESOLU"
    stop_then_target = False
    days_to_exit = len(future)

    for n, (_, row) in enumerate(future.iterrows(), start=1):
        low = float(row["Low"])
        high = float(row["High"])

        stop_hit = low <= stop_price
        target_hit = high >= target_price

        if stop_hit and target_hit:
            outcome = "STOP ET OBJECTIF MEME JOUR"
            days_to_exit = n
            break

        if stop_hit:
            outcome = f"STOP -{stop_pct:.0f}%"
            days_to_exit = n

            remaining = future.iloc[n:]

            if len(remaining) > 0:
                if float(remaining["High"].max()) >= target_price:
                    stop_then_target = True

            break

        if target_hit:
            outcome = "OBJECTIF +3%"
            days_to_exit = n
            break

    return {
        "Outcome": outcome,
        "StopThenTarget": stop_then_target,
        "DaysToExit": days_to_exit
    }

for ticker, data in df.groupby("Ticker"):
    data = data.copy().sort_values("Date").reset_index(drop=True)

    buy_indexes = data.index[data["Signal"] == "ACHAT"]

    for i in buy_indexes:
        result_2 = test_strategy(data, i, 2)
        result_3 = test_strategy(data, i, 3)

        if result_2 is None or result_3 is None:
            continue

        results.append({
            "Ticker": ticker,
            "SignalDate": data.loc[i, "Date"],
            "Score": data.loc[i, "Score"],
            "Entry": data.loc[i, "Close"],
            "Outcome_Stop2": result_2["Outcome"],
            "Shakeout_Stop2": result_2["StopThenTarget"],
            "Days_Stop2": result_2["DaysToExit"],
            "Outcome_Stop3": result_3["Outcome"],
            "Shakeout_Stop3": result_3["StopThenTarget"],
            "Days_Stop3": result_3["DaysToExit"]
        })

result = pd.DataFrame(results)
result.to_csv(OUTPUT_FILE, index=False)

print()
print(f"Signaux comparés : {len(result)}")

for stop in [2, 3\]:
    outcome_col = f"Outcome_Stop{stop}"
    shakeout_col = f"Shakeout_Stop{stop}"

    print()
    print("=" * 60)
    print(f"STOP -{stop}% / OBJECTIF +3%")
    print("=" * 60)

    counts = result[outcome_col].value_counts()
    print(counts)

    targets = (result[outcome_col] == "OBJECTIF +3%").sum()
    stops = (result[outcome_col] == f"STOP -{stop}%").sum()
    ambiguous = (result[outcome_col] == "STOP ET OBJECTIF MEME JOUR").sum()
    unresolved = (result[outcome_col] == "NON RESOLU").sum()
    shakeouts = result[shakeout_col].sum()

    resolved = targets + stops

    print()
    print(f"Objectifs +3% : {targets}")
    print(f"Stops -{stop}% : {stops}")
    print(f"Stop et objectif même jour : {ambiguous}")
    print(f"Non résolus : {unresolved}")

    if resolved > 0:
        print(
            f"Taux objectif parmi cas résolus : "
            f"{targets / resolved * 100:.1f} %"
        )

    print(f"Shakeouts : {shakeouts}")

    if stops > 0:
        print(
            f"Part des stops suivis ensuite de +3% : "
            f"{shakeouts / stops * 100:.1f} %"
        )

print()
print("=" * 60)
print("COMPARAISON DIRECTE")
print("=" * 60)

saved = (
    (result["Outcome_Stop2"] == "STOP -2%")
    & (result["Outcome_Stop3"] != "STOP -3%")
).sum()

extra_targets = (
    (result["Outcome_Stop2"] == "STOP -2%")
    & (result["Outcome_Stop3"] == "OBJECTIF +3%")
).sum()

print(f"Stops -2% évités avec stop -3% : {saved}")
print(f"Stops -2% devenant objectif +3% avec stop -3% : {extra_targets}")

print()
print(f"Fichier : {OUTPUT_FILE}")
