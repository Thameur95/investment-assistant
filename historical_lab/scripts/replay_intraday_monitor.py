import pandas as pd
from pathlib import Path


# ============================================================
# PARAMETRES GENERAUX
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

TP_PCT = 3.0
SL_PCT = -2.0

# 12 bougies = 60 minutes depuis l'ouverture
# 18 bougies = 90 minutes
# 24 bougies = 120 minutes
# None = toute la séance
WINDOWS = {
    "60 min": 12,
    "90 min": 18,
    "120 min": 24,
    "Toute seance": None,
}


# ============================================================
# FONCTIONS STATISTIQUES
# ============================================================

def calculate_stats(values):
    valid = pd.Series(
        values,
        dtype="float64"
    ).dropna()

    if valid.empty:
        return {
            "Cases": 0,
            "Mean": None,
            "Median": None,
            "WinRate": None,
        }

    return {
        "Cases": len(valid),
        "Mean": float(valid.mean()),
        "Median": float(valid.median()),
        "WinRate": float(
            (valid > 0).mean() * 100
        ),
    }


def safe_round(value, digits=3):
    if value is None:
        return None

    if pd.isna(value):
        return None

    return round(
        float(value),
        digits
    )


# ============================================================
# PREPARATION DES DONNEES INTRADAY
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

    missing_columns = (
        required_columns
        - set(df.columns)
    )

    if missing_columns:
        raise ValueError(
            "Colonnes manquantes : "
            + ", ".join(
                sorted(missing_columns)
            )
        )

    df = df.copy()

    df["Datetime_Paris"] = pd.to_datetime(
        df["Datetime_Paris"],
        errors="coerce"
    )

    df["Date_Paris"] = (
        df["Date_Paris"]
        .astype(str)
        .str[:10]
    )

    for column in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    \]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna(
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

    df = (
        df
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )

    return df


# ============================================================
# CALCUL DU VWAP
# ============================================================

def add_vwap(day):
    day = day.copy()

    typical_price = (
        day["High"]
        + day["Low"]
        + day["Close"]
    ) / 3

    cumulative_volume = (
        day["Volume"].cumsum()
    )

    cumulative_value = (
        typical_price
        * day["Volume"]
    ).cumsum()

    safe_volume = cumulative_volume.replace(
        0,
        float("nan")
    )

    day["VWAP"] = (
        cumulative_value
        / safe_volume
    )

    return day


# ============================================================
# DETECTION DU SIGNAL D'ENTREE
# ============================================================

def detect_entry(
    ticker,
    date,
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
        vol4 = 0.0
    else:
        vol4 = float(
            initial_volumes.mean()
        )

    day = add_vwap(day)

    decision = "EN ATTENTE"
    reason = "Prix dans Opening Range"

    decision_time = None
    decision_position = None
    decision_price = None
    decision_volume = None
    decision_vwap = None

    invalid_position = 3

    # ========================================================
    # PROTECTIONS INITIALES
    # ========================================================

    if gap_pct <= -3:

        decision = "SETUP INITIAL INVALIDE"
        reason = "Gap baissier >= 3%"

    elif close4 <= opening * 0.98:

        decision = "SETUP INITIAL INVALIDE"

        reason = (
            "Baisse >= 2% apres 20 minutes"
        )

    elif (
        red4 >= 3
        and close4 < opening
    ):

        decision = "SETUP INITIAL INVALIDE"

        reason = (
            "Au moins 3 bougies rouges sur 4"
        )

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

            # ================================================
            # INVALIDATION DURANT LA FENETRE
            # ================================================

            if (
                current_low < or_low
                or current_close
                <= opening * 0.98
            ):

                decision = (
                    "SETUP INITIAL INVALIDE"
                )

                reason = (
                    "Cassure baissiere "
                    "Opening Range"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

                invalid_position = int(
                    position
                )

                break

            # ================================================
            # ACHAT CONFIRME
            # ================================================

            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume >= vol4
            ):

                decision = "ACHAT CONFIRME"

                reason = (
                    "Cassure haussiere "
                    "+ VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

                decision_position = int(
                    position
                )

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

        # ====================================================
        # FIN DE LA FENETRE SANS CONFIRMATION
        # ====================================================

        if decision == "EN ATTENTE":

            window_finished = (
                max_bars is None
                or len(day) >= max_bars
            )

            if window_finished:

                decision = (
                    "SETUP INITIAL INVALIDE"
                )

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
                        f"pendant {max_bars * 5} "
                        "minutes"
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

        for position, row in (
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
                >= vol4 * 1.5
            ):

                decision = "ACHAT REACTIVE"

                reason = (
                    "Retournement confirme : "
                    "OR High + VWAP + volume"
                )

                decision_time = row[
                    "Datetime_Paris"
                ]

                decision_position = int(
                    position
                )

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
        "Date": date,
        "SignalPrice": signal_price,
        "OpeningPrice": opening,
        "GapPct": gap_pct,
        "OR_High": or_high,
        "OR_Low": or_low,
        "Close20Min": close4,
        "RedCandles4": red4,
        "InitialAvgVolume": vol4,
        "Decision": decision,
        "Reason": reason,
        "DecisionTime": decision_time,
        "DecisionPosition": decision_position,
        "DecisionPrice": decision_price,
        "DecisionVolume": decision_volume,
        "DecisionVWAP": decision_vwap,
        "MoveBeforeEntryPct":
            move_before_entry_pct,
        "Bars": len(day),
    }


# ============================================================
# SIMULATION TP / SL / SORTIE J+2
# ============================================================

def simulate_trade(
    full_intraday,
    entry_date,
    entry_time,
    entry_price
):
    """
    Simule le trade depuis la bougie suivant la confirmation.

    TP : +3 %
    SL : -2 %
    Sortie forcée : clôture de J+2.

    Si TP et SL sont touchés dans la même bougie,
    le scénario prudent considère le SL touché en premier.
    """

    if entry_price is None:
        return None

    if entry_time is None:
        return None

    entry_price = float(
        entry_price
    )

    entry_time = pd.Timestamp(
        entry_time
    )

    if entry_price <= 0:
        return None

    target_price = (
        entry_price
        * (1 + TP_PCT / 100)
    )

    stop_price = (
        entry_price
        * (1 + SL_PCT / 100)
    )

    available_dates = sorted(
        full_intraday[
            "Date_Paris"
        ].unique()
    )

    future_dates = [
        session_date
        for session_date in available_dates
        if session_date >= entry_date
    ]

    if entry_date not in future_dates:
        return None

    entry_date_position = (
        future_dates.index(
            entry_date
        )
    )

    required_exit_position = (
        entry_date_position + 2
    )

    if (
        required_exit_position
        >= len(future_dates)
    ):
        return {
            "TradeStatus": "DONNEES INCOMPLETES",
            "ExitReason": "J+2 indisponible",
            "EntryPrice": entry_price,
            "TargetPrice": target_price,
            "StopPrice": stop_price,
            "ExitTime": None,
            "ExitPrice": None,
            "RealizedReturnPct": None,
            "HoldingSession": None,
            "AmbiguousBar": False,
            "TargetHit": False,
            "StopHit": False,
        }

    exit_date_j2 = future_dates[
        required_exit_position
    ]

    trade_data = full_intraday.loc[
        (
            full_intraday["Datetime_Paris"]
            > entry_time
        )
        &
        (
            full_intraday["Date_Paris"]
            <= exit_date_j2
        )
    ].copy()

    if trade_data.empty:
        return {
            "TradeStatus": "DONNEES INCOMPLETES",
            "ExitReason": "Aucune bougie apres entree",
            "EntryPrice": entry_price,
            "TargetPrice": target_price,
            "StopPrice": stop_price,
            "ExitTime": None,
            "ExitPrice": None,
            "RealizedReturnPct": None,
            "HoldingSession": None,
            "AmbiguousBar": False,
            "TargetHit": False,
            "StopHit": False,
        }

    exit_reason = None
    exit_time = None
    exit_price = None
    holding_session = None

    target_hit = False
    stop_hit = False
    ambiguous_bar = False

    for _, row in trade_data.iterrows():

        current_high = float(
            row["High"]
        )

        current_low = float(
            row["Low"]
        )

        current_date = str(
            row["Date_Paris"]
        )[:10]

        hit_target = (
            current_high >= target_price
        )

        hit_stop = (
            current_low <= stop_price
        )

        if hit_target and hit_stop:

            # Hypothèse prudente :
            # stop touché avant target.
            exit_reason = (
                "SL ET TP MEME BOUGIE "
                "- SL PRIORITAIRE"
            )

            exit_time = row[
                "Datetime_Paris"
            ]

            exit_price = stop_price
            stop_hit = True
            ambiguous_bar = True

        elif hit_stop:

            exit_reason = "STOP LOSS -2%"

            exit_time = row[
                "Datetime_Paris"
            ]

            exit_price = stop_price
            stop_hit = True

        elif hit_target:

            exit_reason = "TAKE PROFIT +3%"

            exit_time = row[
                "Datetime_Paris"
            ]

            exit_price = target_price
            target_hit = True

        if exit_reason is not None:

            if current_date == entry_date:
                holding_session = "J"

            else:
                session_number = (
                    future_dates.index(
                        current_date
                    )
                    - entry_date_position
                )

                holding_session = (
                    f"J+{session_number}"
                )

            break

    # ========================================================
    # SORTIE FORCEE A LA CLOTURE DE J+2
    # ========================================================

    if exit_reason is None:

        j2_data = trade_data.loc[
            trade_data["Date_Paris"]
            == exit_date_j2
        ]

        if j2_data.empty:

            return {
                "TradeStatus": "DONNEES INCOMPLETES",
                "ExitReason": "Cloture J+2 indisponible",
                "EntryPrice": entry_price,
                "TargetPrice": target_price,
                "StopPrice": stop_price,
                "ExitTime": None,
                "ExitPrice": None,
                "RealizedReturnPct": None,
                "HoldingSession": None,
                "AmbiguousBar": False,
                "TargetHit": False,
                "StopHit": False,
            }

        last_bar = j2_data.iloc[-1]

        exit_reason = "SORTIE TEMPORELLE J+2"

        exit_time = last_bar[
            "Datetime_Paris"
        ]

        exit_price = float(
            last_bar["Close"]
        )

        holding_session = "J+2"

    realized_return_pct = (
        (
            exit_price
            / entry_price
        )
        - 1
    ) * 100

    return {
        "TradeStatus": "TERMINE",
        "ExitReason": exit_reason,
        "EntryPrice": entry_price,
        "TargetPrice": target_price,
        "StopPrice": stop_price,
        "ExitTime": exit_time,
        "ExitPrice": exit_price,
        "RealizedReturnPct":
            realized_return_pct,
        "HoldingSession":
            holding_session,
        "AmbiguousBar":
            ambiguous_bar,
        "TargetHit":
            target_hit,
        "StopHit":
            stop_hit,
    }


# ============================================================
# CHARGEMENT DES DONNEES DAILY
# ============================================================

print("=" * 90)
print("HISTORICAL LAB")
print("TP +3% / SL -2% / SORTIE MAXIMUM J+2")
print("=" * 90)

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


# ============================================================
# CHARGEMENT DES FICHIERS INTRADAY
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

        ticker_intraday = pd.read_csv(
            intraday_file
        )

        ticker_intraday = (
            prepare_intraday_data(
                ticker_intraday
            )
        )

        intraday_by_ticker[
            ticker
        ] = ticker_intraday

    except Exception as error:

        print(
            ticker,
            "| erreur chargement :",
            str(error)
        )


# ============================================================
# TEST DES FENETRES
# ============================================================

results_by_window = {}

for window_name, max_bars in WINDOWS.items():

    print()
    print("=" * 90)
    print(
        f"TEST FENETRE : {window_name}"
    )
    print("=" * 90)

    window_results = []

    for ticker, ticker_intraday in (
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
                    "Close",
                ]
            )
            .sort_values("Date")
            .reset_index(drop=True)
        )

        if daily.empty:
            continue

        # ====================================================
        # SIGNAL DE J-1 VERS SESSION J
        # ====================================================

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

        ticker_results = []

        for date, day in (
            ticker_intraday.groupby(
                "Date_Paris"
            )
        ):

            date = str(date)[:10]

            if date not in candidate_sessions:
                continue

            signal_price = (
                signal_price_by_session
                .get(date)
            )

            if signal_price is None:
                continue

            entry_result = detect_entry(
                ticker=ticker,
                date=date,
                day=day,
                signal_price=signal_price,
                max_bars=max_bars
            )

            if entry_result is None:
                continue

            is_buy = (
                entry_result["Decision"]
                in [
                    "ACHAT CONFIRME",
                    "ACHAT REACTIVE",
                ]
            )

            trade_result = None

            if is_buy:

                trade_result = simulate_trade(
                    full_intraday=ticker_intraday,
                    entry_date=date,
                    entry_time=entry_result[
                        "DecisionTime"
                    ],
                    entry_price=entry_result[
                        "DecisionPrice"
                    ]
                )

            complete_result = dict(
                entry_result
            )

            if trade_result is None:

                complete_result.update(
                    {
                        "TradeStatus": "PAS DE TRADE",
                        "ExitReason": None,
                        "TargetPrice": None,
                        "StopPrice": None,
                       "ExitTime": None,
                        "ExitPrice": None,
                        "RealizedReturnPct": None,
                        "HoldingSession": None,
                        "AmbiguousBar": False,
                        "TargetHit": False,
                        "StopHit": False,
                    }
                )

            else:

                complete_result.update(
                    trade_result
                )

            ticker_results.append(
                complete_result
            )

        if ticker_results:

            ticker_df = pd.DataFrame(
                ticker_results
            )

            window_results.append(
                ticker_df
            )

            completed_trades = ticker_df.loc[
                ticker_df["TradeStatus"]
                == "TERMINE"
            ]

            targets = int(
                completed_trades[
                    "TargetHit"
                ].sum()
            )

            stops = int(
                completed_trades[
                    "StopHit"
                ].sum()
            )

            print(
                f"{ticker:8s} | "
                f"seances {len(ticker_df):3d} | "
                f"trades {len(completed_trades):3d} | "
                f"TP {targets:3d} | "
                f"SL {stops:3d}"
            )

    if window_results:

        results_by_window[
            window_name
        ] = pd.concat(
            window_results,
            ignore_index=True
        )


