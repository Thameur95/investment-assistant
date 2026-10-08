import pandas as pd
from pathlib import Path

print("=" * 70)
print("HISTORICAL LAB")
print("COMPARAISON STOPS FIXES VS ATR")
print("=" * 70)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "stop_atr_comparison.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

strategies = {
    "STOP_FIXE_2": {"type": "fixed", "value": 2.0},
    "STOP_FIXE_3": {"type": "fixed", "value": 3.0},
    "STOP_ATR_1": {"type": "atr", "value": 1.0},
    "STOP_ATR_1_5": {"type": "atr", "value": 1.5},
    "STOP_ATR_2": {"type": "atr", "value": 2.0},
}

results = []


def test_strategy(data, i, strategy_name, strategy):

    entry = float(data.loc[i, "Close"])

    atr = data.loc[i, "ATR14"]

    if pd.isna(atr):
        return None

    atr = float(atr)

    if strategy["type"] == "fixed":
        stop_distance_pct = strategy["value"]
        stop_price = entry * (1 - stop_distance_pct / 100)

    else:
        multiplier = strategy["value"]
        stop_distance = atr * multiplier
        stop_price = entry - stop_distance
        stop_distance_pct = (stop_distance / entry) * 100

    target_price = entry * 1.03

    future = data.iloc[i + 1:i + 6].copy()

    if len(future) == 0:
        return None

    outcome = "NON RESOLU"
    pnl_pct = None
    exit_date = None
    days_to_exit = None

    for n, (_, row) in enumerate(future.iterrows(), start=1):

        low = float(row["Low"])
        high = float(row["High"])

        stop_hit = low <= stop_price
        target_hit = high >= target_price

        if stop_hit and target_hit:
            outcome = "AMBIGU"
            pnl_pct = None
            exit_date = row["Date"]
            days_to_exit = n
            break

        if stop_hit:
            outcome = "STOP"
            pnl_pct = -stop_distance_pct
            exit_date = row["Date"]
            days_to_exit = n
            break

        if target_hit:
            outcome = "OBJECTIF +3%"
            pnl_pct = 3.0
            exit_date = row["Date"]
            days_to_exit = n
            break

    if outcome == "NON RESOLU":

        last_row = future.iloc[-1]
        exit_price = float(last_row["Close"])

        pnl_pct = (exit_price / entry - 1) * 100
        exit_date = last_row["Date"]
        days_to_exit = len(future)

    return {
        "Strategy": strategy_name,
        "Outcome": outcome,
        "PnL_Pct": pnl_pct,
        "StopDistancePct": stop_distance_pct,
        "StopPrice": stop_price,
        "ATR14": atr,
        "ExitDate": exit_date,
        "DaysToExit": days_to_exit
    }


for ticker, data in df
