from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
INTRADAY_DIR = BASE_DIR / "data" / "intraday"
DAILY_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_DIR = BASE_DIR / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ATR_PERIOD = 14
TP_MIN_PCT = 1.0
TP_MAX_PCT = 4.0
SL_MIN_PCT = 0.75
SL_MAX_PCT = 3.0

# Hypothèses d'exécution, par côté.
FEE_PCT_PER_SIDE = 0.05
SLIPPAGE_PCT_PER_SIDE = 0.05

# Minimum requis pour afficher une conclusion sur une période.
MIN_TRADES_PER_FOLD = 3

# Trois finalistes seulement, afin d'éviter une nouvelle optimisation massive.
# Le benchmark reste volontairement simple.
FINALISTS = [
    {
        "Name": "BENCHMARK_FIXED_60",
        "EntryWindow": "60 min",
        "EntryBars": 12,
        "EntryType": "COMBINE",
        "TPMode": "FIXED",
        "TPValue": 2.0,
        "SLMode": "FIXED",
        "SLValue": 2.0,
        "Horizon": 3,
        "WeekendPolicy": "HOLD_WEEKEND",
    },
    {
        "Name": "ATR_075_100_60",
        "EntryWindow": "60 min",
        "EntryBars": 12,
        "EntryType": "COMBINE",
        "TPMode": "ATR",
        "TPValue": 0.75,
        "SLMode": "ATR",
        "SLValue": 1.00,
        "Horizon": 4,
        "WeekendPolicy": "HOLD_WEEKEND",
    },
    {
        "Name": "HYBRID_ATRSL_60",
        "EntryWindow": "60 min",
        "EntryBars": 12,
        "EntryType": "COMBINE",
        "TPMode": "HYBRID",
        "TPValue": 0.0,
        "SLMode": "ATR",
        "SLValue": 1.00,
        "Horizon": 4,
        "WeekendPolicy": "HOLD_WEEKEND",
    },
]

# Walk-forward chronologique à fenêtres croissantes.
# Chaque fold teste uniquement des dates postérieures à la période d'entraînement.
WALK_FORWARD_FOLDS = [
    {"Name": "WF1", "TrainEnd": 0.50, "TestEnd": 0.67},
    {"Name": "WF2", "TrainEnd": 0.67, "TestEnd": 0.84},
    {"Name": "WF3", "TrainEnd": 0.84, "TestEnd": 1.00},
]


# ============================================================
# PREPARATION DES DONNEES
# ============================================================

