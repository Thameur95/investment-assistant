import itertools
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# CONFIGURATION CENTRALE DU LABORATOIRE
# ============================================================
BASE_DIR = Path(__file__).resolve().parent.parent
INTRADAY_DIR = BASE_DIR / "data" / "intraday"
DAILY_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_DIR = BASE_DIR / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ENTRY_WINDOWS = {"60 min": 12, "90 min": 18, "120 min": 24, "Toute seance": None}
ENTRY_TYPES = ["ACHAT CONFIRME", "ACHAT REACTIVE", "COMBINE"]
TP_FIXED = [1.5, 2.0, 2.5, 3.0]
TP_ATR_MULTIPLIERS = [0.75, 1.0, 1.25]
SL_FIXED = [1.0, 1.5, 2.0, 2.5]
SL_ATR_MULTIPLIERS = [0.50, 0.75, 1.0]
HORIZONS = [1, 2, 3, 4, 5]
WEEKEND_POLICIES = ["HOLD_WEEKEND", "FRIDAY_FLAT"]

ATR_PERIOD = 14
TP_MIN = 1.0
TP_MAX = 4.0
SL_MIN = 0.75
SL_MAX = 3.0

# Frais et slippage en pourcentage de la valeur de la position, par côté.
FEE_PCT_PER_SIDE = 0.05
SLIPPAGE_PCT_PER_SIDE = 0.05

# Découpage chronologique global.
TRAIN_RATIO = 0.60
VALIDATION_RATIO = 0.20
MIN_TRADES_RANKING = 5


