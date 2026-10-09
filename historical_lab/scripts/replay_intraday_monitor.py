import pandas as pd
from pathlib import Path


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent

INTRADAY_DIR = (
    BASE_DIR
    / "data"
    / "intraday"
)

DAILY_FILE = (
    BASE_DIR
    / "data"
    / "processed"
    / "historical_indicators.csv"
)

OUTPUT_SUMMARY = (
    BASE_DIR
    / "atr_target_comparison.csv"
)

OUTPUT_TRADES = (
    BASE_DIR
    / "atr_target_trades.csv"
)

# Configuration retenue provisoirement
CONFIRMATION_BARS = 12
MAX_HORIZON = 3
SL_PCT = 2.0

# Benchmark fixe
FIXED_TP_PCT = 2.0

# TP dynamique :
# TP = ATR% × multiplicateur
# puis limité entre MIN_TP et MAX_TP
ATR_MULTIPLIERS = [
    0.75,
    1.00,
    1.25,
]

MIN_TP_PCT = 1.5
MAX_TP_PCT = 3.0


# ============================================================
# UTILITAIRES
# ============================================================

def prepare_intraday_data(df):
    required_columns = {
        "Datetime_Paris",
        "Date_Paris",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    }

    missing = required_columns - set(df.columns)

    if missing:
        raise ValueError(
            "Colonnes intraday manquantes : "
            + ", ".join(sorted(missing))
        )

    result = df.copy()

    result["Datetime_Paris"] = pd.to_datetime(
        result["Datetime_Paris"],
        errors="coerce"
    )

    result["Date_Paris"] = (
        result["Date_Paris"]
        .astype(str)
        .str[:10]
    )

    for column in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]:
        result[column] = pd.to_numeric(
            result[column],
            errors="coerce"
        )

    result = result.dropna(
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

    return (
        result
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )


def add_vwap(day):
    result = day.copy()

    typical_price = (
        result["High"]
        + result["Low"]
        + result["Close"]
    ) / 3

    cumulative_volume = (
        result["Volume"].cumsum()
    )

    cumulative_value = (
        typical_price
        * result["Volume"]
    ).cumsum()

    result["VWAP"] = (
        cumulative_value
        / cumulative_volume.replace(
            0,
            float("nan")
        )
    )

    return result


def calculate_atr(daily, period=14):
    """
    Calcule ATR14 et ATR14 en pourcentage du cours de clôture.
    """

    result = daily.copy()

    result["PreviousClose"] = (
        result["Close"].shift(1)
    )

    range_high_low = (
        result["High"]
        - result["Low"]
    )

    range_high_previous = (
        result["High"]
        - result["PreviousClose"]
    ).abs()

    range_low_previous = (
        result["Low"]
        - result["PreviousClose"]
    ).abs()

    result["TrueRange"] = pd.concat(
        [
            range_high_low,
            range_high_previous,
            range_low_previous,
        ],
        axis=1
    ).max(axis=1)

    result["ATR14"] = (
        result["TrueRange"]
        .rolling(
            window=period,
            min_periods=period
        )
        .mean()
    )

    result["ATR14Pct"] = (
        result["ATR14"]
        / result["Close"]
        * 100
    )

    return result


def calculate_metrics(values):
    series = pd.Series(
        values,
        dtype="float64"
    ).dropna()

    if series.empty:
        return {
            "Trades": 0,
            "Mean": None,
            "Median": None,
            "WinRate": None,
            "AverageWin": None,
            "AverageLoss": None,
            "ProfitFactor": None,
            "Expectancy": None,
        }

    winners = series[
        series > 0
    ]

    losers = series[
        series < 0
    ]

    gross_profit = float(
        winners.sum()
    )

    gross_loss = abs(
        float(losers.sum())
    )

    if gross_loss > 0:
        profit_factor = (
            gross_profit
            / gross_loss
        )
    elif gross_profit > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    return {
        "Trades": len(series),
        "Mean": float(series.mean()),
        "Median": float(series.median()),
        "WinRate": float(
            (series > 0).mean() * 100
        ),
        "AverageWin": (
            float(winners.mean())
            if not winners.empty
            else None
        ),
        "AverageLoss": (
            float(losers.mean())
            if not losers.empty
            else None
        ),
        "ProfitFactor": profit_factor,
        "Expectancy": float(
            series.mean()
        ),
    }


def calculate_dynamic_target(
    atr_pct,
    multiplier
):
    if atr_pct is None:
        return None

    if pd.isna(atr_pct):
        return None

    target = float(
        atr_pct
    ) * float(
        multiplier
    )

    target = max(
        MIN_TP_PCT,
        target
    )

    target = min(
        MAX_TP_PCT,
        target
    )

    return target


# ============================================================
# DETECTION DE L'ENTREE
# ============================================================

def detect_entry(
    ticker,
    session_date,
    day,
    signal_price,
    atr_pct
):
    day = (
        day
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
        .copy()
    )

    if len(day) < 4:
        return None

    signal_price = float(
        signal_price
    )

    if signal_price <= 0:
        return None

    opening = float(
        day["Open"].iloc[0]
    )

    if opening <= 0:
        return None

    gap_pct = (
        (
            opening
            / signal_price
        )
        - 1
    ) * 100

    first4 = day.iloc[:4].copy()

    or_high = float(
        first4["High"].max()
    )

    or_low = float(
        first4["Low"].min()
    )

    close4 = float(
        first4["Close"].iloc[-1]
    )

    red4 = int(
        (
            first4["Close"]
            < first4["Open"]
        ).sum()
    )

    initial_volumes = first4.loc[
        first4["Volume"] > 0,
        "Volume"
    ]

    if initial_volumes.empty:
        initial_average_volume = 0.0
    else:
        initial_average_volume = float(
            initial_volumes.mean()
        )

    day = add_vwap(day)

    decision = "EN ATTENTE"
    reason = "Prix dans Opening Range"

    decision_time = None
    decision_price = None
    decision_volume = None
    decision_vwap = None

    invalid_position = 3

    # ========================================================
    # INVALIDATIONS INITIALES
    # ========================================================

    if gap_pct <= -3:

        decision = "SETUP INITIAL INVALIDE"
        reason = "Gap baissier >= 3%"

    elif close4 <= opening * 0.98:

        decision = "SETUP INITIAL INVALIDE"
        reason = "Baisse >= 2% apres 20 minutes"

    elif (
        red4 >= 3
        and close4 < opening
    ):

        decision = "SETUP INITIAL INVALIDE"
        reason = "Au moins 3 bougies rouges sur 4"

    else:

        confirmation_window = (
            day.iloc[
                :CONFIRMATION_BARS
            ].copy()
        )

        for position, row in (
            confirmation_window
            .iloc[4:]
            .iterrows()
        ):

            current_low = float(
                row["Low"]
            )

            current_close = float(
                row["Close"]
            )

            current_volume = float(
                row["Volume"]
            )

            current_vwap = row["VWAP"]

            if pd.isna(current_vwap):
                continue

            current_vwap = float(
                current_vwap
            )

            if (
                current_low < or_low
                or current_close
                <= opening * 0.98
            ):

                decision = "SETUP INITIAL INVALIDE"

                reason = (
                    "Cassure baissiere "
                    "Opening Range"
                )

                invalid_position = int(
                    position
                )

                break

            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume
                >= initial_average_volume
            ):

                decision = "ACHAT CONFIRME"

                reason = (
                    "Cassure haussiere "
                    "+ VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

                decision_price = (
                    current_close
                )

                decision_volume = (
                    current_volume
                )

                decision_vwap = (
                    current_vwap
                )

                break

        if decision == "EN ATTENTE":

            if (
                len(day)
                >= CONFIRMATION_BARS
            ):

                decision = "SETUP INITIAL INVALIDE"

                reason = (
                    "Aucune confirmation "
                    "pendant 60 minutes"
                )

                invalid_position = (
                    CONFIRMATION_BARS - 1
                )

    # ========================================================
    # ACHAT REACTIF
    # ========================================================

    if decision == "SETUP INITIAL INVALIDE":

        reactive_start = (
            invalid_position + 1
        )

        for _, row in (
            day
            .iloc[reactive_start:]
            .iterrows()
        ):

            current_close = float(
                row["Close"]
            )

            current_volume = float(
                row["Volume"]
            )

            current_vwap = row["VWAP"]

            if pd.isna(current_vwap):
                continue

            current_vwap = float(
                current_vwap
            )

            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume
                >= initial_average_volume * 1.5
            ):

                decision = "ACHAT REACTIVE"

                reason = (
                    "Retournement confirme : "
                    "OR High + VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

                decision_price = (
                    current_close
                )

                decision_volume = (
                    current_volume
                )

                decision_vwap = (
                    current_vwap
                )

                break

    is_buy = decision in [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE",
    ]

    move_before_entry_pct = None

    if (
        is_buy
        and decision_price is not None
    ):

        move_before_entry_pct = (
            (
                decision_price
                / opening
            )
            - 1
        ) * 100

    return {
        "Ticker": ticker,
        "SessionDate": session_date,
        "SignalPrice": signal_price,
        "OpeningPrice": opening,
        "GapPct": gap_pct,
        "ORHigh": or_high,
        "ORLow": or_low,
        "ATR14Pct": atr_pct,
        "Decision": decision,
        "Reason": reason,
        "DecisionTime": decision_time,
        "DecisionPrice": decision_price,
        "DecisionVolume": decision_volume,
        "DecisionVWAP": decision_vwap,
        "MoveBeforeEntryPct":
            move_before_entry_pct,
    }


# ============================================================
# SIMULATION JUSQU'A J+3
# ============================================================

def simulate_trade(
    full_intraday,
    entry_date,
    entry_time,
    entry_price,
    target_pct
):
    if entry_time is None:
        return None

    if entry_price is None:
        return None

    entry_time = pd.Timestamp(
        entry_time
    )

    entry_price = float(
        entry_price
    )

    if entry_price <= 0:
        return None

    target_price = (
        entry_price
        * (1 + target_pct / 100)
    )

    stop_price = (
        entry_price
        * (1 - SL_PCT / 100)
    )

    session_dates = sorted(
        full_intraday[
            "Date_Paris"
        ].unique()
    )

    if entry_date not in session_dates:
        return None

    entry_position = (
        session_dates.index(
            entry_date
        )
    )

    forced_exit_position = (
        entry_position
        + MAX_HORIZON
    )

    if (
        forced_exit_position
        >= len(session_dates)
    ):
        return {
            "TradeStatus": "DONNEES INCOMPLETES",
            "ExitReason": "J+3 indisponible",
            "ExitTime": None,
            "ExitPrice": None,
            "ReturnPct": None,
            "TargetHit": False,
            "StopHit": False,
            "GapExit": False,
            "AmbiguousBar": False,
            "HoldingSession": None,
        }

    forced_exit_date = session_dates[
        forced_exit_position
    ]

    trade_data = full_intraday.loc[
        (
            full_intraday[
                "Datetime_Paris"
            ] > entry_time
        )
        &
        (
            full_intraday[
                "Date_Paris"
            ] <= forced_exit_date
        )
    ].copy()

    if trade_data.empty:
        return {
            "TradeStatus": "DONNEES INCOMPLETES",
            "ExitReason": "Aucune bougie apres entree",
            "ExitTime": None,
            "ExitPrice": None,
            "ReturnPct": None,
            "TargetHit": False,
            "StopHit": False,
            "GapExit": False,
            "AmbiguousBar": False,
            "HoldingSession": None,
        }

    exit_reason = None
    exit_time = None
    exit_price = None

    target_hit = False
    stop_hit = False
    gap_exit = False
    ambiguous_bar = False
    holding_session = None

    for session_date, session in (
        trade_data.groupby(
            "Date_Paris",
            sort=True
        )
    ):

        session = (
            session
            .sort_values("Datetime_Paris")
            .reset_index(drop=True)
        )

        if session.empty:
            continue

        session_number = (
            session_dates.index(
                session_date
            )
            - entry_position
        )

        if session_number == 0:
            session_label = "J"
        else:
            session_label = (
                f"J+{session_number}"
            )

        first_bar = session.iloc[0]

        # ====================================================
        # GAP A L'OUVERTURE
        # ====================================================

        session_open = float(
            first_bar["Open"]
        )

        if session_open <= stop_price:

            exit_reason = "GAP SOUS STOP"
            exit_time = first_bar[
                "Datetime_Paris"
            ]
            exit_price = session_open
            stop_hit = True
            gap_exit = True
            holding_session = session_label

            break

        if session_open >= target_price:

            exit_reason = "GAP AU-DESSUS TARGET"
            exit_time = first_bar[
                "Datetime_Paris"
            ]
            exit_price = session_open
            target_hit = True
            gap_exit = True
            holding_session = session_label

            break

        # ====================================================
        # TP / SL BOUGIE PAR BOUGIE
        # ====================================================

        for _, row in session.iterrows():

            current_high = float(
                row["High"]
            )

            current_low = float(
                row["Low"]
            )

            hit_target = (
                current_high >= target_price
            )

            hit_stop = (
                current_low <= stop_price
            )

            if hit_target and hit_stop:

                exit_reason = (
                    "TP ET SL MEME BOUGIE "
                    "- SL PRIORITAIRE"
                )

                exit_time = row[
                    "Datetime_Paris"
                ]

                exit_price = stop_price
                stop_hit = True
                ambiguous_bar = True
                holding_session = session_label

            elif hit_stop:

                exit_reason = "STOP -2%"

                exit_time = row[
                    "Datetime_Paris"
                ]

                exit_price = stop_price
                stop_hit = True
                holding_session = session_label

            elif hit_target:

                exit_reason = (
                    f"TARGET +{target_pct:.3f}%"
                )

                exit_time = row[
                    "Datetime_Paris"
                ]

                exit_price = target_price
                target_hit = True
                holding_session = session_label

            if exit_reason is not None:
                break

        if exit_reason is not None:
            break

    # ========================================================
    # SORTIE TEMPORELLE J+3
    # ========================================================

    if exit_reason is None:

        forced_data = trade_data.loc[
            trade_data["Date_Paris"]
            == forced_exit_date
        ]

        if forced_data.empty:

            return {
                "TradeStatus": "DONNEES INCOMPLETES",
                "ExitReason": "Cloture J+3 indisponible",
                "ExitTime": None,
                "ExitPrice": None,
                "ReturnPct": None,
                "TargetHit": False,
                "StopHit": False,
                "GapExit": False,
                "AmbiguousBar": False,
                "HoldingSession": None,
            }

        last_bar = forced_data.iloc[-1]

        exit_reason = "SORTIE TEMPORELLE J+3"

        exit_time = last_bar[
            "Datetime_Paris"
        ]

        exit_price = float(
            last_bar["Close"]
        )

        holding_session = "J+3"

    return_pct = (
        (
            exit_price
            / entry_price
        )
        - 1
    ) * 100

    return {
        "TradeStatus": "TERMINE",
        "ExitReason": exit_reason,
        "ExitTime": exit_time,
        "ExitPrice": exit_price,
        "ReturnPct": return_pct,
        "TargetHit": target_hit,
        "StopHit": stop_hit,
        "GapExit": gap_exit,
        "AmbiguousBar": ambiguous_bar,
        "HoldingSession": holding_session,
    }


# ============================================================
# CHARGEMENT DES DONNEES DAILY
# ============================================================

print("=" * 120)
print("HISTORICAL LAB")
print("TP FIXE 2% VS TP DYNAMIQUE ATR")
print("FENETRE 60 MIN / SL -2% / HORIZON J+3")
print("=" * 120)

daily_all = pd.read_csv(
    DAILY_FILE
)

required_daily_columns = {
    "Date",
    "Ticker",
    "Open",
    "High",
    "Low",
    "Close",
    "Signal",
}

missing_daily_columns = (
    required_daily_columns
    - set(daily_all.columns)
)

if missing_daily_columns:

    print(
        "Colonnes quotidiennes manquantes :",
        sorted(missing_daily_columns)
    )

    raise SystemExit(1)

daily_all["Date"] = pd.to_datetime(
    daily_all["Date"],
    errors="coerce"
)

for column in [
    "Open",
    "High",
    "Low",
    "Close",
]:
    daily_all[column] = pd.to_numeric(
        daily_all[column],
        errors="coerce"
    )


# ============================================================
# CHARGEMENT DES DONNEES INTRADAY
# ============================================================

intraday_by_ticker = {}

for intraday_file in sorted(
    INTRADAY_DIR.glob("*_5m.csv")
):

    ticker = (
        intraday_file.stem
        .replace("_5m", "")
    )

    try:

        intraday_df = pd.read_csv(
            intraday_file
        )

        intraday_by_ticker[ticker] = (
            prepare_intraday_data(
                intraday_df
            )
        )

    except Exception as error:

        print(
            ticker,
            "| erreur intraday :",
            str(error)
        )


# ============================================================
# DETECTION DES ENTREES
# ============================================================

entries = []

for ticker, intraday_df in (
    intraday_by_ticker.items()
):

    daily = daily_all.loc[
        daily_all["Ticker"] == ticker
    ].copy()

    if daily.empty:
        continue

    daily = (
        daily
        .dropna(
            subset=[
                "Date",
                "High",
                "Low",
                "Close",
            ]
        )
        .sort_values("Date")
        .drop_duplicates(
            subset="Date",
            keep="last"
        )
        .set_index(drop=True)
    )

    if daily.empty:
        continue

    daily = calculate_atr(
        daily,
        period=14
    )

    daily["SessionDate"] = (
        daily["Date"]
        .shift(-1)
        .dt.strftime("%Y-%m-%d")
    )

    signal_rows = daily.loc[
        daily["Signal"] == "ACHAT"
    ].copy()

    candidate_sessions = set(
        signal_rows[
            "SessionDate"
        ].dropna()
    )

    signal_price_by_session = (
        signal_rows
        .dropna(
            subset=[
                "SessionDate",
                "Close",
            ]
        )
        .drop_duplicates(
            subset="SessionDate",
            keep="last"
        )
        .set_index(
            "SessionDate"
        )["Close"]
        .to_dict()
    )

    atr_by_session = (
        signal_rows
        .dropna(
            subset=[
                "SessionDate",
                "ATR14Pct",
            ]
        )
        .drop_duplicates(
            subset="SessionDate",
            keep="last"
        )
        .set_index(
            "SessionDate"
        )["ATR14Pct"]
        .to_dict()
    )

    for session_date, day in (
        intraday_df.groupby(
            "Date_Paris"
        )
    ):

        session_date = str(
            session_date
        )[:10]

        if (
            session_date
            not in candidate_sessions
        ):
            continue

        signal_price = (
            signal_price_by_session
            .get(session_date)
        )

        atr_pct = atr_by_session.get(
            session_date
        )

        if signal_price is None:
            continue

        entry = detect_entry(
            ticker=ticker,
            session_date=session_date,
            day=day,
            signal_price=signal_price,
            atr_pct=atr_pct
        )

        if entry is None:
            continue

        if entry["Decision"] in [
            "ACHAT CONFIRME",
            "ACHAT REACTIVE",
        ]:
            entries.append(entry)

print(
    "Entrées détectées :",
    len(entries)
)


# ============================================================
# DEFINITIONS DES STRATEGIES DE TARGET
# ============================================================

target_strategies = [
    {
        "Strategy": "FIXED_2.0",
        "Mode": "FIXED",
        "Multiplier": None,
    }
]

for multiplier in ATR_MULTIPLIERS:

    target_strategies.append(
        {
            "Strategy": (
                f"ATR_X_{multiplier:.2f}"
            ),
            "Mode": "ATR",
            "Multiplier": multiplier,
        }
    )


# ============================================================
# SIMULATION
# ============================================================

all_trades = []

for strategy in target_strategies:

    for entry in entries:

        ticker = entry["Ticker"]

        if strategy["Mode"] == "FIXED":

            target_pct = FIXED_TP_PCT

        else:

            target_pct = (
                calculate_dynamic_target(
                    atr_pct=entry[
                        "ATR14Pct"
                    ],
                    multiplier=strategy[
                        "Multiplier"
                    ]
                )
            )

        if target_pct is None:
            continue

        trade = simulate_trade(
            full_intraday=(
                intraday_by_ticker[
                    ticker
                ]
            ),
            entry_date=entry[
                "SessionDate"
            ],
            entry_time=entry[
                "DecisionTime"
            ],
            entry_price=entry[
                "DecisionPrice"
            ],
            target_pct=target_pct
        )

        if trade is None:
            continue

        all_trades.append(
            {
                **entry,
                **trade,
                "TargetStrategy":
                    strategy["Strategy"],
                "TargetMode":
                    strategy["Mode"],
                "ATRMultiplier":
                    strategy["Multiplier"],
                "TargetPct":
                    target_pct,
                "SLPct":
                    SL_PCT,
                "Horizon":
                    MAX_HORIZON,
                "WeekendPolicy":
                    "HOLD_WEEKEND",
            }
        )


# ============================================================
# CONSOLIDATION
# ============================================================

if not all_trades:

    print(
        "Aucun trade simulé."
    )

    raise SystemExit(0)

trades = pd.DataFrame(
    all_trades
)

trades.to_csv(
    OUTPUT_TRADES,
    index=False,
    encoding="utf-8"
)

summary_rows = []


# ============================================================
# STATISTIQUES PAR STRATEGIE
# ============================================================

for strategy_name, group in (
    trades.groupby(
        "TargetStrategy"
    )
):

    completed = group.loc[
        group["TradeStatus"]
        == "TERMINE"
    ].copy()

    incomplete = group.loc[
        group["TradeStatus"]
        == "DONNEES INCOMPLETES"
    ].copy()

    if completed.empty:
        continue

    metrics = calculate_metrics(
        completed["ReturnPct"]
    )

    targets = int(
        completed["TargetHit"].sum()
    )

    stops = int(
        completed["StopHit"].sum()
    )

    gap_exits = int(
        completed["GapExit"].sum()
    )

    ambiguous = int(
        completed["AmbiguousBar"].sum()
    )

    temporal_exits = int(
        (
            completed["ExitReason"]
            == "SORTIE TEMPORELLE J+3"
        ).sum()
    )

    summary_rows.append(
        {
            "TargetStrategy":
                strategy_name,

            "Trades":
                len(completed),

            "Incomplete":
                len(incomplete),

            "MeanTargetPct":
                completed[
                    "TargetPct"
                ].mean(),

            "MedianTargetPct":
                completed[
                    "TargetPct"
                ].median(),

            "MinTargetPct":
                completed[
                    "TargetPct"
                ].min(),

            "MaxTargetPct":
                completed[
                    "TargetPct"
                ].max(),

            "Targets":
                targets,

            "TargetRatePct":
                targets
                / len(completed)
                * 100,

            "Stops":
                stops,

            "StopRatePct":
                stops
                / len(completed)
                * 100,

            "TemporalExits":
                temporal_exits,

            "TemporalExitRatePct":
                temporal_exits
                / len(completed)
                * 100,

            "WinRatePct":
                metrics["WinRate"],

            "PerfMeanPct":
                metrics["Mean"],

            "PerfMedianPct":
                metrics["Median"],

            "AverageWinPct":
                metrics["AverageWin"],

            "AverageLossPct":
                metrics["AverageLoss"],

            "ProfitFactor":
                metrics["ProfitFactor"],

            "ExpectancyPct":
                metrics["Expectancy"],

            "MoveBeforeEntryPct":
                completed[
                    "MoveBeforeEntryPct"
                ].mean(),

            "AverageATRPct":
                completed[
                    "ATR14Pct"
                ].mean(),

            "GapExits":
                gap_exits,

            "AmbiguousBars":
                ambiguous,
        }
    )


# ============================================================
# TABLEAU FINAL
# ============================================================

summary = pd.DataFrame(
    summary_rows
)

summary["ProfitFactorNumeric"] = (
    pd.to_numeric(
        summary["ProfitFactor"],
        errors="coerce"
    )
)

summary = summary.sort_values(
    by=[
        "ExpectancyPct",
        "ProfitFactorNumeric",
        "WinRatePct",
    ],
    ascending=[
        False,
        False,
        False,
    ]
)

summary.to_csv(
    OUTPUT_SUMMARY,
    index=False,
    encoding="utf-8"
)

display_columns = [
    "TargetStrategy",
    "Trades",
    "Incomplete",
    "MeanTargetPct",
    "MedianTargetPct",
    "MinTargetPct",
    "MaxTargetPct",
    "Targets",
    "TargetRatePct",
    "Stops",
    "StopRatePct",
    "TemporalExits",
    "TemporalExitRatePct",
    "WinRatePct",
    "PerfMeanPct",
    "PerfMedianPct",
    "AverageWinPct",
    "AverageLossPct",
    "ProfitFactor",
    "ExpectancyPct",
    "AverageATRPct",
    "MoveBeforeEntryPct",
    "GapExits",
]

for column in [
    "MeanTargetPct",
    "MedianTargetPct",
    "MinTargetPct",
    "MaxTargetPct",
    "TargetRatePct",
    "StopRatePct",
    "TemporalExitRatePct",
    "WinRatePct",
    "PerfMeanPct",
    "PerfMedianPct",
    "AverageWinPct",
    "AverageLossPct",
    "ProfitFactor",
    "ExpectancyPct",
    "AverageATRPct",
    "MoveBeforeEntryPct",
]:
    if column in summary.columns:

        summary[column] = pd.to_numeric(
            summary[column],
            errors="coerce"
        ).round(3)

pd.set_option(
    "display.max_columns",
    None
)

pd.set_option(
    "display.width",
    300
)

print()
print("=" * 180)
print("COMPARAISON TP FIXE VS TP DYNAMIQUE ATR")
print("=" * 180)

print(
    summary[
        display_columns
    ].to_string(
        index=False
    )
)


# ============================================================
# DETAIL PAR TYPE D'ACHAT
# ============================================================

print()
print("=" * 180)
print("DETAIL PAR TYPE D'ACHAT")
print("=" * 180)

for strategy_name in (
    summary["TargetStrategy"]
):

    print()
    print(
        f"--- {strategy_name} ---"
    )

    strategy_trades = trades.loc[
        (
            trades["TargetStrategy"]
            == strategy_name
        )
        &
        (
            trades["TradeStatus"]
            == "TERMINE"
        )
    ].copy()

    for decision_type in [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE",
    ]:

        subset = strategy_trades.loc[
            strategy_trades["Decision"]
            == decision_type
        ].copy()

        if subset.empty:
            continue

        metrics = calculate_metrics(
            subset["ReturnPct"]
        )

        targets = int(
            subset["TargetHit"].sum()
        )

        stops = int(
            subset["StopHit"].sum()
        )

        print()
        print(
            decision_type,
            "| Trades :",
            len(subset)
        )

        print(
            "Target atteint :",
            targets,
            f"({targets / len(subset) * 100:.1f} %)"
        )

        print(
            "Stop atteint :",
            stops,
            f"({stops / len(subset) * 100:.1f} %)"
        )

        print(
            "Target moyen :",
            round(
                subset[
                    "TargetPct"
                ].mean(),
                3
            ),
            "%"
        )

        print(
            "Performance moyenne :",
            round(
                metrics["Mean"],
                3
            ),
            "%"
        )

        print(
            "Win rate :",
            round(
                metrics["WinRate"],
                1
            ),
            "%"
        )

        print(
            "Profit Factor :",
            (
                round(
                    metrics["ProfitFactor"],
                    3
                )
                if (
                    metrics["ProfitFactor"]
                    is not None
                    and metrics["ProfitFactor"]
                    != float("inf")
                )
                else metrics["ProfitFactor"]
            )
        )


# ============================================================
# DETAIL PAR CLASSE DE VOLATILITE
# ============================================================

print()
print("=" * 180)
print("PERFORMANCE PAR CLASSE DE VOLATILITE")
print("=" * 180)

completed_trades = trades.loc[
    trades["TradeStatus"] == "TERMINE"
].copy()

completed_trades["ATRClass"] = pd.cut(
    completed_trades["ATR14Pct"],
    bins=[
        float("-inf"),
        1.5,
        2.5,
        float("inf")
    ],
    labels=[
        "ATR faible < 1.5%",
        "ATR moyen 1.5%-2.5%",
        "ATR fort > 2.5%"
    ]
)

volatility_stats = (
    completed_trades
    .groupby(
        [
            "TargetStrategy",
            "ATRClass"
        ],
        observed=True
    )
    .agg(
        Trades=(
            "Ticker",
            "count"
        ),
        TargetMoyen=(
            "TargetPct",
            "mean"
        ),
        PerformanceMoyenne=(
            "ReturnPct",
            "mean"
        ),
        PerformanceMediane=(
            "ReturnPct",
            "median"
        ),
        WinRate=(
            "ReturnPct",
            lambda values: (
                values > 0
            ).mean() * 100
        ),
        Targets=(
            "TargetHit",
            "sum"
        ),
        Stops=(
            "StopHit",
            "sum"
        )
    )
    .reset_index()
)

for column in [
    "TargetMoyen",
    "PerformanceMoyenne",
    "PerformanceMediane",
    "WinRate",
]:
    volatility_stats[column] = (
        volatility_stats[column]
        .round(3)
    )

print(
    volatility_stats.to_string(
        index=False
    )
)


# ============================================================
# FIN
# ============================================================

print()
print(
    "Résumé sauvegardé :",
    OUTPUT_SUMMARY
)

print(
    "Détail des trades sauvegardé :",
    OUTPUT_TRADES
)

print()
print("=" * 180)
print("FIN DU TEST ATR")
print("=" * 180)
