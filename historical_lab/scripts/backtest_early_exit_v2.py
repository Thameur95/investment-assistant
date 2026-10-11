from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent

LEDGER_FILE = BASE_DIR / "results" / "risk_manager_ledger.csv"
INTRADAY_DIR = BASE_DIR / "data" / "intraday"
OUTPUT_DIR = BASE_DIR / "results"

OUTPUT_FILE = OUTPUT_DIR / "early_exit_v2_backtest.csv"
SUMMARY_FILE = OUTPUT_DIR / "early_exit_v2_summary.csv"

FEE_SIDE_PCT = 0.05
SLIPPAGE_SIDE_PCT = 0.05

MODEL = "FIXED_1000"


# ============================================================
# REGLES CANDIDATES
# ============================================================

RULES = [
    {
        "name": "V1_BASELINE",
        "minute": None,
        "return_max": None,
        "mfe_max": None,
        "require_below_vwap": False,
    },

    # Faiblesse precoce a 30 min
    {
        "name": "T30_RET_-0.75",
        "minute": 30,
        "return_max": -0.75,
        "mfe_max": None,
        "require_below_vwap": False,
    },
    {
        "name": "T30_RET_-0.75_MFE_0.25",
        "minute": 30,
        "return_max": -0.75,
        "mfe_max": 0.25,
        "require_below_vwap": False,
    },
    {
        "name": "T30_RET_-0.75_MFE_0.25_VWAP",
        "minute": 30,
        "return_max": -0.75,
        "mfe_max": 0.25,
        "require_below_vwap": True,
    },

    # Faiblesse a 60 min
    {
        "name": "T60_RET_-0.75",
        "minute": 60,
        "return_max": -0.75,
        "mfe_max": None,
        "require_below_vwap": False,
    },
    {
        "name": "T60_RET_-0.75_MFE_0.25",
        "minute": 60,
        "return_max": -0.75,
        "mfe_max": 0.25,
        "require_below_vwap": False,
    },
    {
        "name": "T60_RET_-0.75_MFE_0.25_VWAP",
        "minute": 60,
        "return_max": -0.75,
        "mfe_max": 0.25,
        "require_below_vwap": True,
    },

    # Regles en R, plus adaptatives a la volatilite
    {
        "name": "T30_RET_-0.30R_MFE_0.10R",
        "minute": 30,
        "return_r_max": -0.30,
        "mfe_r_max": 0.10,
        "require_below_vwap": False,
    },
    {
        "name": "T60_RET_-0.30R_MFE_0.10R",
        "minute": 60,
        "return_r_max": -0.30,
        "mfe_r_max": 0.10,
        "require_below_vwap": False,
    },
]


def prepare_intraday(df):
    x = df.copy()

    x["Datetime_Paris"] = pd.to_datetime(
        x["Datetime_Paris"],
        errors="coerce",
    )

    x["Date_Paris"] = x["Date_Paris"].astype(str).str[:10]

    for column in ["Open", "High", "Low", "Close", "Volume"\]:
        x[column] = pd.to_numeric(x[column], errors="coerce")

    x = x.dropna(
        subset=[
            "Datetime_Paris",
            "Open",
            "High",
            "Low",
            "Close",
        ]
    )

    return x.sort_values("Datetime_Paris").reset_index(drop=True)


def add_vwap(df):
    x = df.copy()

    typical = (
        x["High"]
        + x["Low"]
        + x["Close"]
    ) / 3.0

    cumulative_volume = x["Volume"].cumsum()

    x["VWAP"] = (
        (typical * x["Volume"]).cumsum()
        / cumulative_volume.replace(0, np.nan)
    )

    return x


def load_intraday():
    data = {}

    for path in sorted(INTRADAY_DIR.glob("*_5m.csv")):
        ticker = path.stem.replace("_5m", "")

        df = pd.read_csv(path)

        data[ticker] = prepare_intraday(df)

    return data


