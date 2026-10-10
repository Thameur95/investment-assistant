from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
INTRADAY_DIR = BASE_DIR / "data" / "intraday"
DAILY_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
OUTPUT_DIR = BASE_DIR / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INITIAL_CAPITAL = 3000.0
MAX_POSITIONS = 3
MAX_SHARE_PRICE = 2000.0
FEE_SIDE_PCT = 0.05
SLIPPAGE_SIDE_PCT = 0.05
ENTRY_BARS = 12
HORIZON = 4

ALLOCATION_MODELS = [
    {"name": "FIXED_1000", "mode": "FIXED", "risk_pct": None, "cap": 1000.0},
    {"name": "RISK_0_75", "mode": "RISK", "risk_pct": 0.75, "cap": 1000.0},
    {"name": "RISK_1_00", "mode": "RISK", "risk_pct": 1.00, "cap": 1000.0},
    {"name": "RISK_1_25", "mode": "RISK", "risk_pct": 1.25, "cap": 1000.0},
]


def prepare_intraday(df):
    needed = {"Datetime_Paris", "Date_Paris", "Open", "High", "Low", "Close", "Volume"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError("Colonnes intraday manquantes: " + ", ".join(sorted(missing)))
    x = df.copy()
    x["Datetime_Paris"] = pd.to_datetime(x["Datetime_Paris"], errors="coerce")
    x["Date_Paris"] = x["Date_Paris"].astype(str).str[:10]
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.dropna(subset=list(needed)).sort_values("Datetime_Paris").reset_index(drop=True)


def calculate_atr(daily, period=14):
    x = daily.copy()
    prev = x["Close"].shift(1)
    tr = pd.concat([
        x["High"] - x["Low"],
        (x["High"] - prev).abs(),
        (x["Low"] - prev).abs(),
    ], axis=1).max(axis=1)
    x["ATR14"] = tr.rolling(period, min_periods=period).mean()
    x["ATR14Pct"] = x["ATR14"] / x["Close"] * 100.0
    return x


def add_vwap(day):
    x = day.copy()
    typical = (x["High"] + x["Low"] + x["Close"]) / 3.0
    cv = x["Volume"].cumsum()
    x["VWAP"] = (typical * x["Volume"]).cumsum() / cv.replace(0, np.nan)
    return x


def detect_entry(ticker, date, day, signal_price, atr_pct):
    day = day.sort_values("Datetime_Paris").reset_index(drop=True).copy()
    if len(day) < 4 or signal_price is None or float(signal_price) <= 0:
        return None
    opening = float(day["Open"].iloc[0])
    gap = (opening / float(signal_price) - 1.0) * 100.0
    first4 = day.iloc[:4]
    or_high = float(first4["High"].max())
    or_low = float(first4["Low"].min())
    close4 = float(first4["Close"].iloc[-1])
    red4 = int((first4["Close"] < first4["Open"]).sum())
    vols = first4.loc[first4["Volume"] > 0, "Volume"]
    vol4 = float(vols.mean()) if not vols.empty else 0.0
    day = add_vwap(day)

    decision = "EN ATTENTE"
    reason = "Prix dans OR"
    invalid_pos = 3
    entry_time = entry_price = None

    if gap <= -3:
        decision, reason = "INVALIDE", "Gap baissier >= 3%"
    elif close4 <= opening * 0.98:
        decision, reason = "INVALIDE", "Baisse >= 2% apres 20 minutes"
    elif red4 >= 3 and close4 < opening:
        decision, reason = "INVALIDE", "3 bougies rouges sur 4"
    else:
        for pos, row in day.iloc[:ENTRY_BARS].iloc[4:].iterrows():
            low = float(row["Low"])
            close = float(row["Close"])
            volume = float(row["Volume"])
            vwap = row["VWAP"]
            if pd.isna(vwap):
                continue
            if low < or_low or close <= opening * 0.98:
                decision, reason, invalid_pos = "INVALIDE", "Cassure OR Low", int(pos)
                break
            if close > or_high and close > float(vwap) and volume >= vol4:
                decision, reason = "ACHAT CONFIRME", "OR High + VWAP + volume"
                entry_time, entry_price = row["Datetime_Paris"], close
                break
        if decision == "EN ATTENTE" and len(day) >= ENTRY_BARS:
            decision, reason, invalid_pos = "INVALIDE", "Pas de confirmation en 60 min", ENTRY_BARS - 1

    if decision == "INVALIDE":
        for _, row in day.iloc[invalid_pos + 1:].iterrows():
            close = float(row["Close"])
            volume = float(row["Volume"])
            vwap = row["VWAP"]
            if pd.isna(vwap):
                continue
            if close > or_high and close > float(vwap) and volume >= vol4 * 1.5:
                decision, reason = "ACHAT REACTIVE", "Retournement OR + VWAP + volume"
                entry_time, entry_price = row["Datetime_Paris"], close
                break

    if decision not in ["ACHAT CONFIRME", "ACHAT REACTIVE"]:
        return None

    return {
        "Ticker": ticker,
        "SessionDate": date,
        "Decision": decision,
        "Reason": reason,
        "DecisionTime": entry_time,
        "DecisionPrice": entry_price,
        "OpeningPrice": opening,
        "ATR14Pct": float(atr_pct) if atr_pct is not None and not pd.isna(atr_pct) else np.nan,
        "MoveBeforeEntryPct": (entry_price / opening - 1.0) * 100.0,
    }


def hybrid_tp(atr):
    if atr < 1.5:
        return 1.5
    if atr < 2.5:
        return 2.0
    return min(3.0, max(2.0, atr * 0.75))


def atr_sl(atr):
    return min(3.0, max(0.75, atr))


def simulate_trade(intraday, entry):
    if pd.isna(entry["ATR14Pct"]):
        return None
    tp_pct = hybrid_tp(float(entry["ATR14Pct"]))
    sl_pct = atr_sl(float(entry["ATR14Pct"]))
    raw_entry = float(entry["DecisionPrice"])
    effective_entry = raw_entry * (1.0 + SLIPPAGE_SIDE_PCT / 100.0)
    target = raw_entry * (1.0 + tp_pct / 100.0)
    stop = raw_entry * (1.0 - sl_pct / 100.0)
    dates = sorted(intraday["Date_Paris"].unique())
    date = entry["SessionDate"]
    if date not in dates or dates.index(date) + HORIZON >= len(dates):
        return {"TradeStatus": "DONNEES INCOMPLETES"}
    start = dates.index(date)
    end_date = dates[start + HORIZON]
    bars = intraday.loc[
        (intraday["Datetime_Paris"] > pd.Timestamp(entry["DecisionTime"]))
        & (intraday["Date_Paris"] <= end_date)
    ].copy()
    if bars.empty:
        return {"TradeStatus": "DONNEES INCOMPLETES"}

    exit_reason = exit_time = raw_exit = holding = None
    target_hit = stop_hit = gap_exit = ambiguous = False

    for d, session in bars.groupby("Date_Paris", sort=True):
        session = session.sort_values("Datetime_Paris").reset_index(drop=True)
        offset = dates.index(d) - start
        label = "J" if offset == 0 else f"J+{offset}"
        first = session.iloc[0]
        op = float(first["Open"])
        if op <= stop:
            exit_reason, exit_time, raw_exit = "GAP SOUS STOP", first["Datetime_Paris"], op
            stop_hit, gap_exit, holding = True, True, label
            break
        if op >= target:
            exit_reason, exit_time, raw_exit = "GAP AU-DESSUS TARGET", first["Datetime_Paris"], op
            target_hit, gap_exit, holding = True, True, label
            break
        for _, row in session.iterrows():
            ht = float(row["High"]) >= target
            hs = float(row["Low"]) <= stop
            if ht and hs:
                exit_reason, raw_exit, stop_hit, ambiguous = "TP ET SL MEME BOUGIE - SL", stop, True, True
            elif hs:
                exit_reason, raw_exit, stop_hit = "STOP", stop, True
            elif ht:
                exit_reason, raw_exit, target_hit = "TARGET", target, True
            if exit_reason:
                exit_time, holding = row["Datetime_Paris"], label
                break
        if exit_reason:
            break

    if not exit_reason:
        last = bars.loc[bars["Date_Paris"] == end_date].iloc[-1]
        exit_reason, exit_time, raw_exit, holding = f"SORTIE J+{HORIZON}", last["Datetime_Paris"], float(last["Close"]), f"J+{HORIZON}"

    effective_exit = raw_exit * (1.0 - SLIPPAGE_SIDE_PCT / 100.0)
    net_pct = (effective_exit / effective_entry - 1.0) * 100.0 - 2.0 * FEE_SIDE_PCT
    return {
        "TradeStatus": "TERMINE",
        "ExitReason": exit_reason,
        "ExitTime": exit_time,
        "RawEntryPrice": raw_entry,
        "EffectiveEntryPrice": effective_entry,
        "RawExitPrice": raw_exit,
        "EffectiveExitPrice": effective_exit,
        "NetReturnPct": net_pct,
        "TPPct": tp_pct,
        "SLPct": sl_pct,
        "TargetHit": target_hit,
        "StopHit": stop_hit,
        "GapExit": gap_exit,
        "AmbiguousBar": ambiguous,
        "HoldingSession": holding,
    }


def equity_at_cost(cash, positions):
    return cash + sum(p["TotalEntryCost"] for p in positions)


def close_due(now, positions, cash, ledger, equity):
    due = [p for p in positions if p["ExitTime"] <= now]
    open_pos = [p for p in positions if p["ExitTime"] > now]
    for p in due:
        gross = p["Quantity"] * p["EffectiveExitPrice"]
        fee = gross * FEE_SIDE_PCT / 100.0
        net = gross - fee
        cash += net
        pnl = net - p["TotalEntryCost"]
        ledger.append({
            **p,
            "ExitGrossEUR": gross,
            "ExitFeeEUR": fee,
            "ExitNetEUR": net,
            "PnLEUR": pnl,
            "ReturnPct": pnl / p["TotalEntryCost"] * 100.0,
            "RMultiple": pnl / p["PlannedRiskEUR"] if p["PlannedRiskEUR"] > 0 else np.nan,
            "CashAfterExitEUR": cash,
        })
    if due:
        equity.append({"Datetime": now, "EquityEUR": equity_at_cost(cash, open_pos)})
    return cash, open_pos


def drawdown(equity_points):
    x = pd.DataFrame(equity_points).sort_values("Datetime").drop_duplicates("Datetime", keep="last")
    x["Peak"] = x["EquityEUR"].cummax()
    x["DDEUR"] = x["EquityEUR"] - x["Peak"]
    x["DDPct"] = x["DDEUR"] / x["Peak"] * 100.0
    return float(x["DDEUR"].min()), float(x["DDPct"].min())


def order_budget(model, equity, sl_pct):
    if model["mode"] == "FIXED":
        return model["cap"], np.nan
    risk_eur = equity * model["risk_pct"] / 100.0
    return min(model["cap"], risk_eur / (sl_pct / 100.0)), risk_eur


def simulate_portfolio(trades, model):
    x = trades.loc[trades["TradeStatus"] == "TERMINE"].copy()
    x["DecisionTime"] = pd.to_datetime(x["DecisionTime"], errors="coerce")
    x["ExitTime"] = pd.to_datetime(x["ExitTime"], errors="coerce")
    x = x.dropna(subset=["DecisionTime", "ExitTime", "EffectiveEntryPrice", "EffectiveExitPrice"])
    x["Priority"] = x["Decision"].map({"ACHAT CONFIRME": 0, "ACHAT REACTIVE": 1}).fillna(2)
    x = x.sort_values(["DecisionTime", "Priority", "Ticker"])

    cash = INITIAL_CAPITAL
    positions, ledger, rejects = [], [], []
    equity = [{"Datetime": x["DecisionTime"].min(), "EquityEUR": INITIAL_CAPITAL}]

    for _, t in x.iterrows():
        now = t["DecisionTime"]
        cash, positions = close_due(now, positions, cash, ledger, equity)
        if len(positions) >= MAX_POSITIONS:
            rejects.append({"Model": model["name"], "Ticker": t["Ticker"], "DecisionTime": now, "Reason": "MAX_POSITIONS", "CashEUR": cash})
            continue
        current_equity = equity_at_cost(cash, positions)
        price = float(t["EffectiveEntryPrice"])
        sl_pct = float(t["SLPct"])
        budget, risk_budget = order_budget(model, current_equity, sl_pct)
        if price > MAX_SHARE_PRICE:
            rejects.append({"Model": model["name"], "Ticker": t["Ticker"], "DecisionTime": now, "Reason": "PRICE_ABOVE_2000", "CashEUR": cash})
            continue
        qty = int(budget // price)
        if qty < 1:
            one_share_risk = price * sl_pct / 100.0
            allowed = model["mode"] == "FIXED" or (not pd.isna(risk_budget) and one_share_risk <= risk_budget)
            if allowed and price <= cash:
                qty = 1
        gross = qty * price
        fee = gross * FEE_SIDE_PCT / 100.0
        cost = gross + fee
        while qty > 0 and cost > cash:
            qty -= 1
            gross = qty * price
            fee = gross * FEE_SIDE_PCT / 100.0
            cost = gross + fee
        if qty <= 0:
            rejects.append({"Model": model["name"], "Ticker": t["Ticker"], "DecisionTime": now, "Reason": "BUDGET_OR_CASH", "CashEUR": cash, "BudgetEUR": budget})
            continue
        planned_risk = gross * sl_pct / 100.0
        cash -= cost
        positions.append({
            "Model": model["name"], "Ticker": t["Ticker"], "SessionDate": t["SessionDate"],
            "Decision": t["Decision"], "DecisionTime": now, "ExitTime": t["ExitTime"],
            "ExitReason": t["ExitReason"], "Quantity": qty,
            "EffectiveEntryPrice": price, "EffectiveExitPrice": float(t["EffectiveExitPrice"]),
            "EntryGrossEUR": gross, "EntryFeeEUR": fee, "TotalEntryCost": cost,
            "BudgetEUR": budget, "RiskBudgetEUR": risk_budget, "PlannedRiskEUR": planned_risk,
            "PlannedRiskPctEquity": planned_risk / current_equity * 100.0,
            "TPPct": float(t["TPPct"]), "SLPct": sl_pct, "CashAfterEntryEUR": cash,
        })
        equity.append({"Datetime": now, "EquityEUR": equity_at_cost(cash, positions)})

    if positions:
        cash, positions = close_due(max(p["ExitTime"] for p in positions), positions, cash, ledger, equity)

    led = pd.DataFrame(ledger)
    rej = pd.DataFrame(rejects)
    if led.empty:
        return {}, led, rej
    winners = led.loc[led["PnLEUR"] > 0]
    losers = led.loc[led["PnLEUR"] < 0]
    gp, gl = float(winners["PnLEUR"].sum()), abs(float(losers["PnLEUR"].sum()))
    pf = gp / gl if gl > 0 else np.inf
    dd_eur, dd_pct = drawdown(equity)
    summary = {
        "AllocationModel": model["name"], "FinalCapitalEUR": cash,
        "PnLEUR": cash - INITIAL_CAPITAL, "ReturnPct": (cash / INITIAL_CAPITAL - 1.0) * 100.0,
        "ExecutedTrades": len(led), "RejectedTrades": len(rej),
        "WinRatePct": len(winners) / len(led) * 100.0, "ProfitFactor": pf,
        "MaxDrawdownEUR": dd_eur, "MaxDrawdownPct": dd_pct,
        "AveragePositionEUR": float(led["EntryGrossEUR"].mean()),
        "AverageRiskEUR": float(led["PlannedRiskEUR"].mean()),
        "AverageRiskPctEquity": float(led["PlannedRiskPctEquity"].mean()),
        "AverageRMultiple": float(led["RMultiple"].mean()),
        "LargestWinEUR": float(led["PnLEUR"].max()), "LargestLossEUR": float(led["PnLEUR"].min()),
    }
    return summary, led, rej


print("=" * 120)
print("HISTORICAL LAB - RISK MANAGER")
print("MOTEUR FIGE HYBRID ATR / FENETRE 60 MIN / HORIZON J+4")
print("=" * 120)

daily_all = pd.read_csv(DAILY_FILE)
needed_daily = {"Date", "Ticker", "High", "Low", "Close", "Signal"}
missing = needed_daily - set(daily_all.columns)
if missing:
    raise ValueError("Colonnes daily manquantes: " + ", ".join(sorted(missing)))
daily_all["Date"] = pd.to_datetime(daily_all["Date"], errors="coerce")
for c in ["High", "Low", "Close"]:
    daily_all[c] = pd.to_numeric(daily_all[c], errors="coerce")

intraday_by_ticker = {}
for path in sorted(INTRADAY_DIR.glob("*_5m.csv")):
    ticker = path.stem.replace("_5m", "")
    intraday_by_ticker[ticker] = prepare_intraday(pd.read_csv(path))

entries = []
for ticker, intraday in intraday_by_ticker.items():
    daily = daily_all.loc[daily_all["Ticker"] == ticker].copy()
    daily = daily.dropna(subset=["Date", "High", "Low", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    if daily.empty:
        continue
    daily = calculate_atr(daily)
    daily["SessionDate"] = daily["Date"].shift(-1).dt.strftime("%Y-%m-%d")
    signals = daily.loc[daily["Signal"] == "ACHAT"].copy()
    price_map = signals.dropna(subset=["SessionDate", "Close"]).drop_duplicates("SessionDate", keep="last").set_index("SessionDate")["Close"].to_dict()
    atr_map = signals.dropna(subset=["SessionDate"]).drop_duplicates("SessionDate", keep="last").set_index("SessionDate")["ATR14Pct"].to_dict()
    for date, day in intraday.groupby("Date_Paris"):
        date = str(date)[:10]
        if date not in price_map:
            continue
        entry = detect_entry(ticker, date, day, price_map.get(date), atr_map.get(date))
        if entry is not None:
            entries.append(entry)

print("Entrées détectées :", len(entries))

trade_rows = []
for entry in entries:
    trade = simulate_trade(intraday_by_ticker[entry["Ticker"]], entry)
    if trade is not None:
        trade_rows.append({**entry, **trade})

trades = pd.DataFrame(trade_rows)
trades_file = OUTPUT_DIR / "risk_manager_source_trades.csv"
trades.to_csv(trades_file, index=False, encoding="utf-8")

summaries, ledgers, rejections = [], [], []
for model in ALLOCATION_MODELS:
    summary, ledger, rejected = simulate_portfolio(trades, model)
    if summary:
        summaries.append(summary)
    if not ledger.empty:
        ledgers.append(ledger)
    if not rejected.empty:
        rejections.append(rejected)

summary_df = pd.DataFrame(summaries).sort_values(["FinalCapitalEUR", "ProfitFactor"], ascending=False)
ledger_df = pd.concat(ledgers, ignore_index=True) if ledgers else pd.DataFrame()
rejection_df = pd.concat(rejections, ignore_index=True) if rejections else pd.DataFrame()

summary_file = OUTPUT_DIR / "risk_manager_summary.csv"
ledger_file = OUTPUT_DIR / "risk_manager_ledger.csv"
rejection_file = OUTPUT_DIR / "risk_manager_rejections.csv"
summary_df.to_csv(summary_file, index=False, encoding="utf-8")
ledger_df.to_csv(ledger_file, index=False, encoding="utf-8")
rejection_df.to_csv(rejection_file, index=False, encoding="utf-8")

print()
print("=" * 180)
print("COMPARAISON DES ALLOCATIONS")
print("=" * 180)
columns = [
    "AllocationModel", "FinalCapitalEUR", "PnLEUR", "ReturnPct", "ExecutedTrades",
    "RejectedTrades", "WinRatePct", "ProfitFactor", "MaxDrawdownEUR", "MaxDrawdownPct",
    "AveragePositionEUR", "AverageRiskEUR", "AverageRiskPctEquity", "AverageRMultiple",
    "LargestWinEUR", "LargestLossEUR",
]
print(summary_df[columns].round(3).to_string(index=False))
print()
print("Sources :", trades_file)
print("Résumé :", summary_file)
print("Journal :", ledger_file)
print("Rejets :", rejection_file)
print("=" * 180)
print("FIN DU TEST RISK MANAGER")
print("=" * 180)
