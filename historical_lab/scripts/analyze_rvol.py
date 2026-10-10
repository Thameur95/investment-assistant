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
OUTPUT_DISTRIBUTION = RESULTS_DIR / "rvol_distribution_summary.csv"
OUTPUT_OUTLIERS = RESULTS_DIR / "rvol_outliers.csv"
OUTPUT_CONVICTION = RESULTS_DIR / "conviction_score_trades.csv"
OUTPUT_CONVICTION_SUMMARY = RESULTS_DIR / "conviction_score_summary.csv"

MIN_HISTORY_SESSIONS = 3
RVOL_THRESHOLDS = [1.0, 1.2, 1.5, 2.0]
OUTLIER_QUANTILE = 0.95

# Le score reste informatif. Il ne bloque aucun achat.
# Technique: signal déjà validé par le moteur.
# RVOL: bonus progressif, borné.
# Type: léger bonus au réactif, conformément aux résultats observés.
TECHNICAL_BASE_SCORE = 60.0
CONFIRMED_BONUS = 5.0
REACTIVE_BONUS = 10.0
RVOL_MAX_BONUS = 25.0


# ============================================================
# PREPARATION INTRADAY ET RVOL SANS FUITE D'INFORMATION
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

    for column in ["Open", "High", "Low", "Close", "Volume"]:
        result[column] = pd.to_numeric(
            result[column],
            errors="coerce",
        )

    result = (
        result
        .dropna(
            subset=[
                "Datetime_Paris",
                "Date_Paris",
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
            ]
        )
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )

    result["BarNumber"] = (
        result.groupby("Date_Paris").cumcount()
    )

    # Moyenne historique de la même bougie horaire.
    # shift(1) exclut la séance courante et évite la fuite d'information.
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


# ============================================================
# METRIQUES
# ============================================================

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


# ============================================================
# CHARGEMENT ET RATTACHEMENT DU RVOL
# ============================================================

def load_intraday_by_ticker():
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
# SCORE DE CONVICTION INFORMATIF
# ============================================================

def rvol_bonus(rvol):
    if pd.isna(rvol):
        return 0.0

    if rvol < 1.0:
        return 0.0

    if rvol < 1.2:
        return 5.0

    if rvol < 1.5:
        return 10.0

    if rvol < 2.0:
        return 17.5

    return RVOL_MAX_BONUS


def calculate_conviction(row):
    score = TECHNICAL_BASE_SCORE

    if row["Decision"] == "ACHAT CONFIRME":
        score += CONFIRMED_BONUS
    elif row["Decision"] == "ACHAT REACTIVE":
        score += REACTIVE_BONUS

    score += rvol_bonus(row["DecisionRVOL"])

    return min(100.0, max(0.0, score))


def conviction_label(score):
    if score >= 90:
        return "TRES FORTE"
    if score >= 80:
        return "FORTE"
    if score >= 70:
        return "MOYENNE"
    return "STANDARD"


# ============================================================
# EXECUTION
# ============================================================

print("=" * 120)
print("HISTORICAL LAB - ANALYSE RVOL ET SCORE DE CONVICTION")
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
        "Aucun trade exploitable."
    )

intraday_by_ticker = load_intraday_by_ticker()

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

print("Trades source :", len(details))
print("RVOL calculable :", len(available))
print("RVOL manquant :", len(details) - len(available))

print()
print("Statut du calcul RVOL")
print(
    details["RVOLStatus"]
    .value_counts(dropna=False)
)


# ============================================================
# DISTRIBUTION ET VALEURS EXTREMES
# ============================================================

distribution_rows = []
outliers = pd.DataFrame()

if not available.empty:
    rvol_series = available["DecisionRVOL"]

    distribution_rows.append(
        {
            "Trades": len(rvol_series),
            "Minimum": rvol_series.min(),
            "P25": rvol_series.quantile(0.25),
            "Median": rvol_series.median(),
            "Mean": rvol_series.mean(),
            "P75": rvol_series.quantile(0.75),
            "P90": rvol_series.quantile(0.90),
            "P95": rvol_series.quantile(0.95),
            "Maximum": rvol_series.max(),
        }
    )

    outlier_cutoff = rvol_series.quantile(
        OUTLIER_QUANTILE
    )

    outliers = (
        available.loc[
            available["DecisionRVOL"]
            >= outlier_cutoff
        ]
        .sort_values(
            "DecisionRVOL",
            ascending=False,
        )
    )

distribution = pd.DataFrame(
    distribution_rows
)


# ============================================================
# CLASSES RVOL
# ============================================================

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

