import pandas as pd
from pathlib import Path

print("=" * 65)
print("HISTORICAL LAB")
print("PNL : STOP -2% VS -3% | OBJECTIF +3%")
print("=" * 65)

BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_FILE = BASE_DIR / "backtests" / "pnl_stop_comparison.csv"

OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

df = pd.read_csv(INPUT_FILE)
df["Date"] = pd.to_datetime(df["Date"])
df = df.sort_values(["Ticker", "Date"]).reset_index(drop=True)

results = []


def test_strategy(data, i, stop_pct):

    entry = float(data.loc[i, "Close"])
    stop_price = entry * (1 - stop_pct / 100)
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

        # Avec les données journalières, impossible de savoir
        # lequel a été touché en premier.
        if stop_hit and target_hit:
            outcome = "AMBIGU"
            pnl_pct = None
            exit_date = row["Date"]
            days_to_exit = n
            break

        if stop_hit:
            outcome = f"STOP -{stop_pct:.0f}%"
            pnl_pct = -stop_pct
            exit_date = row["Date"]
            days_to_exit = n
            break

        if target_hit:
            outcome = "OBJECTIF +3%"
            pnl_pct = 3.0
            exit_date = row["Date"]
            days_to_exit = n
            break

    # Si ni stop ni objectif après 5 séances :
    # sortie à la clôture de la dernière séance disponible
    if outcome == "NON RESOLU":

        last_row = future.iloc[-1]
        exit_price = float(last_row["Close"])

        pnl_pct = (exit_price / entry - 1) * 100
        exit_date = last_row["Date"]
        days_to_exit = len(future)

    return {
        "Outcome": outcome,
        "PnL_Pct": pnl_pct,
        "ExitDate": exit_date,
        "DaysToExit": days_to_exit
    }


for ticker, data in df.groupby("Ticker"):

    data = data.copy()
    data = data.sort_values("Date").reset_index(drop=True)

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
            "PnL_Stop2_Pct": result_2["PnL_Pct"],
            "Days_Stop2": result_2["DaysToExit"],

            "Outcome_Stop3": result_3["Outcome"],
            "PnL_Stop3_Pct": result_3["PnL_Pct"],
            "Days_Stop3": result_3["DaysToExit"]
        })


result = pd.DataFrame(results)

result.to_csv(OUTPUT_FILE, index=False)

print()
print(f"Signaux comparés : {len(result)}")


def print_strategy_stats(stop):

    pnl_col = f"PnL_Stop{stop}_Pct"
    outcome_col = f"Outcome_Stop{stop}"

    print()
    print("=" * 65)
    print(f"STOP -{stop}% / OBJECTIF +3%")
    print("=" * 65)

    print()
    print("Issues :")
    print(result[outcome_col].value_counts())

    # Exclusion des cas ambigus
    valid = result[result[pnl_col].notna()].copy()

    pnl = valid[pnl_col]

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    breakeven = pnl[pnl == 0]

    total_pnl = pnl.sum()
    avg_pnl = pnl.mean()
    median_pnl = pnl.median()

    win_rate = (pnl > 0).mean() * 100

    avg_win = wins.mean() if len(wins) > 0 else 0
    avg_loss = losses.mean() if len(losses) > 0 else 0

    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else float("inf")
    )

    expectancy = avg_pnl

    print()
    print(f"Trades utilisables : {len(valid)}")
    print(f"Cas ambigus exclus : {(result[outcome_col] == 'AMBIGU').sum()}")

    print()
    print(f"PnL cumulé théorique : {total_pnl:+.2f} points %")
    print(f"PnL moyen par trade : {avg_pnl:+.3f} %")
    print(f"PnL médian par trade : {median_pnl:+.3f} %")

    print()
    print(f"Win rate : {win_rate:.1f} %")
    print(f"Trades gagnants : {len(wins)}")
    print(f"Trades perdants : {len(losses)}")
    print(f"Trades neutres : {len(breakeven)}")

    print()
    print(f"Gain moyen : {avg_win:+.2f} %")
    print(f"Perte moyenne : {avg_loss:+.2f} %")
    print(f"Profit Factor : {profit_factor:.3f}")
    print(f"Espérance par trade : {expectancy:+.3f} %")

    print()
    print("RESULTATS PAR SCORE")
    print("-" * 65)

    for score in sorted(valid["Score"].dropna().unique()):

        subset = valid[valid["Score"] == score]
        score_pnl = subset[pnl_col]

        score_winrate = (score_pnl > 0).mean() * 100
        score_avg = score_pnl.mean()
        score_total = score_pnl.sum()

        score_wins = score_pnl[score_pnl > 0]
        score_losses = score_pnl[score_pnl < 0]

        score_gross_profit = score_wins.sum()
        score_gross_loss = abs(score_losses.sum())

        score_pf = (
            score_gross_profit / score_gross_loss
            if score_gross_loss > 0
            else float("inf")
        )

        print()
        print(
            f"Score {int(score)}/100 | "
            f"{len(subset)} trades"
        )

        print(
            f"  PnL moyen : {score_avg:+.3f} % | "
            f"PnL cumulé : {score_total:+.2f} %"
        )

        print(
            f"  Win rate : {score_winrate:.1f} % | "
            f"Profit Factor : {score_pf:.3f}"
        )

    return {
        "stop": stop,
        "trades": len(valid),
        "total": total_pnl,
        "average": avg_pnl,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor
    }


stats_2 = print_strategy_stats(2)
stats_3 = print_strategy_stats(3)


print()
print("=" * 65)
print("COMPARAISON FINALE")
print("=" * 65)

print()
print(
    f"Stop -2% : PnL moyen {stats_2['average'\]:+.3f}% | "
    f"Win rate {stats_2['win_rate'\]:.1f}% | "
    f"PF {stats_2['profit_factor'\]:.3f}"
)

print(
    f"Stop -3% : PnL moyen {stats_3['average'\]:+.3f}% | "
    f"Win rate {stats_3['win_rate'\]:.1f}% | "
    f"PF {stats_3['profit_factor'\]:.3f}"
)

difference = stats_3["average"] - stats_2["average"]

print()
print(
    f"Différence de PnL moyen (-3% moins -2%) : "
    f"{difference:+.3f} point % par trade"
)

if stats_3["average"] > stats_2["average"\]:
    print("MEILLEUR PNL MOYEN : STOP -3%")
elif stats_2["average"] > stats_3["average"\]:
    print("MEILLEUR PNL MOYEN : STOP -2%")
else:
    print("PNL MOYEN IDENTIQUE")

print()
print(f"Fichier : {OUTPUT_FILE}")
