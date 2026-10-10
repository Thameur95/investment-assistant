from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTRADAY_DIR = BASE_DIR / "data" / "intraday"
RESULTS_DIR = BASE_DIR / "results"

TRADES_FILE = RESULTS_DIR / "risk_manager_source_trades.csv"
OUTPUT_DETAILS = RESULTS_DIR / "rvol_trade_details.csv"
OUTPUT_CLASSES = RESULTS_DIR / "rvol_class_summary.csv"
OUTPUT_THRESHOLDS = RESULTS_DIR / "rvol_threshold_summary.csv"

STRATEGY_TO_ANALYZE = "HYBRID_ATRSL_60"
MIN_HISTORY_SESSIONS = 3
RVOL_THRESHOLDS = [1.0, 1.2, 1.5, 2.0]


# ============================================================
# UTILITAIRES
# ============================================================

def prepare_intraday(df):
    required = {
        "Datetime_Paris",
        "Date_Paris",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    }

    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            "Colonnes intraday manquantes : "
            + ", ".join(sorted(missing))
        )

    result = df.copy()

    result["Datetime_Paris"] = pd.to_datetime(
        result["Datetime_Paris"],
        errors="coerce",
    )

    result["Date_Paris"] = (
        result["Date_Paris"]
        .astype(str)
        .str[:10]
    )

    result["Volume"] = pd.to_numeric(
        result["Volume"],
        errors="coerce",
    )

    result = (
        result
        .dropna(
            subset=[
                "Datetime_Paris",
                "Date_Paris",
                "Volume",
            ]
        )
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )

    # Position de la bougie dans la séance.
    result["BarNumber"] = (
        result.groupby("Date_Paris").cumcount()
    )

    # Moyenne du volume de la même bougie sur les séances précédentes.
    # shift(1) garantit qu'aucune donnée du jour courant n'est utilisée.
    result["HistoricalMeanVolumeSameBar"] = (
        result
        .groupby("BarNumber")["Volume"]
        .transform(
            lambda values: (
                values
                .expanding(
                    min_periods=MIN_HISTORY_SESSIONS
                )
                .mean()
                .shift(1)
            )
        )
    )

    result["RVOLSameBar"] = (
        result["Volume"]
        / result["HistoricalMeanVolumeSameBar"]
        .replace(0, np.nan)
    )

    return result


def calculate_metrics(values):
    series = pd.Series(
        values,
        dtype="float64",
    ).dropna()

    if series.empty:
        return {
            "Trades": 0,
            "WinRatePct": np.nan,
            "ExpectancyPct": np.nan,
            "MedianPct": np.nan,
            "ProfitFactor": np.nan,
            "MaxDrawdownPct": np.nan,
        }

    winners = series[series > 0]
    losers = series[series < 0]

    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = np.inf
    else:
        profit_factor = np.nan

    equity = (
        1.0 + series / 100.0
    ).cumprod()

    drawdown = (
        equity / equity.cummax() - 1.0
    )

    return {
        "Trades": len(series),
        "WinRatePct": (
            (series > 0).mean() * 100.0
        ),
        "ExpectancyPct": float(series.mean()),
        "MedianPct": float(series.median()),
        "ProfitFactor": profit_factor,
        "MaxDrawdownPct": (
            float(drawdown.min()) * 100.0
        ),
    }


def load_rvol_by_ticker():
    data = {}

    for intraday_file in sorted(
        INTRADAY_DIR.glob("*_5m.csv")
    ):
        ticker = (
            intraday_file.stem
            .replace("_5m", "")
        )

        try:
            data[ticker] = prepare_intraday(
                pd.read_csv(intraday_file)
            )
        except Exception as error:
            print(
                ticker,
                "| erreur intraday :",
                str(error),
            )

    return data