def prepare_intraday_data(df):
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

    for column in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]:
        result[column] = pd.to_numeric(
            result[column],
            errors="coerce",
        )

    return (
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


def calculate_atr(daily, period=ATR_PERIOD):
    result = daily.copy()

    previous_close = result["Close"].shift(1)

    true_range = pd.concat(
        [
            result["High"] - result["Low"],
            (result["High"] - previous_close).abs(),
            (result["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    result["ATR14"] = (
        true_range
        .rolling(
            window=period,
            min_periods=period,
        )
        .mean()
    )

    result["ATR14Pct"] = (
        result["ATR14"]
        / result["Close"]
        * 100.0
    )

    return result


def add_vwap(day):
    result = day.copy()

    typical_price = (
        result["High"]
        + result["Low"]
        + result["Close"]
    ) / 3.0

    cumulative_volume = result["Volume"].cumsum()

    result["VWAP"] = (
        (typical_price * result["Volume"])
        .cumsum()
        / cumulative_volume.replace(0, np.nan)
    )

    return result


# ============================================================
# DETECTION DES ENTREES
# ============================================================

def detect_entry(
    ticker,
    session_date,
    day,
    signal_price,
    atr_pct,
    max_bars,
):
    day = (
        day
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
        .copy()
    )

    if len(day) < 4:
        return None

    if signal_price is None or pd.isna(signal_price):
        return None

    signal_price = float(signal_price)

    if signal_price <= 0:
        return None

    opening = float(day["Open"].iloc[0])

    if opening <= 0:
        return None

    gap_pct = (
        opening / signal_price - 1.0
    ) * 100.0

    first4 = day.iloc[:4].copy()

    or_high = float(first4["High"].max())
    or_low = float(first4["Low"].min())
    close4 = float(first4["Close"].iloc[-1])

    red4 = int(
        (
            first4["Close"]
            < first4["Open"]
        ).sum()
    )

    initial_volumes = first4.loc[
        first4["Volume"] > 0,
        "Volume",
    ]

    initial_average_volume = (
        float(initial_volumes.mean())
        if not initial_volumes.empty
        else 0.0
    )

    day = add_vwap(day)

    decision = "EN ATTENTE"
    reason = "Prix dans Opening Range"

    decision_time = None
    decision_price = None
    decision_volume = None
    decision_vwap = None

    invalid_position = 3

    if gap_pct <= -3:
        decision = "SETUP INITIAL INVALIDE"
        reason = "Gap baissier >= 3%"

    elif close4 <= opening * 0.98:
        decision = "SETUP INITIAL INVALIDE"
        reason = "Baisse >= 2% apres 20 minutes"

    elif red4 >= 3 and close4 < opening:
        decision = "SETUP INITIAL INVALIDE"
        reason = "Au moins 3 bougies rouges sur 4"

    else:
        confirmation_window = (
            day.copy()
            if max_bars is None
            else day.iloc[:max_bars].copy()
        )

        for position, row in (
            confirmation_window
            .iloc[4:]
            .iterrows()
        ):
            current_low = float(row["Low"])
            current_close = float(row["Close"])
            current_volume = float(row["Volume"])
            current_vwap = row["VWAP"]

            if pd.isna(current_vwap):
                continue

            current_vwap = float(current_vwap)

            if (
                current_low < or_low
                or current_close <= opening * 0.98
            ):
                decision = "SETUP INITIAL INVALIDE"
                reason = "Cassure baissiere Opening Range"
                invalid_position = int(position)
                break

            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume >= initial_average_volume
            ):
                decision = "ACHAT CONFIRME"
                reason = "Cassure haussiere + VWAP + volume"
                decision_time = row["Datetime_Paris"]
                decision_price = current_close
                decision_volume = current_volume
                decision_vwap = current_vwap
                break

        if decision == "EN ATTENTE":
            window_finished = (
                max_bars is None
                or len(day) >= max_bars
            )

            if window_finished:
                decision = "SETUP INITIAL INVALIDE"

                if max_bars is None:
                    reason = "Aucune confirmation pendant la seance"
                    invalid_position = len(day) - 1
                else:
                    reason = (
                        "Aucune confirmation pendant "
                        f"{max_bars * 5} minutes"
                    )
                    invalid_position = max_bars - 1

    if decision == "SETUP INITIAL INVALIDE":
        for _, row in (
            day
            .iloc[invalid_position + 1:]
            .iterrows()
        ):
            current_close = float(row["Close"])
            current_volume = float(row["Volume"])
            current_vwap = row["VWAP"]

            if pd.isna(current_vwap):
                continue

            current_vwap = float(current_vwap)

            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume >= initial_average_volume * 1.5
            ):
                decision = "ACHAT REACTIVE"
                reason = (
                    "Retournement confirme : "
                    "OR High + VWAP + volume"
                )
                decision_time = row["Datetime_Paris"]
                decision_price = current_close
                decision_volume = current_volume
                decision_vwap = current_vwap
                break

    if decision not in [
        "ACHAT CONFIRME",
        "ACHAT REACTIVE",
    ]:
        return None

    move_before_entry_pct = (
        decision_price / opening - 1.0
    ) * 100.0

    return {
        "Ticker": ticker,
        "SessionDate": session_date,
        "ATR14Pct": atr_pct,
        "Decision": decision,
        "Reason": reason,
        "DecisionTime": decision_time,
        "DecisionPrice": decision_price,
        "DecisionVolume": decision_volume,
        "DecisionVWAP": decision_vwap,
        "OpeningPrice": opening,
        "GapPct": gap_pct,
        "ORHigh": or_high,
        "ORLow": or_low,
        "MoveBeforeEntryPct": move_before_entry_pct,
    }


# ============================================================
# TP ET SL
# ============================================================

def resolve_tp(entry, mode, value):
    atr_pct = entry.get("ATR14Pct")

    if mode == "FIXED":
        return float(value)

    if atr_pct is None or pd.isna(atr_pct):
        return None

    atr_pct = float(atr_pct)

    if mode == "ATR":
        return min(
            TP_MAX_PCT,
            max(
                TP_MIN_PCT,
                atr_pct * float(value),
            ),
        )

    if mode == "HYBRID":
        if atr_pct < 1.5:
            return 1.5

        if atr_pct < 2.5:
            return 2.0

        return min(
            3.0,
            max(
                2.0,
                atr_pct * 0.75,
            ),
        )

    raise ValueError(
        f"Mode TP inconnu : {mode}"
    )


def resolve_sl(entry, mode, value):
    atr_pct = entry.get("ATR14Pct")

    if mode == "FIXED":
        return float(value)

    if atr_pct is None or pd.isna(atr_pct):
        return None

    return min(
        SL_MAX_PCT,
        max(
            SL_MIN_PCT,
            float(atr_pct) * float(value),
        ),
    )


# ============================================================
# SIMULATION DU TRADE
# ============================================================

def simulate_trade(
    full_intraday,
    entry,
    tp_pct,
    sl_pct,
    horizon,
    weekend_policy,
):
    entry_time = entry["DecisionTime"]
    entry_date = entry["SessionDate"]
    raw_entry_price = float(entry["DecisionPrice"])

    entry_time = pd.Timestamp(entry_time)

    effective_entry_price = (
        raw_entry_price
        * (
            1.0
            + SLIPPAGE_PCT_PER_SIDE / 100.0
        )
    )

    target_price = (
        raw_entry_price
        * (1.0 + tp_pct / 100.0)
    )

    stop_price = (
        raw_entry_price
        * (1.0 - sl_pct / 100.0)
    )

    session_dates = sorted(
        full_intraday["Date_Paris"].unique()
    )

    if entry_date not in session_dates:
        return None

    entry_position = session_dates.index(
        entry_date
    )

    forced_exit_position = (
        entry_position + horizon
    )

    if forced_exit_position >= len(session_dates):
        return {
            "TradeStatus": "DONNEES INCOMPLETES"
        }

    forced_exit_date = session_dates[
        forced_exit_position
    ]

    trade_data = full_intraday.loc[
        (
            full_intraday["Datetime_Paris"]
            > entry_time
        )
        & (
            full_intraday["Date_Paris"]
            <= forced_exit_date
        )
    ].copy()

    if trade_data.empty:
        return {
            "TradeStatus": "DONNEES INCOMPLETES"
        }

    exit_reason = None
    exit_time = None
    raw_exit_price = None
    holding_session = None

    target_hit = False
    stop_hit = False
    gap_exit = False
    ambiguous_bar = False

    for session_date, session in (
        trade_data.groupby(
            "Date_Paris",
            sort=True,
        )
    ):
        session = (
            session
            .sort_values("Datetime_Paris")
            .reset_index(drop=True)
        )

        if session.empty:
            continue

        offset = (
            session_dates.index(session_date)
            - entry_position
        )

        session_label = (
            "J"
            if offset == 0
            else f"J+{offset}"
        )

        first_bar = session.iloc[0]
        session_open = float(first_bar["Open"])

        if session_open <= stop_price:
            exit_reason = "GAP SOUS STOP"
            exit_time = first_bar["Datetime_Paris"]
            raw_exit_price = session_open
            holding_session = session_label
            stop_hit = True
            gap_exit = True
            break

        if session_open >= target_price:
            exit_reason = "GAP AU-DESSUS TARGET"
            exit_time = first_bar["Datetime_Paris"]
            raw_exit_price = session_open
            holding_session = session_label
            target_hit = True
            gap_exit = True
            break

        for _, row in session.iterrows():
            hit_target = (
                float(row["High"])
                >= target_price
            )

            hit_stop = (
                float(row["Low"])
                <= stop_price
            )

            if hit_target and hit_stop:
                exit_reason = (
                    "TP ET SL MEME BOUGIE "
                    "- SL PRIORITAIRE"
                )
                raw_exit_price = stop_price
                stop_hit = True
                ambiguous_bar = True

            elif hit_stop:
                exit_reason = "STOP"
                raw_exit_price = stop_price
                stop_hit = True

            elif hit_target:
                exit_reason = "TARGET"
                raw_exit_price = target_price
                target_hit = True

            if exit_reason is not None:
                exit_time = row["Datetime_Paris"]
                holding_session = session_label
                break

        if exit_reason is not None:
            break

        if (
            weekend_policy == "FRIDAY_FLAT"
            and pd.Timestamp(session_date).weekday() == 4
        ):
            last_bar = session.iloc[-1]
            exit_reason = "SORTIE VENDREDI"
            exit_time = last_bar["Datetime_Paris"]
            raw_exit_price = float(last_bar["Close"])
            holding_session = session_label
            break

    if exit_reason is None:
        forced_data = trade_data.loc[
            trade_data["Date_Paris"]
            == forced_exit_date
        ]

        if forced_data.empty:
            return {
                "TradeStatus": "DONNEES INCOMPLETES"
            }

        last_bar = forced_data.iloc[-1]

        exit_reason = (
            f"SORTIE TEMPORELLE J+{horizon}"
        )

        exit_time = last_bar["Datetime_Paris"]
        raw_exit_price = float(last_bar["Close"])
        holding_session = f"J+{horizon}"

    effective_exit_price = (
        raw_exit_price
        * (
            1.0
            - SLIPPAGE_PCT_PER_SIDE / 100.0
        )
    )

    gross_return_pct = (
        raw_exit_price / raw_entry_price - 1.0
    ) * 100.0

    net_return_pct = (
        effective_exit_price
        / effective_entry_price
        - 1.0
    ) * 100.0

    net_return_pct -= (
        2.0 * FEE_PCT_PER_SIDE
    )

    return {
        "TradeStatus": "TERMINE",
        "ExitReason": exit_reason,
        "ExitTime": exit_time,
        "RawEntryPrice": raw_entry_price,
        "RawExitPrice": raw_exit_price,
        "GrossReturnPct": gross_return_pct,
        "NetReturnPct": net_return_pct,
        "TargetHit": target_hit,
        "StopHit": stop_hit,
        "GapExit": gap_exit,
        "AmbiguousBar": ambiguous_bar,
        "HoldingSession": holding_session,
    }


# ============================================================
# STATISTIQUES
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
            "AverageWinPct": np.nan,
            "AverageLossPct": np.nan,
            "ProfitFactor": np.nan,
            "MaxDrawdownPct": np.nan,
            "WorstTradePct": np.nan,
        }

    winners = series[series > 0]
    losers = series[series < 0]

    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))

    if gross_loss > 0:
        profit_factor = (
            gross_profit / gross_loss
        )
    elif gross_profit > 0:
        profit_factor = np.inf
    else:
        profit_factor = np.nan

    equity_curve = (
        1.0 + series / 100.0
    ).cumprod()

    drawdown = (
        equity_curve
        / equity_curve.cummax()
        - 1.0
    )

    return {
        "Trades": len(series),
        "WinRatePct": (
            (series > 0).mean() * 100.0
        ),
        "ExpectancyPct": float(series.mean()),
        "MedianPct": float(series.median()),
        "AverageWinPct": (
            float(winners.mean())
            if not winners.empty
            else np.nan
        ),
        "AverageLossPct": (
            float(losers.mean())
            if not losers.empty
            else np.nan
        ),
        "ProfitFactor": profit_factor,
        "MaxDrawdownPct": (
            float(drawdown.min()) * 100.0
        ),
        "WorstTradePct": float(series.min()),
    }