# ============================================================
# RAPPORT DETAILLE
# ============================================================

summary_rows = []

for window_name, result in (
    results_by_window.items()
):

    print()
    print("=" * 90)
    print(
        f"RESULTATS : {window_name}"
    )
    print("=" * 90)

    buy_mask = result[
        "Decision"
    ].isin(
        [
            "ACHAT CONFIRME",
            "ACHAT REACTIVE",
        ]
    )

    triggered = result.loc[
        buy_mask
    ].copy()

    completed = triggered.loc[
        triggered["TradeStatus"]
        == "TERMINE"
    ].copy()

    incomplete = triggered.loc[
        triggered["TradeStatus"]
        == "DONNEES INCOMPLETES"
    ].copy()

    rejects = result.loc[
        ~buy_mask
    ].copy()

    targets = completed.loc[
        completed["TargetHit"]
    ].copy()

    stops = completed.loc[
        completed["StopHit"]
    ].copy()

    time_exits = completed.loc[
        completed["ExitReason"]
        == "SORTIE TEMPORELLE J+2"
    ].copy()

    ambiguous = completed.loc[
        completed["AmbiguousBar"]
    ].copy()

    print(
        "Séances analysées :",
        len(result)
    )

    print(
        "Signaux d'achat :",
        len(triggered)
    )

    print(
        "Trades complets J à J+2 :",
        len(completed)
    )

    print(
        "Trades avec données incomplètes :",
        len(incomplete)
    )

    print(
        "Setups rejetés :",
        len(rejects)
    )

    if len(result) > 0:

        trigger_rate = (
            len(triggered)
            / len(result)
            * 100
        )

    else:

        trigger_rate = 0.0

    print(
        "Taux de déclenchement :",
        round(
            trigger_rate,
            1
        ),
        "%"
    )

    if completed.empty:

        print(
            "Aucun trade complet "
            "pour cette fenêtre."
        )

        continue

    target_rate = (
        len(targets)
        / len(completed)
        * 100
    )

    stop_rate = (
        len(stops)
        / len(completed)
        * 100
    )

    time_exit_rate = (
        len(time_exits)
        / len(completed)
        * 100
    )

    realized_stats = calculate_stats(
        completed[
            "RealizedReturnPct"
        ]
    )

    move_stats = calculate_stats(
        completed[
            "MoveBeforeEntryPct"
        ]
    )

    gross_profit = completed.loc[
        completed["RealizedReturnPct"] > 0,
        "RealizedReturnPct"
    ].sum()

    gross_loss = abs(
        completed.loc[
            completed[
                "RealizedReturnPct"
            ] < 0,
            "RealizedReturnPct"
        ].sum()
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

    average_win = completed.loc[
        completed["RealizedReturnPct"] > 0,
        "RealizedReturnPct"
    ].mean()

    average_loss = completed.loc[
        completed["RealizedReturnPct"] < 0,
        "RealizedReturnPct"
    ].mean()

    expectancy = (
        completed[
            "RealizedReturnPct"
        ].mean()
    )

    print()
    print(
        "Target +3% atteint :",
        len(targets),
        f"({target_rate:.1f} %)"
    )

    print(
        "Stop -2% atteint :",
        len(stops),
        f"({stop_rate:.1f} %)"
    )

    print(
        "Sortie temporelle J+2 :",
        len(time_exits),
        f"({time_exit_rate:.1f} %)"
    )

    print(
        "Bougies ambiguës TP + SL :",
        len(ambiguous)
    )

    print()
    print(
        "Performance réalisée moyenne :",
        round(
            realized_stats["Mean"],
            3
        ),
        "%"
    )

    print(
        "Performance réalisée médiane :",
        round(
            realized_stats["Median"],
            3
        ),
        "%"
    )

    print(
        "Taux de trades positifs :",
        round(
            realized_stats["WinRate"],
            1
        ),
        "%"
    )

    print(
        "Gain moyen des gagnants :",
        (
            round(
                float(average_win),
                3
            )
            if pd.notna(average_win)
            else "N/A"
        ),
        "%"
    )

    print(
        "Perte moyenne des perdants :",
        (
            round(
                float(average_loss),
                3
            )
            if pd.notna(average_loss)
            else "N/A"
        ),
        "%"
    )

    print(
        "Profit Factor :",
        (
            round(
                float(profit_factor),
                3
            )
            if (
                profit_factor is not None
                and profit_factor
                != float("inf")
            )
            else profit_factor
        )
    )

    print(
        "Esperance par trade :",
        round(
            float(expectancy),
            3
        ),
        "%"
    )

    print()
    print(
        "Mouvement moyen deja consomme "
        "avant entree :",
        round(
            move_stats["Mean"],
            3
        ),
        "%"
    )

    print(
        "Mouvement median deja consomme "
        "avant entree :",
        round(
            move_stats["Median"],
            3
        ),
        "%"
    )

    print()
    print("Sorties par horizon :")

    print(
        completed[
            "HoldingSession"
        ].value_counts(
            dropna=False
        )
    )

    print()
    print(
        "Sorties par type :"
    )

    print(
        completed[
            "ExitReason"
        ].value_counts(
            dropna=False
        )
    )

    summary_rows.append(
        {
            "Fenetre": window_name,
            "Seances": len(result),
            "SignauxAchat": len(triggered),
            "TradesComplets": len(completed),
            "DonneesIncompletes":
                len(incomplete),
            "TauxDeclenchementPct":
                trigger_rate,
            "Target3Pct": len(targets),
            "TargetRatePct":
                target_rate,
            "StopMoins2Pct": len(stops),
            "StopRatePct":
                stop_rate,
            "SortiesJ2": len(time_exits),
            "SortieJ2RatePct":
                time_exit_rate,
            "WinRatePct":
                realized_stats["WinRate"],
            "PerfMoyennePct":
                realized_stats["Mean"],
            "PerfMedianePct":
                realized_stats["Median"],
            "AverageWinPct":
                average_win,
            "AverageLossPct":
                average_loss,
            "ProfitFactor":
                profit_factor,
            "ExpectancyPct":
                expectancy,
            "MoveBeforeEntryPct":
                move_stats["Mean"],
            "AmbiguousBars":
                len(ambiguous),
        }
    )


# ============================================================
# TABLEAU COMPARATIF FINAL
# ============================================================

print()
print("=" * 130)
print("COMPARAISON TP +3% / SL -2% / SORTIE J+2")
print("=" * 130)

if not summary_rows:

    print(
        "Aucun trade complet disponible."
    )

    raise SystemExit(0)

summary = pd.DataFrame(
    summary_rows
)

columns_to_round = [
    "TauxDeclenchementPct",
    "TargetRatePct",
    "StopRatePct",
    "SortieJ2RatePct",
    "WinRatePct",
    "PerfMoyennePct",
    "PerfMedianePct",
    "AverageWinPct",
    "AverageLossPct",
    "ProfitFactor",
    "ExpectancyPct",
    "MoveBeforeEntryPct",
]

for column in columns_to_round:

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
    250
)