def attach_rvol(trades, intraday_by_ticker):
    rows = []

    for _, trade in trades.iterrows():
        row = trade.to_dict()
        ticker = row["Ticker"]
        decision_time = pd.Timestamp(
            row["DecisionTime"]
        )

        row["DecisionRVOL"] = np.nan
        row["DecisionBarNumber"] = np.nan
        row["HistoricalMeanVolumeSameBar"] = np.nan
        row["DecisionBarVolume"] = np.nan
        row["RVOLStatus"] = "INTRADAY_ABSENT"

        intraday = intraday_by_ticker.get(ticker)

        if intraday is None or intraday.empty:
            rows.append(row)
            continue

        exact = intraday.loc[
            intraday["Datetime_Paris"]
            == decision_time
        ]

        if exact.empty:
            # Repli prudent : dernière bougie disponible au moment de décision.
            same_day = intraday.loc[
                (
                    intraday["Date_Paris"]
                    == decision_time.strftime("%Y-%m-%d")
                )
                & (
                    intraday["Datetime_Paris"]
                    <= decision_time
                )
            ]

            if same_day.empty:
                row["RVOLStatus"] = "BOUGIE_DECISION_ABSENTE"
                rows.append(row)
                continue

            decision_bar = same_day.iloc[-1]
            row["RVOLStatus"] = "BOUGIE_PRECEDENTE_UTILISEE"
        else:
            decision_bar = exact.iloc[-1]
            row["RVOLStatus"] = "OK"

        row["DecisionRVOL"] = decision_bar["RVOLSameBar"]
        row["DecisionBarNumber"] = decision_bar["BarNumber"]
        row["HistoricalMeanVolumeSameBar"] = decision_bar[
            "HistoricalMeanVolumeSameBar"
        ]
        row["DecisionBarVolume"] = decision_bar["Volume"]

        if pd.isna(row["DecisionRVOL"]):
            row["RVOLStatus"] = "HISTORIQUE_INSUFFISANT"

        rows.append(row)

    return pd.DataFrame(rows)


# ============================================================
# EXECUTION
# ============================================================

print("=" * 120)
print("HISTORICAL LAB - ANALYSE RVOL")
print("LE RVOL RESTE INFORMATIF, LE MOTEUR V1 N'EST PAS MODIFIE")
print("=" * 120)

if not TRADES_FILE.exists():
    raise FileNotFoundError(
        "Le fichier source est absent : "
        f"{TRADES_FILE}"
    )

trades = pd.read_csv(TRADES_FILE)

required_trade_columns = {
    "Ticker",
    "DecisionTime",
    "Decision",
    "NetReturnPct",
    "TargetHit",
    "StopHit",
}

missing_trade_columns = (
    required_trade_columns
    - set(trades.columns)
)

if missing_trade_columns:
    raise ValueError(
        "Colonnes trades manquantes : "
        + ", ".join(
            sorted(missing_trade_columns)
        )
    )

trades["DecisionTime"] = pd.to_datetime(
    trades["DecisionTime"],
    errors="coerce",
)

trades["NetReturnPct"] = pd.to_numeric(
    trades["NetReturnPct"],
    errors="coerce",
)

source = trades.loc[
    trades["DecisionTime"].notna()
    & trades["NetReturnPct"].notna()
].copy()

if source.empty:
    raise SystemExit(
        "Aucun trade HYBRID exploitable."
    )

intraday_by_ticker = load_rvol_by_ticker()

details = attach_rvol(
    source,
    intraday_by_ticker,
)

details["DecisionRVOL"] = pd.to_numeric(
    details["DecisionRVOL"],
    errors="coerce",
)

available = details.dropna(
    subset=[
        "DecisionRVOL",
        "NetReturnPct",
    ]
).copy()

print(
    "Trades source :",
    len(details),
)

print(
    "RVOL calculable :",
    len(available),
)

print(
    "RVOL manquant :",
    len(details) - len(available),
)

status_counts = (
    details["RVOLStatus"]
    .value_counts(dropna=False)
)

print()
print("Statut du calcul RVOL")
print(status_counts)


# ============================================================
# CLASSES RVOL
# ============================================================

class_rows = []