# ============================================================
# WALK-FORWARD
# ============================================================

def build_walk_forward_periods(all_dates):
    dates = sorted(all_dates)
    total_dates = len(dates)
    periods = []

    if total_dates < 6:
        return periods

    for fold in WALK_FORWARD_FOLDS:
        train_end_index = int(
            total_dates * fold["TrainEnd"]
        )

        test_end_index = int(
            total_dates * fold["TestEnd"]
        )

        train_end_index = max(
            1,
            min(
                train_end_index,
                total_dates - 1,
            ),
        )

        test_end_index = max(
            train_end_index + 1,
            min(
                test_end_index,
                total_dates,
            ),
        )

        train_dates = dates[:train_end_index]

        test_dates = dates[
            train_end_index:test_end_index
        ]

        if not test_dates:
            continue

        periods.append(
            {
                "Fold": fold["Name"],
                "TrainStart": train_dates[0],
                "TrainEnd": train_dates[-1],
                "TestStart": test_dates[0],
                "TestEnd": test_dates[-1],
                "TrainDates": set(train_dates),
                "TestDates": set(test_dates),
            }
        )

    return periods


# ============================================================
# CHARGEMENT
# ============================================================

print("=" * 120)
print("HISTORICAL LAB - FINALISTES WALK-FORWARD")
print("3 STRATEGIES SEULEMENT, FRAIS ET SLIPPAGE INCLUS")
print("=" * 120)

