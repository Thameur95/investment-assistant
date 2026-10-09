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
    / "tp_horizon_weekend_comparison.csv"
)

OUTPUT_TRADES = (
    BASE_DIR
    / "tp_horizon_weekend_trades.csv"
)

# Fenêtre maximale pour obtenir une confirmation.
# Le moteur achète immédiatement quand les conditions sont remplies.
ENTRY_WINDOWS = {
    "60 min": 12,
    "90 min": 18,
    "120 min": 24,
    "Toute seance": None,
}

# Objectifs à comparer.
TP_LEVELS = [
    1.5,
    2.0,
    2.5,
    3.0,
]

# Stop provisoirement fixé à -2 %.
SL_PCT = 2.0

# Sortie forcée à J+1, J+2 ou J+3.
MAX_HORIZONS = [
    1,
    2,
    3,
]

# HOLD_WEEKEND :
# autorise le maintien pendant le week-end.
#
# FRIDAY_FLAT :
# ferme la position le vendredi à la clôture
# si elle est toujours ouverte.
WEEKEND_POLICIES = [
    "HOLD_WEEKEND",
    "FRIDAY_FLAT",
]


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
            "Colonnes manquantes : "
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


# ============================================================
# DETECTION DE L'ENTREE
# ============================================================

def detect_entry(
    ticker,
    session_date,
    day,
    signal_price,
    max_bars
):
    day = (
        day
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
        .copy()
    )

    if len(day) < 4:
        return None

    signal_price = float(signal_price)

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

        if max_bars is None:
            confirmation_window = day.copy()
        else:
            confirmation_window = (
                day.iloc[:max_bars].copy()
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

            window_finished = (
                max_bars is None
                or len(day) >= max_bars
            )

            if window_finished:

                decision = "SETUP INITIAL INVALIDE"

                if max_bars is None:

                    reason = (
                        "Aucune confirmation "
                        "pendant la seance"
                    )

                    invalid_position = (
                        len(day) - 1
                    )

                else:

                    reason = (
                        "Aucune confirmation "
                        f"pendant {max_bars * 5} minutes"
                    )

                    invalid_position = (
                        max_bars - 1
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

    move_before_entry = None

    if (
        is_buy
        and decision_price is not None
    ):

        move_before_entry = (
            (
                decision_price
                / opening
            )
            - 1
        ) * 100

    return {
        "Ticker": ticker,
        "SessionDate": session_date,
        "OpeningPrice": opening,
        "GapPct": gap_pct,
        "ORHigh": or_high,
        "ORLow": or_low,
        "Decision": decision,
        "Reason": reason,
        "DecisionTime": decision_time,
        "DecisionPrice": decision_price,
        "DecisionVolume": decision_volume,
        "DecisionVWAP": decision_vwap,
        "MoveBeforeEntryPct": move_before_entry,
    }


# ============================================================
# SIMULATION DU TRADE
# ============================================================

def simulate_trade(
    full_intraday,
    entry_date,
    entry_time,
    entry_price,
    tp_pct,
    sl_pct,
    max_horizon,
    weekend_policy
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
        * (1 + tp_pct / 100)
    )

    stop_price = (
        entry_price
        * (1 - sl_pct / 100)
    )

    session_dates = sorted(
        full_intraday[
            "Date_Paris"
        ].unique()
    )

    if entry_date not in session_dates:
        return None

    entry_session_position = (
        session_dates.index(
            entry_date
        )
    )

    forced_exit_position = (
        entry_session_position
        + max_horizon
    )

    if (
        forced_exit_position
        >= len(session_dates)
    ):
        return {
            "TradeStatus": "DONNEES INCOMPLETES",
            "ExitReason": "Horizon indisponible",
            "ExitTime": None,
            "ExitPrice": None,
            "ReturnPct": None,
            "TargetHit": False,
            "StopHit": False,
            "HoldingSession": None,
            "WeekendExit": False,
            "GapExit": False,
            "AmbiguousBar": False,
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
            "HoldingSession": None,
            "WeekendExit": False,
            "GapExit": False,
            "AmbiguousBar": False,
        }

    exit_reason = None
    exit_time = None
    exit_price = None
    holding_session = None

    target_hit = False
    stop_hit = False
    weekend_exit = False
    gap_exit = False
    ambiguous_bar = False

    previous_date = None

    for session_date, session in trade_data.groupby(
        "Date_Paris",
        sort=True
    ):

        session = (
            session
            .sort_values("Datetime_Paris")
            .reset_index(drop=True)
        )

        if session.empty:
            continue

        first_bar = session.iloc[0]

        is_new_session = (
            previous_date is None
            or session_date != previous_date
        )

        # ====================================================
        # GESTION DU GAP D'OUVERTURE
        # ====================================================

        if is_new_session:

            session_open = float(
                first_bar["Open"]
            )

            if session_open <= stop_price:

                exit_reason = "GAP SOUS STOP"

                exit_time = first_bar[
                    "Datetime_Paris"
                ]

                # Exécution au premier prix disponible.
                exit_price = session_open

                stop_hit = True
                gap_exit = True

            elif session_open >= target_price:

                exit_reason = "GAP AU-DESSUS TARGET"

                exit_time = first_bar[
                    "Datetime_Paris"
                ]

                exit_price = session_open

                target_hit = True
                gap_exit = True

            if exit_reason is not None:
                holding_session = (
                    "J"
                    if session_date == entry_date
                    else (
                        "J+"
                        + str(
                            session_dates.index(
                                session_date
                            )
                            - entry_session_position
                        )
                    )
                )

                break

        # ====================================================
        # TEST TP / SL BOUGIE PAR BOUGIE
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

                # Les données 5 minutes ne donnent pas
                # l'ordre exact intrabougie.
                # Hypothèse prudente : stop en premier.
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

            elif hit_stop:

                exit_reason = (
                    f"STOP -{sl_pct:.1f}%"
                )

                exit_time = row[
                    "Datetime_Paris"
                ]

                exit_price = stop_price
                stop_hit = True

            elif hit_target:

                exit_reason = (
                    f"TARGET +{tp_pct:.1f}%"
                )

                exit_time = row[
                    "Datetime_Paris"
                ]

                exit_price = target_price
                target_hit = True

            if exit_reason is not None:

                holding_session = (
                    "J"
                    if session_date == entry_date
                    else (
                        "J+"
                        + str(
                            session_dates.index(
                                session_date
                            )
                            - entry_session_position
                        )
                    )
                )

                break

        if exit_reason is not None:
            break

        # ====================================================
        # SORTIE OBLIGATOIRE LE VENDREDI
        # ====================================================

        session_timestamp = pd.Timestamp(
            session_date
        )

        if (
            weekend_policy == "FRIDAY_FLAT"
            and session_timestamp.weekday() == 4
        ):

            last_bar = session.iloc[-1]

            exit_reason = "SORTIE VENDREDI"

            exit_time = last_bar[
                "Datetime_Paris"
            ]

            exit_price = float(
                last_bar["Close"]
            )

            weekend_exit = True

            holding_session = (
                "J"
                if session_date == entry_date
                else (
                    "J+"
                    + str(
                        session_dates.index(
                            session_date
                        )
                        - entry_session_position
                    )
                )
            )

            break

        previous_date = session_date

    # ========================================================
    # SORTIE TEMPORELLE
    # ========================================================

    if exit_reason is None:

        forced_data = trade_data.loc[
            trade_data["Date_Paris"]
            == forced_exit_date
        ]

        if forced_data.empty:
            return {
                "TradeStatus": "DONNEES INCOMPLETES",
                "ExitReason": "Cloture horizon indisponible",
                "ExitTime": None,
                "ExitPrice": None,
                "ReturnPct": None,
                "TargetHit": False,
                "StopHit": False,
                "HoldingSession": None,
                "WeekendExit": False,
                "GapExit": False,
                "AmbiguousBar": False,
            }

        last_bar = forced_data.iloc[-1]

        exit_reason = (
            f"SORTIE TEMPORELLE J+{max_horizon}"
        )

        exit_time = last_bar[
            "Datetime_Paris"
        ]

        exit_price = float(
            last_bar["Close"]
        )

        holding_session = (
            f"J+{max_horizon}"
        )

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
        "HoldingSession": holding_session,
        "WeekendExit": weekend_exit,
        "GapExit": gap_exit,
        "AmbiguousBar": ambiguous_bar,
    }


# ============================================================
# CHARGEMENT DES DONNEES
# ============================================================

print("=" * 100)
print("HISTORICAL LAB")
print("OPTIMISATION TP / HORIZON / WEEK-END")
print("=" * 100)

daily_all = pd.read_csv(
    DAILY_FILE
)

daily_all["Date"] = pd.to_datetime(
    daily_all["Date"],
    errors="coerce"
)

daily_all["Close"] = pd.to_numeric(
    daily_all["Close"],
    errors="coerce"
)

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
            "| erreur :",
            str(error)
        )


