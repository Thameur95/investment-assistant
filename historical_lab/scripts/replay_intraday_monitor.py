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
DAILY_FILE = (
    BASE_DIR
    / "data"
    / "processed"
    / "historical_indicators.csv"
)

df = pd.read_csv(INPUT_FILE)

daily = pd.read_csv(DAILY_FILE)

daily["Date"] = pd.to_datetime(
    daily["Date"]
)

daily = daily[
    daily["Ticker"] == "MSFT"
].copy()

daily = daily.sort_values(
    "Date"
).reset_index(drop=True)

daily["CandidateNextSession"] = (
    daily["Signal"] == "ACHAT"
)

daily["SessionDate"] = (
    daily["Date"]
    .shift(-1)
    .dt.strftime("%Y-%m-%d")
)

candidate_sessions = set(
    daily.loc[
        daily["CandidateNextSession"],
        "SessionDate"
    ].dropna()
)
daily_close_by_date = (
    daily.set_index(
        daily["Date"].dt.strftime("%Y-%m-%d")
    )["Close"]
    .to_dict()
)

daily_dates = (
    daily["Date"]
    .dt.strftime("%Y-%m-%d")
    .tolist()
)
daily_date_positions = {
    date: position
    for position, date in enumerate(daily_dates)
}

df["Datetime_Paris"] = pd.to_datetime(
    df["Datetime_Paris"]
)

sessions = []

for date, day in df.groupby("Date_Paris"):
    if date not in candidate_sessions:
        continue

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
    return_j1_pct = None
    return_j2_pct = None
    return_j5_pct = None

    if confirmed:

        intraday_return_pct = (
            (
                close_final
                / confirmation_price
            ) - 1
        ) * 100

    if confirmed and date in daily_date_positions:

        current_position = daily_date_positions[
            date
        ]

        future_returns = {}

        for horizon in [1, 2, 5]:

            future_position = (
                current_position
                + horizon
            )

            if future_position < len(daily_dates):

                future_date = daily_dates[
                    future_position
                ]

                future_close = daily_close_by_date[
                    future_date
                ]

                future_returns[horizon] = (
                    (
                        future_close
                        / confirmation_price
                    ) - 1
                ) * 100

        return_j1_pct = future_returns.get(1)
        return_j2_pct = future_returns.get(2)
        return_j5_pct = future_returns.get(5)

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
        "ReturnJ1Pct": return_j1_pct,
        "ReturnJ2Pct": return_j2_pct,
        "ReturnJ5Pct": return_j5_pct,
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
print()
print("=" * 60)
print("PERFORMANCE DEPUIS LE PRIX DE CONFIRMATION")
print("=" * 60)

for horizon in [1, 2, 5]:


    column = f"ReturnJ{horizon}Pct"

    valid_returns = confirmed_only[
        column
    ].dropna()

    if len(valid_returns) == 0:
        continue

    print()
    print(f"J+{horizon}")

    print(
        "Nombre de cas :",
        len(valid_returns)
    )

    print(
        "Performance moyenne :",
        round(
            valid_returns.mean(),
            3
        ),
        "%"
    )

    print(
        "Performance médiane :",
        round(
            valid_returns.median(),
            3
        ),
        "%"
    )

    print(
        "Taux positif :",
        round(
            (
                valid_returns > 0
            ).mean() * 100,
            1
        ),
        "%"
    )
