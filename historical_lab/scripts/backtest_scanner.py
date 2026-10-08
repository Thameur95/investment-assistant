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


for ticker, data in df.groupby("Ticker"):

    data = data.copy()
    data = data.sort_values("Date").reset_index(drop=True)

    buy_indexes = data.index[data["Signal"] == "ACHAT"]

    for i in buy_indexes:

        for strategy_name, strategy in strategies.items():

            tested = test_strategy(
                data,
                i,
                strategy_name,
                strategy
            )

            if tested is None:
                continue

            results.append({
                "Ticker": ticker,
                "SignalDate": data.loc[i, "Date"],
                "Score": data.loc[i, "Score"],
                "Entry": data.loc[i, "Close"],
                "Strategy": tested["Strategy"],
                "Outcome": tested["Outcome"],
                "PnL_Pct": tested["PnL_Pct"],
                "StopDistancePct": tested["StopDistancePct"],
                "StopPrice": tested["StopPrice"],
                "ATR14": tested["ATR14"],
                "ExitDate": tested["ExitDate"],
                "DaysToExit": tested["DaysToExit"]
            })


result = pd.DataFrame(results)

result.to_csv(OUTPUT_FILE, index=False)

print()
print("Nombre total de simulations :", len(result))


summary = []

for strategy_name in strategies.keys():

    subset = result[result["Strategy"] == strategy_name].copy()

    valid = subset[subset["PnL_Pct"].notna()].copy()

    pnl = valid["PnL_Pct"]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]

    targets = (subset["Outcome"] == "OBJECTIF +3%").sum()
    stops = (subset["Outcome"] == "STOP").sum()
    unresolved = (subset["Outcome"] == "NON RESOLU").sum()
    ambiguous = (subset["Outcome"] == "AMBIGU").sum()

    avg_pnl = pnl.mean()
    total_pnl = pnl.sum()
    win_rate = (pnl > 0).mean() * 100

    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = losses.mean() if len(losses) > 0 else 0

    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf")

    avg_stop_distance = subset["StopDistancePct"].mean()

    summary.append({
        "Strategy": strategy_name,
        "Trades": len(valid),
        "Targets": targets,
        "Stops": stops,
        "Unresolved": unresolved,
        "Ambiguous": ambiguous,
        "AvgStopPct": avg_stop_distance,
        "AvgPnL": avg_pnl,
        "TotalPnL": total_pnl,
        "WinRate": win_rate,
        "AvgWin": avg_win,
        "AvgLoss": avg_loss,
        "ProfitFactor": profit_factor
    })

    print()
    print("=" * 70)
    print(strategy_name)
    print("=" * 70)

    print("Trades utilisables :", len(valid))
    print("Objectifs +3% :", targets)
    print("Stops :", stops)
    print("Non resolus :", unresolved)
    print("Cas ambigus :", ambiguous)

    print()
    print("Distance stop moyenne : {:+.2f} %".format(-avg_stop_distance))
    print("PnL moyen : {:+.3f} %".format(avg_pnl))
    print("PnL cumule : {:+.2f} points %".format(total_pnl))
    print("Win rate : {:.1f} %".format(win_rate))
    print("Gain moyen : {:+.2f} %".format(avg_win))
    print("Perte moyenne : {:+.2f} %".format(avg_loss))
    print("Profit Factor : {:.3f}".format(profit_factor))


summary_df = pd.DataFrame(summary)

print()
print("=" * 70)
print("CLASSEMENT PAR PNL MOYEN")
print("=" * 70)

ranking = summary_df.sort_values(
    "AvgPnL",
    ascending=False
)

for _, row in ranking.iterrows():

    print(
        "{} | PnL moyen {:+.3f}% | Win rate {:.1f}% | "
        "PF {:.3f} | Stop moyen -{:.2f}%".format(
            row["Strategy"],
            row["AvgPnL"],
            row["WinRate"],
            row["ProfitFactor"],
            row["AvgStopPct"]
        )
    )


print()
print("=" * 70)
print("RESULTATS ATR PAR SCORE")
print("=" * 70)

atr_strategies = [
    "STOP_ATR_1",
    "STOP_ATR_1_5",
    "STOP_ATR_2"
]

for strategy_name in atr_strategies:

    print()
    print(strategy_name)

    subset = result[
        (result["Strategy"] == strategy_name)
        & result["PnL_Pct"].notna()
    ]

    for score in sorted(subset["Score"].dropna().unique()):

        score_data = subset[
            subset["Score"] == score
        ]

        score_pnl = score_data["PnL_Pct"]

        win_rate = (score_pnl > 0).mean() * 100
        avg_pnl = score_pnl.mean()

        wins = score_pnl[score_pnl > 0]
        losses = score_pnl[score_pnl < 0]

        gross_profit = wins.sum()
        gross_loss = abs(losses.sum())

        if gross_loss > 0:
            pf = gross_profit / gross_loss
        else:
            pf = float("inf")

        print(
            "  Score {}/100 | {} trades | "
            "PnL moyen {:+.3f}% | Win rate {:.1f}% | PF {:.3f}".format(
                int(score),
                len(score_data),
                avg_pnl,
                win_rate,
                pf
            )
        )


print()
print("Fichier :", OUTPUT_FILE)