if not DAILY_FILE.exists():
    raise FileNotFoundError(
        f"Fichier introuvable : {DAILY_FILE}"
    )

daily_all = pd.read_csv(DAILY_FILE)

required_daily_columns = {
    "Date",
    "Ticker",
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
    raise ValueError(
        "Colonnes daily manquantes : "
        + ", ".join(
            sorted(missing_daily_columns)
        )
    )

daily_all["Date"] = pd.to_datetime(
    daily_all["Date"],
    errors="coerce",
)

for column in [
    "High",
    "Low",
    "Close",
]:
    daily_all[column] = pd.to_numeric(
        daily_all[column],
        errors="coerce",
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
        intraday_by_ticker[ticker] = (
            prepare_intraday_data(
                pd.read_csv(intraday_file)
            )
        )
    except Exception as error:
        print(
            ticker,
            "| erreur intraday :",
            str(error),
        )

if not intraday_by_ticker:
    raise SystemExit(
        "Aucun fichier intraday exploitable."
    )


# ============================================================
# DETECTION DES ENTREES UNE SEULE FOIS
# ============================================================

entries_by_window = {}

unique_windows = {}

for strategy in FINALISTS:
    unique_windows[
        strategy["EntryWindow"]
    ] = strategy["EntryBars"]

for window_name, max_bars in (
    unique_windows.items()
):
    entries = []

    for ticker, intraday in (
        intraday_by_ticker.items()
    ):
        daily = daily_all.loc[
            daily_all["Ticker"] == ticker
        ].copy()

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
                keep="last",
            )
            .reset_index(drop=True)
        )

        if daily.empty:
            continue

        daily = calculate_atr(daily)

        daily["SessionDate"] = (
            daily["Date"]
            .shift(-1)
            .dt.strftime("%Y-%m-%d")
        )

        signals = daily.loc[
            daily["Signal"] == "ACHAT"
        ].copy()

        price_by_session = (
            signals
            .dropna(
                subset=[
                    "SessionDate",
                    "Close",
                ]
            )
            .drop_duplicates(
                subset="SessionDate",
                keep="last",
            )
            .set_index("SessionDate")["Close"]
            .to_dict()
        )

        atr_by_session = (
            signals
            .dropna(subset=["SessionDate"])
            .drop_duplicates(
                subset="SessionDate",
                keep="last",
            )
            .set_index("SessionDate")["ATR14Pct"]
            .to_dict()
        )

        candidate_sessions = set(
            price_by_session
        )

        for session_date, day in (
            intraday.groupby("Date_Paris")
        ):
            session_date = str(
                session_date
            )[:10]

            if session_date not in candidate_sessions:
                continue

            entry = detect_entry(
                ticker=ticker,
                session_date=session_date,
                day=day,
                signal_price=(
                    price_by_session.get(
                        session_date
                    )
                ),
                atr_pct=(
                    atr_by_session.get(
                        session_date
                    )
                ),
                max_bars=max_bars,
            )

            if entry is not None:
                entry["EntryWindow"] = (
                    window_name
                )
                entries.append(entry)

    entries_by_window[window_name] = entries

    missing_atr = sum(
        pd.isna(entry.get("ATR14Pct"))
        for entry in entries
    )

    print(
        f"{window_name}: "
        f"{len(entries)} entrees, "
        f"ATR manquant={missing_atr}"
    )


