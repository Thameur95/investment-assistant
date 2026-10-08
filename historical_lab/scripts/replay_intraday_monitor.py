import pandas as pd
from pathlib import Path

print("=" * 60)
print("HISTORICAL LAB")
print("REPLAY INTRADAY MONITOR")
print("=" * 60)

BASE_DIR = Path(__file__).resolve().parent.parent

INPUT_FILE = (
    BASE_DIR
    / "data"
    / "intraday"
    / "MSFT_5m.csv"
)

df = pd.read_csv(INPUT_FILE)

df["Datetime_Paris"] = pd.to_datetime(
    df["Datetime_Paris"]
)

sessions = []

for date, day in df.groupby("Date_Paris"):

    day = day.sort_values(
        "Datetime_Paris"
    ).reset_index(drop=True)
        typical_price = (
        day["High"]
        + day["Low"]
        + day["Close"]
    ) / 3

    cumulative_volume = (
        day["Volume"].cumsum()
    )

    cumulative_vwap = (
        (typical_price * day["Volume"])
        .cumsum()
    )

    day["VWAP"] = (
        cumulative_vwap
        / cumulative_volume
    )

    if len(day) < 6:
        continue

    opening_range = day.iloc[:6]

    or_high = (
        opening_range["High"].max()
    )

    or_low = (
        opening_range["Low"].min()
    )

    sessions.append({
        "Date": date,
        "OR_High": or_high,
        "OR_Low": or_low,
        "Bars": len(day)
    })

result = pd.DataFrame(sessions)

print()
print("=" * 60)
print("OPENING RANGE")
print("=" * 60)

print(
    "Nombre de séances :",
    len(result)
)

print()
print(result.head(10))