if not available.empty:
    available["RVOLClass"] = pd.cut(
        available["DecisionRVOL"],
        bins=[
            float("-inf"),
            1.0,
            1.2,
            1.5,
            2.0,
            float("inf"),
        ],
        labels=[
            "RVOL < 1.0",
            "1.0 <= RVOL < 1.2",
            "1.2 <= RVOL < 1.5",
            "1.5 <= RVOL < 2.0",
            "RVOL >= 2.0",
        ],
        right=False,
    )

    for rvol_class, group in available.groupby(
        "RVOLClass",
        observed=True,
    ):
        stats = calculate_metrics(
            group["NetReturnPct"]
        )

        class_rows.append(
            {
                "RVOLClass": str(rvol_class),
                "Trades": stats["Trades"],
                "MeanRVOL": group[
                    "DecisionRVOL"
                ].mean(),
                "WinRatePct": stats[
                    "WinRatePct"
                ],
                "ExpectancyPct": stats[
                    "ExpectancyPct"
                ],
                "MedianPct": stats[
                    "MedianPct"
                ],
                "ProfitFactor": stats[
                    "ProfitFactor"
                ],
                "MaxDrawdownPct": stats[
                    "MaxDrawdownPct"
                ],
                "TargetRatePct": (
                    group["TargetHit"].mean()
                    * 100.0
                ),
                "StopRatePct": (
                    group["StopHit"].mean()
                    * 100.0
                ),
            }
        )

classes = pd.DataFrame(class_rows)


# ============================================================
# SEUILS RVOL CUMULATIFS
# ============================================================

threshold_rows = []

for threshold in RVOL_THRESHOLDS:
    subset = available.loc[
        available["DecisionRVOL"]
        >= threshold
    ].copy()

    stats = calculate_metrics(
        subset["NetReturnPct"]
    )

    threshold_rows.append(
        {
            "MinimumRVOL": threshold,
            "Trades": stats["Trades"],
            "WinRatePct": stats[
                "WinRatePct"
            ],
            "ExpectancyPct": stats[
                "ExpectancyPct"
            ],
            "MedianPct": stats[
                "MedianPct"
            ],
            "ProfitFactor": stats[
                "ProfitFactor"
            ],
            "MaxDrawdownPct": stats[
                "MaxDrawdownPct"
            ],
        }
    )

thresholds = pd.DataFrame(
    threshold_rows
)


# ============================================================
# ACHAT CONFIRME VS ACHAT REACTIF
# ============================================================

decision_rows = []

for decision_type, group in available.groupby(
    "Decision"
):
    stats = calculate_metrics(
        group["NetReturnPct"]
    )

    decision_rows.append(
        {
            "Decision": decision_type,
            "Trades": stats["Trades"],
            "MeanRVOL": group[
                "DecisionRVOL"
            ].mean(),
            "WinRatePct": stats[
                "WinRatePct"
            ],
            "ExpectancyPct": stats[
                "ExpectancyPct"
            ],
            "ProfitFactor": stats[
                "ProfitFactor"
            ],
        }
    )

decisions = pd.DataFrame(
    decision_rows
)


# ============================================================
# SORTIE
# ============================================================

print()
print("=" * 140)
print("PERFORMANCE PAR CLASSE RVOL")
print("=" * 140)

if classes.empty:
    print("Aucune classe RVOL exploitable.")
else:
    print(
        classes
        .round(3)
        .to_string(index=False)
    )

print()
print("=" * 140)
print("COMPARAISON DES SEUILS RVOL")
print("=" * 140)

print(
    thresholds
    .round(3)
    .to_string(index=False)
)

print()
print("=" * 140)
print("RVOL PAR TYPE D'ACHAT")
print("=" * 140)

print(
    decisions
    .round(3)
    .to_string(index=False)
)

RESULTS_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

details.to_csv(
    OUTPUT_DETAILS,
    index=False,
    encoding="utf-8",
)

classes.to_csv(
    OUTPUT_CLASSES,
    index=False,
    encoding="utf-8",
)

thresholds.to_csv(
    OUTPUT_THRESHOLDS,
    index=False,
    encoding="utf-8",
)

print()
print("Fichiers générés :")
print("Détails :", OUTPUT_DETAILS)
print("Classes :", OUTPUT_CLASSES)
print("Seuils :", OUTPUT_THRESHOLDS)
print("=" * 120)
print("FIN DE L'ANALYSE RVOL")
print("=" * 120)