# ============================================================
# SIMULATION DES TROIS FINALISTES
# ============================================================

all_trade_rows = []
strategy_summary_rows = []

for strategy in FINALISTS:
    entries = entries_by_window[
        strategy["EntryWindow"]
    ]

    if strategy["EntryType"] == "COMBINE":
        selected_entries = entries
    else:
        selected_entries = [
            entry
            for entry in entries
            if entry["Decision"]
            == strategy["EntryType"]
        ]

    strategy_rows = []

    for entry in selected_entries:
        tp_pct = resolve_tp(
            entry=entry,
            mode=strategy["TPMode"],
            value=strategy["TPValue"],
        )

        sl_pct = resolve_sl(
            entry=entry,
            mode=strategy["SLMode"],
            value=strategy["SLValue"],
        )

        if tp_pct is None or sl_pct is None:
            continue

        trade = simulate_trade(
            full_intraday=(
                intraday_by_ticker[
                    entry["Ticker"]
                ]
            ),
            entry=entry,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            horizon=strategy["Horizon"],
            weekend_policy=(
                strategy["WeekendPolicy"]
            ),
        )

        if trade is None:
            continue

        row = {
            **entry,
            **trade,
            "Strategy": strategy["Name"],
            "TPMode": strategy["TPMode"],
            "TPValue": strategy["TPValue"],
            "TPPct": tp_pct,
            "SLMode": strategy["SLMode"],
            "SLValue": strategy["SLValue"],
            "SLPct": sl_pct,
            "Horizon": strategy["Horizon"],
            "WeekendPolicy": (
                strategy["WeekendPolicy"]
            ),
        }

        all_trade_rows.append(row)

        if trade.get("TradeStatus") == "TERMINE":
            strategy_rows.append(row)

    strategy_frame = pd.DataFrame(
        strategy_rows
    )

    if strategy_frame.empty:
        continue

    strategy_frame = strategy_frame.sort_values(
        [
            "SessionDate",
            "Ticker",
        ]
    )

    overall_metrics = calculate_metrics(
        strategy_frame["NetReturnPct"]
    )

    strategy_summary_rows.append(
        {
            "Strategy": strategy["Name"],
            "EntryWindow": strategy["EntryWindow"],
            "CompletedTrades": len(strategy_frame),
            "IncompleteTrades": (
                len(selected_entries)
                - len(strategy_frame)
            ),
            "TargetRatePct": (
                strategy_frame["TargetHit"]
                .mean()
                * 100.0
            ),
            "StopRatePct": (
                strategy_frame["StopHit"]
                .mean()
                * 100.0
            ),
            "GapExitRatePct": (
                strategy_frame["GapExit"]
                .mean()
                * 100.0
            ),
            "MeanTPPct": (
                strategy_frame["TPPct"].mean()
            ),
            "MeanSLPct": (
                strategy_frame["SLPct"].mean()
            ),
            **overall_metrics,
        }
    )


