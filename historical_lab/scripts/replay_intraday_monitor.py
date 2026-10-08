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

    if len(day) < 6:
        continue

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

    opening_range = day.iloc[:6]

    or_high = (
        opening_range["High"].max()
    )

    or_low = (
        opening_range["Low"].min()
    )
    confirmation_time = None
    confirmation_price = None

    after_or = day.iloc[6:]

    for _, row in after_or.iterrows():

        if (
            row["Close"] > or_high
            and row["Close"] > row["VWAP"]
        ):

            confirmation_time = row[
                "Datetime_Paris"
            ]

            confirmation_price = float(
                row["Close"]
            )

            break

    close_final = float(
    day["Close"].iloc[-1]
    )

    vwap_final = float(
        day["VWAP"].iloc[-1]
    )

    confirmed = (
    confirmation_time is not None
    )
    intraday_return_pct = None

    if confirmed:

        intraday_return_pct = (
            (
                close_final
                / confirmation_price
            ) - 1
        ) * 100

    sessions.append({
        "Date": date,
        "OR_High": or_high,
        "OR_Low": or_low,
        "VWAP_Close": vwap_final,
        "Close_Final": close_final,
        "Confirmed": confirmed,
        "ConfirmationTime": confirmation_time,
        "ConfirmationPrice": confirmation_price,
        "IntradayReturnPct": intraday_return_pct,
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

print()
print(
    "Séances confirmées :",
    result["Confirmed"].sum()
)

print(
    "Taux de confirmation :",
    round(
        result["Confirmed"].mean() * 100,
        1
    ),
    "%"
)

confirmed_only = result[
    result["Confirmed"]
].copy()

print()

print(
    "Performance moyenne après confirmation :",
    round(
        confirmed_only[
            "IntradayReturnPct"
        ].mean(),
        3
    ),
    "%"
)

print(
    "Taux positif :",
    round(
        (
            confirmed_only[
                "IntradayReturnPct"
            ] > 0
        ).mean() * 100,
        1
    ),
    "%"
)
