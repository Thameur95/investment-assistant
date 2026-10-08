import pandas as pd
from pathlib import Path

print("=" * 70)
print("HISTORICAL LAB")
print("STOP FIXE -2% VS -3% VS ATR BORNE 2%-3%")
print("=" * 70)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "bounded_atr_comparison.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

strategies = {
    "STOP_FIXE_2": {
        "type": "fixed",
        "value": 2.0
    },
    "STOP_FIXE_3": {
        "type": "fixed",
        "value": 3.0
    },
    "STOP_ATR_BORNE_2_3": {
        "type": "bounded_atr"
    }
}

results = []


def test_strategy(data, index, strategy_name, strategy):

    entry = float(data.loc[index, "Close"])
    atr_pct = data.loc[index, "ATR14_Pct"]

    if pd.isna(atr_pct):
        return None

    atr_pct = float(atr_pct)

    if strategy["type"] == "fixed":
        stop_distance_pct = float(strategy["value"])

    else:
        stop_distance_pct = min(
            3.0,
            max(2.0, atr_pct)
        )

    stop_price = entry * (
        1 - stop_distance_pct / 100
    )

    target_price = entry * 1.03

    future = data.iloc[
        index + 1:index + 6
    ].copy()

    if len(future) == 0:
        return None

    outcome = "NON RESOLU"
    pnl_pct = None
    exit_price = None
    exit_date = None
    days_to_exit = None
    stop_then_target = False

    for day_number, (_, row) in enumerate(
        future.iterrows(),
        start=1
    ):

        day_open = float(row["Open"])
        day_low = float(row["Low"])
        day_high = float(row["High"])

        stop_hit = day_low <= stop_price
        target_hit = day_high >= target_price

        if day_open <= stop_price:
            outcome = "GAP SOUS STOP"
            exit_price = day_open
            pnl_pct = (
                exit_price / entry - 1
            ) * 100
            exit_date = row["Date"]
            days_to_exit = day_number

            remaining = future.iloc[day_number:]

            if (
                len(remaining) > 0
                and float(remaining["High"].max())
                >= target_price
            ):
                stop_then_target = True

            break

        if day_open >= target_price:
            outcome = "GAP AU-DESSUS OBJECTIF"
            exit_price = day_open
            pnl_pct = (
                exit_price / entry - 1
            ) * 100
            exit_date = row["Date"]
            days_to_exit = day_number
            break

        if stop_hit and target_hit:
            outcome = "AMBIGU"
            exit_date = row["Date"]
            days_to_exit = day_number
            break

        if stop_hit:
            outcome = "STOP"
            exit_price = stop_price
            pnl_pct = -stop_distance_pct
            exit_date = row["Date"]
            days_to_exit = day_number

            remaining = future.iloc[day_number:]

            if (
                len(remaining) > 0
                and float(remaining["High"].max())
                >= target_price
            ):
                stop_then_target = True

            break

        if target_hit:
            outcome = "OBJECTIF +3%"
            exit_price = target_price
            pnl_pct = 3.0
            exit_date = row["Date"]
            days_to_exit = day_number
            break

    if outcome == "NON RESOLU":

        last_row = future.iloc[-1]
        exit_price = float(last_row["Close"])

        pnl_pct = (
            exit_price / entry - 1
        ) * 100

        exit_date = last_row["Date"]
        days_to_exit = len(future)

    return {
        "Strategy": strategy_name,
        "Outcome": outcome,
        "PnL_Pct": pnl_pct,
        "Entry": entry,
        "ExitPrice": exit_price,
        "ExitDate": exit_date,
        "DaysToExit": days_to_exit,
        "ATR14_Pct": atr_pct,
        "StopDistancePct": stop_distance_pct,
        "StopPrice": stop_price,
        "TargetPrice": target_price,
        "StopThenTarget": stop_then_target
    }


for ticker, ticker_data in df.groupby("Ticker"):

    ticker_data = ticker_data.copy()
    ticker_data = ticker_data.sort_values(
        "Date"
    ).reset_index(drop=True)

    buy_indexes = ticker_data.index[
        ticker_data["Signal"] == "ACHAT"
    ]

    for index in buy_indexes:

        for strategy_name, strategy in strategies.items():

            tested = test_strategy(
                ticker_data,
                index,
                strategy_name,
                strategy
            )

            if tested is None:
                continue

            results.append({
                "Ticker": ticker,
                "SignalDate": ticker_data.loc[
                    index,
                    "Date"
                ],
                "Score": ticker_data.loc[
                    index,
                    "Score"
                ],
                "Entry": tested["Entry"],
                "Strategy": tested["Strategy"],
                "Outcome": tested["Outcome"],
                "PnL_Pct": tested["PnL_Pct"],
                "ATR14_Pct": tested["ATR14_Pct"],
                "StopDistancePct": tested[
                    "StopDistancePct"
                ],
                "StopPrice": tested["StopPrice"],
                "TargetPrice": tested[
                    "TargetPrice"
                ],
                "ExitPrice": tested["ExitPrice"],
                "ExitDate": tested["ExitDate"],
                "DaysToExit": tested[
                    "DaysToExit"
                ],
                "StopThenTarget": tested[
                    "StopThenTarget"
                ]
            })


result = pd.DataFrame(results)
result.to_csv(OUTPUT_FILE, index=False)