# ============================================================
# WALK-FORWARD DES FINALISTES
# ============================================================

trades = pd.DataFrame(all_trade_rows)

if trades.empty:
    raise SystemExit(
        "Aucun trade simulé."
    )

completed_trades = trades.loc[
    trades["TradeStatus"] == "TERMINE"
].copy()

all_dates = set(
    completed_trades["SessionDate"]
)

walk_forward_periods = (
    build_walk_forward_periods(
        all_dates
    )
)

walk_forward_rows = []

for strategy in FINALISTS:
    strategy_name = strategy["Name"]

    strategy_trades = completed_trades.loc[
        completed_trades["Strategy"]
        == strategy_name
    ].copy()

    strategy_trades = (
        strategy_trades
        .sort_values(
            [
                "SessionDate",
                "Ticker",
            ]
        )
    )

    for period in walk_forward_periods:
        train = strategy_trades.loc[
            strategy_trades["SessionDate"]
            .isin(period["TrainDates"])
        ].copy()

        test = strategy_trades.loc[
            strategy_trades["SessionDate"]
            .isin(period["TestDates"])
        ].copy()

        train_metrics = calculate_metrics(
            train["NetReturnPct"]
        )

        test_metrics = calculate_metrics(
            test["NetReturnPct"]
        )

        walk_forward_rows.append(
            {
                "Strategy": strategy_name,
                "Fold": period["Fold"],
                "TrainStart": period["TrainStart"],
                "TrainEnd": period["TrainEnd"],
                "TestStart": period["TestStart"],
                "TestEnd": period["TestEnd"],
                **{
                    f"Train_{key}": value
                    for key, value
                    in train_metrics.items()
                },
                **{
                    f"Test_{key}": value
                    for key, value
                    in test_metrics.items()
                },
                "FoldValid": (
                    train_metrics["Trades"]
                    >= MIN_TRADES_PER_FOLD
                    and test_metrics["Trades"]
                    >= MIN_TRADES_PER_FOLD
                ),
            }
        )