def get_checkpoint(trade, intraday, minute):
    decision_time = pd.Timestamp(trade["DecisionTime"])
    exit_time = pd.Timestamp(trade["ExitTime"])

    bars_needed = minute // 5

    bars = intraday.loc[
        (intraday["Datetime_Paris"] > decision_time)
        & (intraday["Datetime_Paris"] < exit_time)
    ].copy()

    bars = bars.sort_values(
        "Datetime_Paris"
    ).reset_index(drop=True)

    if len(bars) < bars_needed:
        return None

    window = bars.iloc[:bars_needed].copy()

    checkpoint = window.iloc[-1]

    checkpoint_time = checkpoint["Datetime_Paris"]
    checkpoint_price = float(checkpoint["Close"])

    raw_entry = float(trade["RawEntryPrice"])

    ret_pct = (
        checkpoint_price / raw_entry - 1.0
    ) * 100.0

    mfe_pct = max(
        0.0,
        (
            float(window["High"].max())
            / raw_entry
            - 1.0
        )
        * 100.0,
    )

    mae_pct = min(
        0.0,
        (
            float(window["Low"].min())
            / raw_entry
            - 1.0
        )
        * 100.0,
    )

    session_date = str(
        checkpoint["Date_Paris"]
    )[:10]

    session = intraday.loc[
        (intraday["Date_Paris"] == session_date)
        & (
            intraday["Datetime_Paris"]
            <= checkpoint_time
        )
    ].copy()

    session = add_vwap(session)

    if session.empty:
        above_vwap = np.nan
    else:
        last_vwap = session["VWAP"].iloc[-1]

        above_vwap = (
            bool(checkpoint_price > last_vwap)
            if not pd.isna(last_vwap)
            else np.nan
        )

    sl_pct = float(trade["SLPct"])

    return {
        "CheckpointTime": checkpoint_time,
        "CheckpointRawPrice": checkpoint_price,
        "CheckpointReturnPct": ret_pct,
        "CheckpointMFE": mfe_pct,
        "CheckpointMAE": mae_pct,
        "CheckpointReturnR":
            ret_pct / sl_pct
            if sl_pct > 0 else np.nan,
        "CheckpointMFER":
            mfe_pct / sl_pct
            if sl_pct > 0 else np.nan,
        "CheckpointMAER":
            mae_pct / sl_pct
            if sl_pct > 0 else np.nan,
        "AboveVWAP": above_vwap,
    }


def rule_triggered(rule, checkpoint):
    if checkpoint is None:
        return False

    if "return_max" in rule:
        value = rule.get("return_max")

        if value is not None:
            if checkpoint["CheckpointReturnPct"] > value:
                return False

    if "mfe_max" in rule:
        value = rule.get("mfe_max")

        if value is not None:
            if checkpoint["CheckpointMFE"] > value:
                return False

    if "return_r_max" in rule:
        value = rule.get("return_r_max")

        if value is not None:
            if checkpoint["CheckpointReturnR"] > value:
                return False

    if "mfe_r_max" in rule:
        value = rule.get("mfe_r_max")

        if value is not None:
            if checkpoint["CheckpointMFER"] > value:
                return False

    if rule.get("require_below_vwap", False):
        if checkpoint["AboveVWAP"] is not False:
            return False

    return True


def calculate_early_exit_return(
    trade,
    checkpoint,
):
    raw_entry = float(trade["RawEntryPrice"])

    effective_entry = float(
        trade["EffectiveEntryPrice"]
    )

    raw_exit = float(
        checkpoint["CheckpointRawPrice"]
    )

    effective_exit = (
        raw_exit
        * (
            1.0
            - SLIPPAGE_SIDE_PCT / 100.0
       )
    )

    net_return_pct = (
        effective_exit
        / effective_entry
        - 1.0
    ) * 100.0 - 2.0 * FEE_SIDE_PCT

    return (
        raw_exit,
        effective_exit,
        net_return_pct,
    )


def simulate_rule(
    rule,
    trades,
    intraday_by_ticker,
):
    rows = []

    for _, trade in trades.iterrows():

        ticker = trade["Ticker"]

        baseline_return = float(
            trade["ReturnPct"]
        )

        result = {
            "Rule": rule["name"],
            "Ticker": ticker,
            "SessionDate":
                trade["SessionDate"],
            "Decision":
                trade["Decision"],
            "DecisionTime":
                trade["DecisionTime"],
            "BaselineExitTime":
                trade["ExitTime"],
            "BaselineExitReason":
                trade["ExitReason"],
            "BaselineReturnPct":
                baseline_return,
            "EarlyExit": False,
            "NewExitTime":
                trade["ExitTime"],
            "NewExitReason":
                trade["ExitReason"],
            "NewReturnPct":
                baseline_return,
        }

        minute = rule.get("minute")

        if minute is None:
            rows.append(result)
            continue

        intraday = intraday_by_ticker.get(
            ticker
        )

        if intraday is None:
            result["Diagnostic"] = (
                "INTRADAY_MANQUANT"
            )
            rows.append(result)
            continue

        checkpoint = get_checkpoint(
            trade,
            intraday,
            minute,
        )

        if checkpoint is None:
            result["Diagnostic"] = (
                "CHECKPOINT_NON_ATTEINT"
            )
            rows.append(result)
            continue

        result.update(checkpoint)

        if rule_triggered(
            rule,
            checkpoint,
        ):
            (
                raw_exit,
                effective_exit,
                new_return,
            ) = calculate_early_exit_return(
                trade,
                checkpoint,
            )

            result["EarlyExit"] = True
            result["NewExitTime"] = (
                checkpoint["CheckpointTime"]
            )
            result["NewExitReason"] = (
                f"EARLY_EXIT_T{minute}"
            )
            result["NewRawExitPrice"] = (
                raw_exit
            )
            result["NewEffectiveExitPrice"] = (
                effective_exit
            )
            result["NewReturnPct"] = (
                new_return
            )

        result["ImprovementPctPoint"] = (
            result["NewReturnPct"]
            - baseline_return
        )

        rows.append(result)

    return pd.DataFrame(rows)