class_rows = []

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
            "MeanRVOL": group["DecisionRVOL"].mean(),
            "MedianRVOL": group["DecisionRVOL"].median(),
            "WinRatePct": stats["WinRatePct"],
            "ExpectancyPct": stats["ExpectancyPct"],
            "MedianReturnPct": stats["MedianPct"],
            "ProfitFactor": stats["ProfitFactor"],
            "MaxDrawdownPct": stats["MaxDrawdownPct"],
            "TargetRatePct": group["TargetHit"].mean() * 100.0,
            "StopRatePct": group["StopHit"].mean() * 100.0,
        }
    )

classes = pd.DataFrame(class_rows)


# ============================================================
# SEUILS RVOL CUMULATIFS
# ============================================================

threshold_rows = []

for threshold in RVOL_THRESHOLDS:
    subset = available.loc[
        available["DecisionRVOL"] >= threshold
    ].copy()

    stats = calculate_metrics(
        subset["NetReturnPct"]
    )

    threshold_rows.append(
        {
            "MinimumRVOL": threshold,
            "Trades": stats["Trades"],
            "WinRatePct": stats["WinRatePct"],
            "ExpectancyPct": stats["ExpectancyPct"],
            "MedianPct": stats["MedianPct"],
            "ProfitFactor": stats["ProfitFactor"],
            "MaxDrawdownPct": stats["MaxDrawdownPct"],
        }
    )

thresholds = pd.DataFrame(
    threshold_rows
)


# ============================================================
# SCORE DE CONVICTION
# ============================================================

details["ConvictionScore"] = details.apply(
    calculate_conviction,
    axis=1,
)

details["ConvictionLabel"] = details[
    "ConvictionScore"
].apply(conviction_label)

conviction_rows = []

for label, group in details.groupby(
    "ConvictionLabel"
):
    stats = calculate_metrics(
        group["NetReturnPct"]
    )

    conviction_rows.append(
        {
            "ConvictionLabel": label,
            "Trades": stats["Trades"],
            "MeanScore": group[
                "ConvictionScore"
            ].mean(),
            "MeanRVOL": group[
                "DecisionRVOL"
            ].mean(),
            "WinRatePct": stats["WinRatePct"],
            "ExpectancyPct": stats["ExpectancyPct"],
            "ProfitFactor": stats["ProfitFactor"],
        }
    )

conviction_summary = pd.DataFrame(
    conviction_rows
).sort_values(
    "MeanScore",
    ascending=False,
)


# ============================================================
# AFFICHAGE
# ============================================================

pd.set_option("display.max_columns", None)
pd.set_option("display.width", 240)

print()
print("=" * 140)
print("DISTRIBUTION DU RVOL")
print("=" * 140)
print(
    distribution
    .round(3)
    .to_string(index=False)
)

print()
print("Valeurs RVOL extremes, P95 et plus")

if outliers.empty:
    print("Aucune valeur extrême.")
else:
    outlier_columns = [
        "Ticker",
        "DecisionTime",
        "Decision",
        "DecisionRVOL",
        "DecisionBarVolume",
        "HistoricalMeanVolumeSameBar",
        "NetReturnPct",
        "TargetHit",
        "StopHit",
    ]

    print(
        outliers[outlier_columns]
        .round(3)
        .to_string(index=False)
    )

print()
print("=" * 140)
print("PERFORMANCE PAR CLASSE RVOL")
print("=" * 140)
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
print("SCORE DE CONVICTION INFORMATIF")
print("=" * 140)
print(
    conviction_summary
    .round(3)
    .to_string(index=False)
)


# ============================================================
# SAUVEGARDE
# ============================================================

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

distribution.to_csv(
    OUTPUT_DISTRIBUTION,
    index=False,
    encoding="utf-8",
)

outliers.to_csv(
    OUTPUT_OUTLIERS,
    index=False,
    encoding="utf-8",
)

details.to_csv(
    OUTPUT_CONVICTION,
    index=False,
    encoding="utf-8",
)

conviction_summary.to_csv(
    OUTPUT_CONVICTION_SUMMARY,
    index=False,
    encoding="utf-8",
)

print()
print("Fichiers générés :")
print("Détails RVOL :", OUTPUT_DETAILS)
print("Classes RVOL :", OUTPUT_CLASSES)
print("Seuils RVOL :", OUTPUT_THRESHOLDS)
print("Distribution :", OUTPUT_DISTRIBUTION)
print("Valeurs extrêmes :", OUTPUT_OUTLIERS)
print("Score par trade :", OUTPUT_CONVICTION)
print("Résumé conviction :", OUTPUT_CONVICTION_SUMMARY)
print("=" * 120)
print("FIN DE L'ANALYSE RVOL")
print("=" * 120)