print()
print(
    "Nombre total de simulations :",
    len(result)
)

summary = []


for strategy_name in strategies.keys():

    subset = result[
        result["Strategy"] == strategy_name
    ].copy()

    valid = subset[
        subset["PnL_Pct"].notna()
    ].copy()

    pnl = valid["PnL_Pct"]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    neutral = pnl[pnl == 0]

    targets = (
        subset["Outcome"] == "OBJECTIF +3%"
    ).sum()

    target_gaps = (
        subset["Outcome"]
        == "GAP AU-DESSUS OBJECTIF"
    ).sum()

    stops = (
        subset["Outcome"] == "STOP"
    ).sum()

    stop_gaps = (
        subset["Outcome"] == "GAP SOUS STOP"
    ).sum()

    unresolved = (
        subset["Outcome"] == "NON RESOLU"
    ).sum()

    ambiguous = (
        subset["Outcome"] == "AMBIGU"
    ).sum()

    shakeouts = subset[
        "StopThenTarget"
    ].sum()

    average_pnl = pnl.mean()
    median_pnl = pnl.median()
    total_pnl = pnl.sum()

    win_rate = (
        (pnl > 0).mean() * 100
    )

    average_win = (
        wins.mean()
        if len(wins) > 0
        else 0
    )

    average_loss = (
        losses.mean()
        if len(losses) > 0
        else 0
    )

    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    if gross_loss > 0:
        profit_factor = (
            gross_profit / gross_loss
        )
    else:
        profit_factor = float("inf")

    average_stop = subset[
        "StopDistancePct"
    ].mean()

    summary.append({
        "Strategy": strategy_name,
        "Trades": len(valid),
        "AverageStopPct": average_stop,
        "AveragePnL": average_pnl,
        "MedianPnL": median_pnl,
        "TotalPnL": total_pnl,
        "WinRate": win_rate,
        "AverageWin": average_win,
        "AverageLoss": average_loss,
        "ProfitFactor": profit_factor,
        "Targets": targets,
        "TargetGaps": target_gaps,
        "Stops": stops,
        "StopGaps": stop_gaps,
        "Unresolved": unresolved,
        "Ambiguous": ambiguous,
        "Shakeouts": shakeouts
    })

    print()
    print("=" * 70)
    print(strategy_name)
    print("=" * 70)

    print(
        "Trades utilisables :",
        len(valid)
    )

    print(
        "Distance moyenne du stop : "
        "-{:.2f} %".format(
            average_stop
        )
    )

    print()
    print("Objectifs +3% :", targets)
    print(
        "Gaps au-dessus objectif :",
        target_gaps
    )
    print("Stops :", stops)
    print("Gaps sous stop :", stop_gaps)
    print("Non résolus :", unresolved)
    print("Cas ambigus :", ambiguous)
    print("Shakeouts :", shakeouts)

    print()
    print(
        "PnL moyen : {:+.3f} %".format(
            average_pnl
        )
    )

    print(
        "PnL médian : {:+.3f} %".format(
            median_pnl
        )
    )

    print(
        "PnL cumulé : {:+.2f} points %".format(
            total_pnl
        )
    )

    print(
        "Win rate : {:.1f} %".format(
            win_rate
        )
    )

    print(
        "Gain moyen : {:+.2f} %".format(
            average_win
        )
    )

    print(
        "Perte moyenne : {:+.2f} %".format(
            average_loss
        )
    )

    print(
        "Profit Factor : {:.3f}".format(
            profit_factor
        )
    )

    print()
    print("RÉSULTATS PAR SCORE")
    print("-" * 70)

    for score in sorted(
        valid["Score"].dropna().unique()
    ):

        score_data = valid[
            valid["Score"] == score
        ]

        score_pnl = score_data[
            "PnL_Pct"
        ]

        score_wins = score_pnl[
            score_pnl > 0
        ]

        score_losses = score_pnl[
            score_pnl < 0
        ]

        score_gross_profit = (
            score_wins.sum()
        )

        score_gross_loss = abs(
            score_losses.sum()
        )

        if score_gross_loss > 0:
            score_pf = (
                score_gross_profit
                / score_gross_loss
            )
        else:
            score_pf = float("inf")

        score_win_rate = (
            (score_pnl > 0).mean()
            * 100
        )

        print(
            "Score {}/100 | {} trades | "
            "PnL moyen {:+.3f}% | "
            "Win rate {:.1f}% | "
            "PF {:.3f}".format(
                int(score),
                len(score_data),
                score_pnl.mean(),
                score_win_rate,
                score_pf
            )
        )


summary_df = pd.DataFrame(summary)

print()
print("=" * 70)
print("CLASSEMENT FINAL")
print("=" * 70)

ranking = summary_df.sort_values(
    "AveragePnL",
    ascending=False
)

for _, row in ranking.iterrows():

    print(
        "{} | PnL moyen {:+.3f}% | "
        "Win rate {:.1f}% | "
        "PF {:.3f} | "
        "Stop moyen -{:.2f}%".format(
            row["Strategy"],
            row["AveragePnL"],
            row["WinRate"],
            row["ProfitFactor"],
            row["AverageStopPct"]
        )
    )

print()
print(
    "Fichier :",
    OUTPUT_FILE
)