# ============================================================
# CONSOLIDATION WALK-FORWARD
# ============================================================

walk_forward = pd.DataFrame(
    walk_forward_rows
)

strategy_summary = pd.DataFrame(
    strategy_summary_rows
)

robustness_rows = []

for strategy_name, group in (
    walk_forward.groupby("Strategy")
):
    valid_group = group.loc[
        group["FoldValid"]
    ].copy()

    if valid_group.empty:
        robustness_rows.append(
            {
                "Strategy": strategy_name,
                "ValidFolds": 0,
                "PositiveTestFolds": 0,
                "WalkForwardPassRatePct": np.nan,
                "MeanTestExpectancyPct": np.nan,
                "MedianTestExpectancyPct": np.nan,
                "MeanTestWinRatePct": np.nan,
                "MeanTestProfitFactor": np.nan,
                "WorstFoldExpectancyPct": np.nan,
                "WalkForwardStatus": (
                    "ECHANTILLON INSUFFISANT"
                ),
            }
        )
        continue

    positive_folds = int(
        (
            valid_group[
                "Test_ExpectancyPct"
            ] > 0
        ).sum()
    )

    valid_folds = len(valid_group)

    pass_rate = (
        positive_folds
        / valid_folds
        * 100.0
    )

    if (
        valid_folds >= 2
        and pass_rate >= 66.0
        and valid_group[
            "Test_ExpectancyPct"
        ].mean() > 0
    ):
        status = "FINALISTE WALK-FORWARD"
    else:
        status = "NON VALIDE"

    robustness_rows.append(
        {
            "Strategy": strategy_name,
            "ValidFolds": valid_folds,
            "PositiveTestFolds": positive_folds,
            "WalkForwardPassRatePct": pass_rate,
            "MeanTestExpectancyPct": (
                valid_group[
                    "Test_ExpectancyPct"
                ].mean()
            ),
            "MedianTestExpectancyPct": (
                valid_group[
                    "Test_ExpectancyPct"
                ].median()
            ),
            "MeanTestWinRatePct": (
                valid_group[
                    "Test_WinRatePct"
                ].mean()
            ),
            "MeanTestProfitFactor": (
                valid_group[
                    "Test_ProfitFactor"
                ]
                .replace(
                    [np.inf, -np.inf],
                    np.nan,
                )
                .mean()
            ),
            "WorstFoldExpectancyPct": (
                valid_group[
                    "Test_ExpectancyPct"
                ].min()
            ),
            "WalkForwardStatus": status,
        }
    )