def summarize(results):
    summaries = []

    for rule, group in results.groupby(
        "Rule",
        sort=False,
    ):
        baseline = (
            group["BaselineReturnPct"]
            .astype(float)
        )

        new = (
            group["NewReturnPct"]
            .astype(float)
        )

        early = group["EarlyExit"].fillna(False)

        winners = new[new > 0]
        losers = new[new < 0]

        gross_profit = winners.sum()
        gross_loss = abs(losers.sum())

        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else np.inf
        )

        summaries.append({
            "Rule": rule,
            "Trades": len(group),
            "EarlyExits": int(early.sum()),
            "BaselineSumPct":
                float(baseline.sum()),
            "NewSumPct":
                float(new.sum()),
            "DeltaPctPoint":
                float(
                    new.sum()
                    - baseline.sum()
                ),
            "AverageReturnPct":
                float(new.mean()),
            "MedianReturnPct":
                float(new.median()),
            "WinRatePct":
                float(
                    (new > 0).mean()
                    * 100.0
                ),
            "ProfitFactor":
                float(profit_factor),
            "WorstTradePct":
                float(new.min()),
            "BestTradePct":
                float(new.max()),
            "ImprovedTrades":
                int(
                    (
                        new
                        > baseline
                    ).sum()
                ),
            "DegradedTrades":
                int(
                    (
                        new
                        < baseline
                    ).sum()
                ),
        })

    return pd.DataFrame(summaries)


print("=" * 120)
print("HISTORICAL LAB - EARLY EXIT V2")
print("V1 INCHANGEE - TEST DES REGLES DE SORTIE PRECOCE")
print("=" * 120)

ledger = pd.read_csv(LEDGER_FILE)

ledger = ledger.loc[
    ledger["Model"] == MODEL
].copy()

ledger["DecisionTime"] = pd.to_datetime(
    ledger["DecisionTime"],
    errors="coerce",
)

ledger["ExitTime"] = pd.to_datetime(
    ledger["ExitTime"],
    errors="coerce",
)

print("Trades FIXED_1000 :", len(ledger))

intraday_by_ticker = load_intraday()

all_results = []

for rule in RULES:
    result = simulate_rule(
        rule,
        ledger,
        intraday_by_ticker,
    )

    all_results.append(result)

results = pd.concat(
    all_results,
    ignore_index=True,
)

summary = summarize(results)

summary = summary.sort_values(
    [
        "NewSumPct",
        "ProfitFactor",
    ],
    ascending=False,
).reset_index(drop=True)

results.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8",
)

summary.to_csv(
    SUMMARY_FILE,
    index=False,
    encoding="utf-8",
)

print()
print("=" * 160)
print("COMPARAISON V1 / CANDIDATS V2")
print("=" * 160)

display_columns = [
    "Rule",
    "Trades",
    "EarlyExits",
    "BaselineSumPct",
    "NewSumPct",
    "DeltaPctPoint",
    "AverageReturnPct",
    "WinRatePct",
    "ProfitFactor",
    "WorstTradePct",
    "ImprovedTrades",
    "DegradedTrades",
]

print(
    summary[
        display_columns
    ]
    .round(3)
    .to_string(index=False)
)

print()
print("Details :", OUTPUT_FILE)
print("Resume  :", SUMMARY_FILE)

print("=" * 160)
print("FIN DU BACKTEST EARLY EXIT V2")
print("=" * 160)