def prepare_intraday_data(df):
    required = {"Datetime_Paris", "Date_Paris", "Open", "High", "Low", "Close", "Volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError("Colonnes manquantes: " + ", ".join(sorted(missing)))
    out = df.copy()
    out["Datetime_Paris"] = pd.to_datetime(out["Datetime_Paris"], errors="coerce")
    out["Date_Paris"] = out["Date_Paris"].astype(str).str[:10]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return (
        out.dropna(subset=["Datetime_Paris", "Date_Paris", "Open", "High", "Low", "Close", "Volume"])
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )


def add_vwap(day):
    out = day.copy()
    typical = (out["High"] + out["Low"] + out["Close"]) / 3.0
    cum_volume = out["Volume"].cumsum()
    out["VWAP"] = (typical * out["Volume"]).cumsum() / cum_volume.replace(0, np.nan)
    return out


def calculate_atr(daily, period=ATR_PERIOD):
    out = daily.copy()
    previous_close = out["Close"].shift(1)
    true_range = pd.concat(
        [
            out["High"] - out["Low"],
            (out["High"] - previous_close).abs(),
            (out["Low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["ATR14"] = true_range.rolling(period, min_periods=period).mean()
    out["ATR14Pct"] = out["ATR14"] / out["Close"] * 100.0
    return out


def detect_entry(ticker, session_date, day, signal_price, atr_pct, max_bars):
    day = day.sort_values("Datetime_Paris").reset_index(drop=True).copy()
    if len(day) < 4 or signal_price is None or float(signal_price) <= 0:
        return None

    signal_price = float(signal_price)
    opening = float(day["Open"].iloc[0])
    if opening <= 0:
        return None

    gap_pct = (opening / signal_price - 1.0) * 100.0
    first4 = day.iloc[:4].copy()
    or_high = float(first4["High"].max())
    or_low = float(first4["Low"].min())
    close4 = float(first4["Close"].iloc[-1])
    red4 = int((first4["Close"] < first4["Open"]).sum())
    initial_volumes = first4.loc[first4["Volume"] > 0, "Volume"]
    initial_avg_volume = float(initial_volumes.mean()) if not initial_volumes.empty else 0.0
    day = add_vwap(day)

    decision = "EN ATTENTE"
    reason = "Prix dans Opening Range"
    decision_time = None
    decision_price = None
    decision_volume = None
    decision_vwap = None
    invalid_position = 3

    if gap_pct <= -3:
        decision, reason = "SETUP INITIAL INVALIDE", "Gap baissier >= 3%"
    elif close4 <= opening * 0.98:
        decision, reason = "SETUP INITIAL INVALIDE", "Baisse >= 2% apres 20 minutes"
    elif red4 >= 3 and close4 < opening:
        decision, reason = "SETUP INITIAL INVALIDE", "Au moins 3 bougies rouges sur 4"
    else:
        window = day.copy() if max_bars is None else day.iloc[:max_bars].copy()
        for position, row in window.iloc[4:].iterrows():
            current_low = float(row["Low"])
            current_close = float(row["Close"])
            current_volume = float(row["Volume"])
            current_vwap = row["VWAP"]
            if pd.isna(current_vwap):
                continue
            current_vwap = float(current_vwap)
            if current_low < or_low or current_close <= opening * 0.98:
                decision = "SETUP INITIAL INVALIDE"
                reason = "Cassure baissiere Opening Range"
                invalid_position = int(position)
                break
            if (
                current_close > or_high
                and current_close > current_vwap
                and current_volume >= initial_avg_volume
            ):
                decision = "ACHAT CONFIRME"
                reason = "Cassure haussiere + VWAP + volume"
                decision_time = row["Datetime_Paris"]
                decision_price = current_close
                decision_volume = current_volume
                decision_vwap = current_vwap
                break

        if decision == "EN ATTENTE":
            finished = max_bars is None or len(day) >= max_bars
            if finished:
                decision = "SETUP INITIAL INVALIDE"
                if max_bars is None:
                    reason = "Aucune confirmation pendant la seance"
                    invalid_position = len(day) - 1
                else:
                    reason = f"Aucune confirmation pendant {max_bars * 5} minutes"
                    invalid_position = max_bars - 1

    if decision == "SETUP INITIAL INVALIDE":
        for _, row in day.iloc[invalid_position + 1 :].iterrows():
            close = float(row["Close"])
            volume = float(row["Volume"])
            vwap = row["VWAP"]
            if pd.isna(vwap):
                continue
            vwap = float(vwap)
            if close > or_high and close > vwap and volume >= initial_avg_volume * 1.5:
                decision = "ACHAT REACTIVE"
                reason = "Retournement confirme: OR High + VWAP + volume"
                decision_time = row["Datetime_Paris"]
                decision_price = close
                decision_volume = volume
                decision_vwap = vwap
                break

    is_buy = decision in ["ACHAT CONFIRME", "ACHAT REACTIVE"]
    move_before_entry = None
    if is_buy and decision_price is not None:
        move_before_entry = (decision_price / opening - 1.0) * 100.0

    return {
        "Ticker": ticker,
        "SessionDate": session_date,
        "SignalPrice": signal_price,
        "OpeningPrice": opening,
        "GapPct": gap_pct,
        "ATR14Pct": atr_pct,
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


def resolve_tp(entry, mode, value):
    atr = entry.get("ATR14Pct")
    if mode == "FIXED":
        return float(value)
    if atr is None or pd.isna(atr):
        return None
    atr = float(atr)
    if mode == "ATR":
        return min(TP_MAX, max(TP_MIN, atr * float(value)))
    if mode == "HYBRID":
        if atr < 1.5:
            return 1.5
        if atr < 2.5:
            return 2.0
        return min(3.0, max(2.0, atr * 0.75))
    raise ValueError(f"Mode TP inconnu: {mode}")


def resolve_sl(entry, mode, value):
    atr = entry.get("ATR14Pct")
    if mode == "FIXED":
        return float(value)
    if atr is None or pd.isna(atr):
        return None
    return min(SL_MAX, max(SL_MIN, float(atr) * float(value)))


def simulate_trade(full_intraday, entry, tp_pct, sl_pct, horizon, weekend_policy):
    entry_time = entry.get("DecisionTime")
    entry_price = entry.get("DecisionPrice")
    entry_date = entry.get("SessionDate")
    if entry_time is None or entry_price is None:
        return None

    entry_time = pd.Timestamp(entry_time)
    raw_entry_price = float(entry_price)
    # Achat pénalisé par slippage et frais. Les frais sont intégrés au rendement net.
    effective_entry_price = raw_entry_price * (1.0 + SLIPPAGE_PCT_PER_SIDE / 100.0)
    target_price = raw_entry_price * (1.0 + tp_pct / 100.0)
    stop_price = raw_entry_price * (1.0 - sl_pct / 100.0)

    dates = sorted(full_intraday["Date_Paris"].unique())
    if entry_date not in dates:
        return None
    entry_pos = dates.index(entry_date)
    forced_pos = entry_pos + int(horizon)
    if forced_pos >= len(dates):
        return {"TradeStatus": "DONNEES INCOMPLETES"}
    forced_date = dates[forced_pos]

    trade_data = full_intraday.loc[
        (full_intraday["Datetime_Paris"] > entry_time)
        & (full_intraday["Date_Paris"] <= forced_date)
    ].copy()
    if trade_data.empty:
        return {"TradeStatus": "DONNEES INCOMPLETES"}

    exit_reason = None
    exit_time = None
    raw_exit_price = None
    target_hit = False
    stop_hit = False
    gap_exit = False
    ambiguous_bar = False
    holding_session = None

    for session_date, session in trade_data.groupby("Date_Paris", sort=True):
        session = session.sort_values("Datetime_Paris").reset_index(drop=True)
        if session.empty:
            continue
        offset = dates.index(session_date) - entry_pos
        label = "J" if offset == 0 else f"J+{offset}"
        first = session.iloc[0]
        session_open = float(first["Open"])

        if session_open <= stop_price:
            exit_reason, raw_exit_price = "GAP SOUS STOP", session_open
            stop_hit, gap_exit, exit_time, holding_session = True, True, first["Datetime_Paris"], label
            break
        if session_open >= target_price:
            exit_reason, raw_exit_price = "GAP AU-DESSUS TARGET", session_open
            target_hit, gap_exit, exit_time, holding_session = True, True, first["Datetime_Paris"], label
            break

        for _, row in session.iterrows():
            hit_target = float(row["High"]) >= target_price
            hit_stop = float(row["Low"]) <= stop_price
            if hit_target and hit_stop:
                exit_reason, raw_exit_price = "TP ET SL MEME BOUGIE - SL PRIORITAIRE", stop_price
                stop_hit, ambiguous_bar = True, True
            elif hit_stop:
                exit_reason, raw_exit_price = "STOP", stop_price
                stop_hit = True
            elif hit_target:
                exit_reason, raw_exit_price = "TARGET", target_price
                target_hit = True
            if exit_reason is not None:
                exit_time, holding_session = row["Datetime_Paris"], label
                break
        if exit_reason is not None:
            break

        if weekend_policy == "FRIDAY_FLAT" and pd.Timestamp(session_date).weekday() == 4:
            last = session.iloc[-1]
            exit_reason = "SORTIE VENDREDI"
            raw_exit_price = float(last["Close"])
            exit_time, holding_session = last["Datetime_Paris"], label
            break

    if exit_reason is None:
        forced = trade_data.loc[trade_data["Date_Paris"] == forced_date]
        if forced.empty:
            return {"TradeStatus": "DONNEES INCOMPLETES"}
        last = forced.iloc[-1]
        exit_reason = f"SORTIE TEMPORELLE J+{horizon}"
        raw_exit_price = float(last["Close"])
        exit_time, holding_session = last["Datetime_Paris"], f"J+{horizon}"

    effective_exit_price = raw_exit_price * (1.0 - SLIPPAGE_PCT_PER_SIDE / 100.0)
    gross_return = (raw_exit_price / raw_entry_price - 1.0) * 100.0
    net_return = (effective_exit_price / effective_entry_price - 1.0) * 100.0
    net_return -= 2.0 * FEE_PCT_PER_SIDE

    return {
        "TradeStatus": "TERMINE",
        "ExitReason": exit_reason,
        "ExitTime": exit_time,
        "RawEntryPrice": raw_entry_price,
        "EffectiveEntryPrice": effective_entry_price,
        "RawExitPrice": raw_exit_price,
        "EffectiveExitPrice": effective_exit_price,
        "GrossReturnPct": gross_return,
        "NetReturnPct": net_return,
        "TargetHit": target_hit,
        "StopHit": stop_hit,
        "GapExit": gap_exit,
        "AmbiguousBar": ambiguous_bar,
        "HoldingSession": holding_session,
    }


def metrics(values):
    s = pd.Series(values, dtype="float64").dropna()
    if s.empty:
        return {}
    winners = s[s > 0]
    losers = s[s < 0]
    gross_profit = float(winners.sum())
    gross_loss = abs(float(losers.sum()))
    pf = gross_profit / gross_loss if gross_loss > 0 else (np.inf if gross_profit > 0 else np.nan)
    equity = (1.0 + s / 100.0).cumprod()
    drawdown = equity / equity.cummax() - 1.0
    return {
        "Trades": len(s),
        "WinRatePct": (s > 0).mean() * 100.0,
        "ExpectancyPct": s.mean(),
        "MedianPct": s.median(),
        "AverageWinPct": winners.mean() if not winners.empty else np.nan,
        "AverageLossPct": losers.mean() if not losers.empty else np.nan,
        "ProfitFactor": pf,
        "MaxDrawdownPct": drawdown.min() * 100.0,
        "WorstTradePct": s.min(),
    }


def split_labels(entries):
    dates = sorted({row["SessionDate"] for row in entries})
    if not dates:
        return {}
    train_end = max(1, int(len(dates) * TRAIN_RATIO))
    validation_end = max(train_end + 1, int(len(dates) * (TRAIN_RATIO + VALIDATION_RATIO)))
    validation_end = min(validation_end, len(dates))
    mapping = {}
    for i, date in enumerate(dates):
        if i < train_end:
            mapping[date] = "TRAIN"
        elif i < validation_end:
            mapping[date] = "VALIDATION"
        else:
            mapping[date] = "TEST"
    return mapping


def build_strategy_grid():
    tp_specs = [("FIXED", x) for x in TP_FIXED] + [("ATR", x) for x in TP_ATR_MULTIPLIERS] + [("HYBRID", 0.0)]
    sl_specs = [("FIXED", x) for x in SL_FIXED] + [("ATR", x) for x in SL_ATR_MULTIPLIERS]
    return list(itertools.product(ENTRY_TYPES, tp_specs, sl_specs, HORIZONS, WEEKEND_POLICIES))


print("=" * 120)
print("HISTORICAL LAB MULTI-TESTS")
print("TP / SL / ATR / HORIZON / WEEK-END / FRAIS / VALIDATION CHRONOLOGIQUE")
print("=" * 120)

daily_all = pd.read_csv(DAILY_FILE)
required_daily = {"Date", "Ticker", "High", "Low", "Close", "Signal"}
missing_daily = required_daily - set(daily_all.columns)
if missing_daily:
    raise ValueError("Colonnes daily manquantes: " + ", ".join(sorted(missing_daily)))

daily_all["Date"] = pd.to_datetime(daily_all["Date"], errors="coerce")
for col in ["High", "Low", "Close"]:
    daily_all[col] = pd.to_numeric(daily_all[col], errors="coerce")

intraday_by_ticker = {}
for path in sorted(INTRADAY_DIR.glob("*_5m.csv")):
    ticker = path.stem.replace("_5m", "")
    try:
        intraday_by_ticker[ticker] = prepare_intraday_data(pd.read_csv(path))
    except Exception as exc:
        print(f"{ticker}: erreur intraday: {exc}")

# Détection une seule fois par fenêtre. Toutes les stratégies de sortie réutilisent les mêmes entrées.
entries_by_window = {}
missing_atr_rows = []
for window_name, max_bars in ENTRY_WINDOWS.items():
    detected = []
    for ticker, intraday in intraday_by_ticker.items():
        daily = daily_all.loc[daily_all["Ticker"] == ticker].copy()
        daily = (
            daily.dropna(subset=["Date", "High", "Low", "Close"])
            .sort_values("Date")
            .drop_duplicates("Date", keep="last")
            .reset_index(drop=True)
        )
        if daily.empty:
            continue
        daily = calculate_atr(daily)
        daily["SessionDate"] = daily["Date"].shift(-1).dt.strftime("%Y-%m-%d")
        signals = daily.loc[daily["Signal"] == "ACHAT"].copy()
        price_map = signals.dropna(subset=["SessionDate", "Close"]).drop_duplicates("SessionDate", keep="last").set_index("SessionDate")["Close"].to_dict()
        atr_map = signals.dropna(subset=["SessionDate"]).drop_duplicates("SessionDate", keep="last").set_index("SessionDate")["ATR14Pct"].to_dict()
        candidates = set(price_map)
        for session_date, day in intraday.groupby("Date_Paris"):
            session_date = str(session_date)[:10]
            if session_date not in candidates:
                continue
            entry = detect_entry(ticker, session_date, day, price_map.get(session_date), atr_map.get(session_date), max_bars)
            if entry and entry["Decision"] in ["ACHAT CONFIRME", "ACHAT REACTIVE"]:
                entry["EntryWindow"] = window_name
                detected.append(entry)
                if entry.get("ATR14Pct") is None or pd.isna(entry.get("ATR14Pct")):
                    missing_atr_rows.append({"EntryWindow": window_name, "Ticker": ticker, "SessionDate": session_date})
    entries_by_window[window_name] = detected
    print(f"{window_name}: {len(detected)} entrees")

all_entries = [row for rows in entries_by_window.values() for row in rows]
if not all_entries:
    raise SystemExit("Aucune entree detectee.")

split_map = split_labels(all_entries)
strategy_grid = build_strategy_grid()
print(f"Configurations testees par fenetre: {len(strategy_grid)}")

trade_rows = []
summary_rows = []
config_id = 0

for window_name, entries in entries_by_window.items():
    for entry_type, tp_spec, sl_spec, horizon, weekend_policy in strategy_grid:
        config_id += 1
        tp_mode, tp_value = tp_spec
        sl_mode, sl_value = sl_spec
        selected_entries = entries if entry_type == "COMBINE" else [e for e in entries if e["Decision"] == entry_type]
        config_trades = []

        for entry in selected_entries:
            tp_pct = resolve_tp(entry, tp_mode, tp_value)
            sl_pct = resolve_sl(entry, sl_mode, sl_value)
            if tp_pct is None or sl_pct is None:
                continue
            trade = simulate_trade(
                intraday_by_ticker[entry["Ticker"]], entry, tp_pct, sl_pct, horizon, weekend_policy
            )
            if trade is None:
                continue
            row = {
                **entry,
                **trade,
                "ConfigId": config_id,
                "EntryTypeFilter": entry_type,
                "TPMode": tp_mode,
                "TPValue": tp_value,
                "TPPct": tp_pct,
                "SLMode": sl_mode,
                "SLValue": sl_value,
                "SLPct": sl_pct,
                "Horizon": horizon,
                "WeekendPolicy": weekend_policy,
                "DatasetSplit": split_map.get(entry["SessionDate"], "UNKNOWN"),
            }
            trade_rows.append(row)
            if trade.get("TradeStatus") == "TERMINE":
                config_trades.append(row)

        if not config_trades:
            continue
        frame = pd.DataFrame(config_trades)
        base = {
            "ConfigId": config_id,
            "EntryWindow": window_name,
            "EntryType": entry_type,
            "TPMode": tp_mode,
            "TPValue": tp_value,
            "SLMode": sl_mode,
            "SLValue": sl_value,
            "Horizon": horizon,
            "WeekendPolicy": weekend_policy,
            "CompletedTrades": len(frame),
            "IncompleteTrades": len(selected_entries) - len(frame),
            "TargetRatePct": frame["TargetHit"].mean() * 100.0,
            "StopRatePct": frame["StopHit"].mean() * 100.0,
            "GapExitRatePct": frame["GapExit"].mean() * 100.0,
            "MeanMoveBeforeEntryPct": frame["MoveBeforeEntryPct"].mean(),
            "MeanTPPct": frame["TPPct"].mean(),
            "MeanSLPct": frame["SLPct"].mean(),
        }
        for split in ["ALL", "TRAIN", "VALIDATION", "TEST"]:
            sub = frame if split == "ALL" else frame.loc[frame["DatasetSplit"] == split]
            m = metrics(sub["NetReturnPct"])
            for key, value in m.items():
                base[f"{split}_{key}"] = value
        summary_rows.append(base)

summary = pd.DataFrame(summary_rows)
trades = pd.DataFrame(trade_rows)
missing_atr = pd.DataFrame(missing_atr_rows).drop_duplicates()

summary_file = OUTPUT_DIR / "multi_test_summary.csv"
trades_file = OUTPUT_DIR / "multi_test_trades.csv"
missing_atr_file = OUTPUT_DIR / "missing_atr_entries.csv"
summary.to_csv(summary_file, index=False, encoding="utf-8")
trades.to_csv(trades_file, index=False, encoding="utf-8")
missing_atr.to_csv(missing_atr_file, index=False, encoding="utf-8")

# Classement robuste: on privilégie TEST, puis VALIDATION, avec filtres minimums.
eligible = summary.loc[
    (summary["ALL_Trades"] >= MIN_TRADES_RANKING)
    & (summary["TRAIN_ExpectancyPct"] > 0)
    & (summary["VALIDATION_ExpectancyPct"] > 0)
    & (summary["TEST_ExpectancyPct"] > 0)
    & (summary["TEST_ProfitFactor"] > 1.0)
].copy()

if eligible.empty:
    print("\nAucune configuration positive sur TRAIN, VALIDATION et TEST.")
    print("Le tableau complet reste disponible dans multi_test_summary.csv.")
else:
    eligible["RobustScore"] = (
        eligible["TEST_ExpectancyPct"] * 0.45
        + eligible["VALIDATION_ExpectancyPct"] * 0.30
        + eligible["TRAIN_ExpectancyPct"] * 0.10
        + eligible["TEST_ProfitFactor"].clip(upper=3.0) * 0.10
        + (eligible["TEST_Trades"].clip(upper=20) / 20.0) * 0.05
    )
    eligible = eligible.sort_values(
        ["RobustScore", "TEST_ExpectancyPct", "TEST_ProfitFactor"], ascending=False
    )
    top_file = OUTPUT_DIR / "multi_test_top_robust.csv"
    eligible.head(30).to_csv(top_file, index=False, encoding="utf-8")

    columns = [
        "ConfigId", "EntryWindow", "EntryType", "TPMode", "TPValue",
        "SLMode", "SLValue", "Horizon", "WeekendPolicy",
        "ALL_Trades", "TRAIN_Trades", "VALIDATION_Trades", "TEST_Trades",
        "TRAIN_ExpectancyPct", "VALIDATION_ExpectancyPct", "TEST_ExpectancyPct",
        "TEST_WinRatePct", "TEST_ProfitFactor", "TEST_MaxDrawdownPct",
        "RobustScore",
    ]
    print("\n" + "=" * 180)
    print("TOP 20 CONFIGURATIONS ROBUSTES")
    print("=" * 180)
    print(eligible[columns].head(20).round(3).to_string(index=False))
    print(f"\nTop robuste sauvegarde: {top_file}")

print("\n" + "=" * 120)
print("CONTROLES")
print("=" * 120)
for name, rows in entries_by_window.items():
    atr_missing = sum(pd.isna(r.get("ATR14Pct")) for r in rows)
    print(f"{name}: entrees={len(rows)}, ATR manquant={atr_missing}")

print(f"\nResume complet: {summary_file}")
print(f"Trades complets: {trades_file}")
print(f"Entrees sans ATR: {missing_atr_file}")
print("=" * 120)
print("FIN DU LABORATOIRE MULTI-TESTS")
print("=" * 120)