robustness = pd.DataFrame(
    robustness_rows
)

final_comparison = (
    strategy_summary
    .merge(
        robustness,
        on="Strategy",
        how="left",
    )
    .sort_values(
        [
            "WalkForwardStatus",
            "WalkForwardPassRatePct",
            "MeanTestExpectancyPct",
            "ExpectancyPct",
        ],
        ascending=[
            True,
            False,
            False,
            False,
        ],
    )
)


# ============================================================
# SAUVEGARDE
# ============================================================

trades_file = (
    OUTPUT_DIR
    / "walk_forward_finalist_trades.csv"
)

walk_forward_file = (
    OUTPUT_DIR
    / "walk_forward_folds.csv"
)

comparison_file = (
    OUTPUT_DIR
    / "walk_forward_final_comparison.csv"
)

completed_trades.to_csv(
    trades_file,
    index=False,
    encoding="utf-8",
)

walk_forward.to_csv(
    walk_forward_file,
    index=False,
    encoding="utf-8",
)

final_comparison.to_csv(
    comparison_file,
    index=False,
    encoding="utf-8",
)


# ============================================================
# AFFICHAGE CONCIS
# ============================================================

pd.set_option(
    "display.max_columns",
    None,
)

pd.set_option(
    "display.width",
    260,
)

print()
print("=" * 160)
print("COMPARAISON GLOBALE DES FINALISTES")
print("=" * 160)

comparison_columns = [
    "Strategy",
    "CompletedTrades",
    "TargetRatePct",
    "StopRatePct",
    "MeanTPPct",
    "MeanSLPct",
    "WinRatePct",
    "ExpectancyPct",
    "ProfitFactor",
    "MaxDrawdownPct",
    "ValidFolds",
    "PositiveTestFolds",
    "WalkForwardPassRatePct",
    "MeanTestExpectancyPct",
    "WorstFoldExpectancyPct",
    "WalkForwardStatus",
]

print(
    final_comparison[
        comparison_columns
    ]
    .round(3)
    .to_string(index=False)
)

print()
print("=" * 160)
print("DETAIL WALK-FORWARD")
print("=" * 160)

walk_forward_columns = [
    "Strategy",
    "Fold",
    "TrainStart",
    "TrainEnd",
    "TestStart",
    "TestEnd",
    "Train_Trades",
    "Test_Trades",
    "Train_ExpectancyPct",
    "Test_ExpectancyPct",
    "Test_WinRatePct",
    "Test_ProfitFactor",
    "Test_MaxDrawdownPct",
    "FoldValid",
]

print(
    walk_forward[
        walk_forward_columns
    ]
    .round(3)
    .to_string(index=False)
)

print()
print("=" * 120)
print("FICHIERS GENERES")
print("=" * 120)
print("Trades :", trades_file)
print("Folds :", walk_forward_file)
print("Comparaison :", comparison_file)
print("=" * 120)
print("FIN DU TEST WALK-FORWARD")
print("=" * 120)