# ============================================================
# DETECTION DES ENTREES POUR CHAQUE FENETRE
# ============================================================

entries_by_window = {}

for window_name, max_bars in (
    ENTRY_WINDOWS.items()
):

    print()
    print(
        "Détection des entrées :",
        window_name
    )

    detected_entries = []

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
                    "Close"
                ]
            )
            .sort_values("Date")
            .reset_index(drop=True)
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
                    "Close"
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

        for session_date, day in (
            intraday_df.groupby("Date_Paris")
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

            if signal_price is None:
                continue

            entry_result = detect_entry(
                ticker=ticker,
                session_date=session_date,
                day=day,
                signal_price=signal_price,
                max_bars=max_bars
            )

            if entry_result is None:
                continue

            if entry_result["Decision"] in [
                "ACHAT CONFIRME",
                "ACHAT REACTIVE",
            ]:
                detected_entries.append(
                    entry_result
                )

    entries_by_window[
        window_name
    ] = detected_entries

    print(
        "Entrées détectées :",
        len(detected_entries)
    )


# ============================================================
# OPTIMISATION DES PARAMETRES
# ============================================================

all_trade_rows = []
summary_rows = []

for window_name, entries in (
    entries_by_window.items()
):

    for tp_pct in TP_LEVELS:

        for max_horizon in MAX_HORIZONS:

            for weekend_policy in (
                WEEKEND_POLICIES
            ):

                configuration_trades = []

                for entry in entries:

                    ticker = entry["Ticker"]

                    intraday_df = (
                        intraday_by_ticker[
                            ticker
                        ]
                    )

                    trade = simulate_trade(
                        full_intraday=intraday_df,
                        entry_date=entry[
                            "SessionDate"
                        ],
                        entry_time=entry[
                            "DecisionTime"
                        ],
                        entry_price=entry[
                            "DecisionPrice"
                        ],
                        tp_pct=tp_pct,
                        sl_pct=SL_PCT,
                        max_horizon=max_horizon,
                        weekend_policy=weekend_policy
                    )

                    if trade is None:
                        continue

                    complete_row = {
                        **entry,
                        **trade,
                        "Fenetre": window_name,
                        "TPPct": tp_pct,
                        "SLPct": SL_PCT,
                        "Horizon": max_horizon,
                        "WeekendPolicy":
                            weekend_policy,
                    }

                    configuration_trades.append(
                        complete_row
                    )

                    all_trade_rows.append(
                        complete_row
                    )

                if not configuration_trades:
                    continue

                config_df = pd.DataFrame(
                    configuration_trades
                )

                completed = config_df.loc[
                    config_df["TradeStatus"]
                    == "TERMINE"
                ].copy()

                incomplete = config_df.loc[
                    config_df["TradeStatus"]
                    == "DONNEES INCOMPLETES"
                ].copy()

                if completed.empty:
                    continue

                metrics = calculate_metrics(
                    completed["ReturnPct"]
                )

                targets = int(
                    completed[
                        "TargetHit"
                    ].sum()
                )

                stops = int(
                    completed[
                        "StopHit"
                    ].sum()
                )

                weekend_exits = int(
                    completed[
                        "WeekendExit"
                    ].sum()
                )

                gap_exits = int(
                    completed[
                        "GapExit"
                    ].sum()
                )

                ambiguous_bars = int(
                    completed[
                        "AmbiguousBar"
                    ].sum()
                )

                target_rate = (
                    targets
                    / len(completed)
                    * 100
                )

                stop_rate = (
                    stops
                    / len(completed)
                    * 100
                )

                move_before_entry = (
                    completed[
                        "MoveBeforeEntryPct"
                    ].mean()
                )

                summary_rows.append(
                    {
                        "Fenetre":
                            window_name,
                        "TPPct":
                            tp_pct,
                        "SLPct":
                            SL_PCT,
                        "Horizon":
                            max_horizon,
                        "WeekendPolicy":
                            weekend_policy,
                        "Trades":
                            len(completed),
                        "Incomplete":
                            len(incomplete),
                        "Targets":
                            targets,
                        "TargetRatePct":
                            target_rate,
                        "Stops":
                            stops,
                        "StopRatePct":
                            stop_rate,
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
                            move_before_entry,
                        "WeekendExits":
                            weekend_exits,
                        "GapExits":
                            gap_exits,
                        "AmbiguousBars":
                            ambiguous_bars,
                    }
                )


# ============================================================
# SAUVEGARDE
# ============================================================

if not summary_rows:

    print(
        "Aucun résultat exploitable."
    )

    raise SystemExit(0)

summary = pd.DataFrame(
    summary_rows
)

trades = pd.DataFrame(
    all_trade_rows
)

summary.to_csv(
    OUTPUT_SUMMARY,
    index=False,
    encoding="utf-8"
)

trades.to_csv(
    OUTPUT_TRADES,
    index=False,
    encoding="utf-8"
)


# ============================================================
# CLASSEMENT DES CONFIGURATIONS
# ============================================================

summary["ProfitFactorNumeric"] = (
    pd.to_numeric(
        summary["ProfitFactor"],
        errors="coerce"
    )
)

summary["ExpectancyNumeric"] = (
    pd.to_numeric(
        summary["ExpectancyPct"],
        errors="coerce"
    )
)

# On exige au moins 10 trades complets
# pour éviter de sélectionner une configuration
# fondée seulement sur quelques observations.
eligible = summary.loc[
    summary["Trades"] >= 10
].copy()

eligible = eligible.sort_values(
    by=[
        "ExpectancyNumeric",
        "ProfitFactorNumeric",
        "WinRatePct",
    ],
    ascending=[
        False,
        False,
        False,
    ]
)

display_columns = [
    "Fenetre",
    "TPPct",
    "SLPct",
    "Horizon",
    "WeekendPolicy",
    "Trades",
    "Incomplete",
    "Targets",
    "TargetRatePct",
    "Stops",
    "StopRatePct",
    "WinRatePct",
    "PerfMeanPct",
    "PerfMedianPct",
    "AverageWinPct",
    "AverageLossPct",
    "ProfitFactor",
    "ExpectancyPct",
    "MoveBeforeEntryPct",
    "WeekendExits",
    "GapExits",
]

for column in [
    "TargetRatePct",
    "StopRatePct",
    "WinRatePct",
    "PerfMeanPct",
    "PerfMedianPct",
    "AverageWinPct",
    "AverageLossPct",
    "ProfitFactor",
    "ExpectancyPct",
    "MoveBeforeEntryPct",
]:
    if column in eligible.columns:
        eligible[column] = pd.to_numeric(
            eligible[column],
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
print("TOP 20 CONFIGURATIONS PAR ESPERANCE")
print("=" * 180)

print(
    eligible[
        display_columns
    ].head(20).to_string(
        index=False
    )
)


# ============================================================
# COMPARAISON AVEC / SANS WEEK-END
# ============================================================

print()
print("=" * 180)
print("COMPARAISON DU RISQUE WEEK-END")
print("=" * 180)

weekend_comparison = (
    summary
    .groupby(
        [
            "TPPct",
            "Horizon",
            "WeekendPolicy",
        ]
    )
    .agg(
        Configurations=(
            "Fenetre",
            "count"
        ),
        Trades=(
            "Trades",
            "sum"
        ),
        WinRatePct=(
            "WinRatePct",
            "mean"
        ),
        ExpectancyPct=(
            "ExpectancyPct",
            "mean"
        ),
        ProfitFactor=(
            "ProfitFactorNumeric",
            "mean"
        ),
        GapExits=(
            "GapExits",
            "sum"
        ),
        WeekendExits=(
            "WeekendExits",
            "sum"
        ),
    )
    .reset_index()
)

for column in [
    "WinRatePct",
    "ExpectancyPct",
    "ProfitFactor",
]:
    weekend_comparison[column] = (
        pd.to_numeric(
            weekend_comparison[column],
            errors="coerce"
        ).round(3)
    )

print(
    weekend_comparison.to_string(
        index=False
    )
)


# ============================================================
# MEILLEURE CONFIGURATION PAR FENETRE
# ============================================================

print()
print("=" * 180)
print("MEILLEURE CONFIGURATION PAR FENETRE")
print("=" * 180)

best_by_window = (
    eligible
    .sort_values(
        by=[
            "ExpectancyNumeric",
            "ProfitFactorNumeric",
        ],
        ascending=[
            False,
            False,
        ]
    )
    .groupby(
        "Fenetre",
        as_index=False
    )
    .first()
)

print(
    best_by_window[
        display_columns
    ].to_string(
        index=False
    )
)


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
print("FIN DE L'OPTIMISATION")
print("=" * 180)