print()

print(
    summary.to_string(
        index=False
    )
)


# ============================================================
# DETAIL PAR TYPE D'ACHAT
# ============================================================

print()
print("=" * 130)
print("DETAIL ACHAT CONFIRME VS ACHAT REACTIVE")
print("=" * 130)

for window_name, result in (
    results_by_window.items()
):

    print()
    print(
        f"--- {window_name} ---"
    )

    for decision_type in [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE",
    ]:

        subset = result.loc[
            (
                result["Decision"]
                == decision_type
            )
            &
            (
                result["TradeStatus"]
                == "TERMINE"
            )
        ].copy()

        if subset.empty:
            continue

        targets = int(
            subset["TargetHit"].sum()
        )

        stops = int(
            subset["StopHit"].sum()
        )

        target_rate = (
            targets
            / len(subset)
            * 100
        )

        stop_rate = (
            stops
            / len(subset)
            * 100
        )

        stats = calculate_stats(
            subset[
                "RealizedReturnPct"
            ]
        )

        print()
        print(
            decision_type,
            "| Trades :",
            len(subset)
        )

        print(
            f"TP +3% : {targets} "
            f"({target_rate:.1f} %)"
        )

        print(
            f"SL -2% : {stops} "
            f"({stop_rate:.1f} %)"
        )

        print(
            "Performance moyenne :",
            round(
                stats["Mean"],
                3
            ),
            "%"
        )

        print(
            "Performance médiane :",
            round(
                stats["Median"],
                3
            ),
            "%"
        )

        print(
            "Win rate réalisé :",
            round(
                stats["WinRate"],
                1
            ),
            "%"
        )


# ============================================================
# FIN
# ============================================================

print()
print("=" * 130)
print("FIN DU BACKTEST TP / SL")
print("=" * 130)
