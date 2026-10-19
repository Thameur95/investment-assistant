#!/usr/bin/env python3
"""
structure_shadow_portfolio.py

Portefeuille virtuel entièrement séparé pour les signaux de l'Agent Structure Shadow.

Entrée lue :
  historical_lab/results/structure_shadow_signals.csv

Sorties propres au Shadow :
  historical_lab/results/structure_shadow_portfolio.csv
  historical_lab/results/structure_shadow_cash.csv
  historical_lab/results/structure_shadow_results.csv
  historical_lab/results/structure_shadow_portfolio_events.csv
  historical_lab/results/structure_shadow_portfolio_summary.json

Principes :
- capital théorique fermé de 3 000 EUR ;
- allocation FIXED_1000, actions entières ;
- maximum 3 positions ouvertes ;
- un seul trade ouvert par ticker ;
- exécution à la première bougie 5 minutes disponible après le signal ;
- si le signal est à 15:55 ou plus tard, entrée à l'ouverture de la séance suivante ;
- TP et SL viennent du signal Structure, puis sont recalés sur le vrai prix d'entrée ;
- horizon maximal J+4 séances ;
- gestion conservatrice des gaps ;
- si TP et SL sont touchés dans la même bougie, STOP d'abord ;
- frais et slippage paramétrables ;
- idempotence : un SignalID n'est jamais exécuté deux fois ;
- aucun accès en écriture aux fichiers V1.

IMPORTANT : simulation Shadow uniquement. Aucun ordre réel.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "historical_lab" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SIGNALS_FILE = RESULTS_DIR / "structure_shadow_signals.csv"
PORTFOLIO_FILE = RESULTS_DIR / "structure_shadow_portfolio.csv"
CASH_FILE = RESULTS_DIR / "structure_shadow_cash.csv"
RESULTS_FILE = RESULTS_DIR / "structure_shadow_results.csv"
EVENTS_FILE = RESULTS_DIR / "structure_shadow_portfolio_events.csv"
SUMMARY_FILE = RESULTS_DIR / "structure_shadow_portfolio_summary.json"

INITIAL_CAPITAL_EUR = 3000.0
FIXED_ALLOCATION_EUR = 1000.0
MAX_OPEN_POSITIONS = 3
MAX_HOLDING_SESSIONS = 5  # séance d'entrée incluse : J à J+4
REQUIRED_AGENT_VERSION = "V2_MULTI_HORIZON"
REQUIRED_SIGNAL_POLICY = "NEXT_EXECUTABLE_BAR_ONLY"
MIN_RR = 2.0
MAX_STALE_FUTURE_BARS = 1
STATE_FILE = RESULTS_DIR / "structure_shadow_state.csv"
SLIPPAGE_BPS = 5.0
FX_SLIPPAGE_BPS = 2.0
USD_TICKER_SUFFIXES = (".PA", ".AS")

PORTFOLIO_FIELDS = [
    "PositionID", "SignalID", "Ticker", "Market", "Status", "SignalTime",
    "SignalSessionDate", "EntryPolicy", "EntryTime", "EntryDate", "EntryPriceRaw",
    "EntryPrice", "Currency", "EURUSDEntry", "Quantity", "GrossBuyValueLocal",
    "GrossBuyValueEUR", "BuyFeeEUR", "TotalBuyCostEUR", "BreakoutLevel",
    "OriginalStopPrice", "OriginalTargetPrice", "StopPrice", "TargetPrice",
    "InitialRiskReward", "LastPrice", "MarketValueEUR", "UnrealizedPnLEUR",
    "HoldingSessions", "OvernightCount", "ExitTime", "ExitDate", "ExitPriceRaw",
    "ExitPrice", "ExitReason", "GrossSellValueLocal", "GrossSellValueEUR",
    "SellFeeEUR", "NetSellProceedsEUR", "RealizedPnLEUR", "ReturnPct",
    "MFEPct", "MAEPct", "SameBarAmbiguity", "ObservationOnly",
    "V1ExecutionChanged", "PortfolioV1Changed", "UpdatedAtUTC"
]

CASH_FIELDS = [
    "InitialCapitalEUR", "AvailableCashEUR", "InvestedCostEUR", "MarketValueEUR",
    "RealizedPnLEUR", "UnrealizedPnLEUR", "FeesPaidEUR", "TotalEquityEUR",
    "OpenPositions", "UpdatedAtUTC", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed"
]

RESULT_FIELDS = [
    "PositionID", "SignalID", "Ticker", "EntryTime", "ExitTime", "EntryPrice",
    "ExitPrice", "Quantity", "ExitReason", "RealizedPnLEUR", "ReturnPct",
    "MFEPct", "MAEPct", "HoldingSessions", "OvernightCount",
    "SameBarAmbiguity", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed"
]

EVENT_FIELDS = [
    "EventID", "EventTimeUTC", "EventType", "PositionID", "SignalID", "Ticker",
    "Price", "Quantity", "AmountEUR", "Reason", "NotificationPriority",
    "NotificationPending", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed"
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def round_money(value: float) -> float:
    return round(float(value), 4)


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path: Path, fields: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_unique(path: Path, fields: Sequence[str], rows: Sequence[Dict[str, Any]], key: str) -> int:
    existing = read_csv_rows(path)
    known = {clean(row.get(key)) for row in existing}
    fresh = [row for row in rows if clean(row.get(key)) not in known]
    write_csv_rows(path, fields, existing + fresh)
    return len(fresh)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def currency_of(ticker: str) -> str:
    return "EUR" if ticker.endswith((".PA", ".AS")) else "USD"


def euro_fee(gross_eur: float) -> float:
    return 1.0 if gross_eur <= 1000.0 else 2.55


def us_fee(gross_eur: float) -> float:
    return max(1.0, round(gross_eur * 0.001, 2))


def fee_for(currency: str, gross_eur: float) -> float:
    return euro_fee(gross_eur) if currency == "EUR" else us_fee(gross_eur)


def is_truthy(value: Any) -> bool:
    return clean(value).lower() in {"true", "1", "yes", "oui"}


def parse_timestamp(value: str) -> Optional[pd.Timestamp]:
    if not clean(value):
        return None
    try:
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            return stamp.tz_localize("Europe/Paris")
        return stamp
    except Exception:
        return None


def normalize_download(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.reset_index()
    data = data.rename(columns={data.columns[0]: "Datetime"})
    data["Datetime"] = pd.to_datetime(data["Datetime"], utc=True, errors="coerce")
    data = data.dropna(subset=["Datetime"]).copy()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["Open", "High", "Low", "Close"])


def download_5m(ticker: str) -> pd.DataFrame:
    return normalize_download(yf.download(
        ticker, period="60d", interval="5m", progress=False,
        auto_adjust=True, prepost=False, threads=False
    ))


def download_fx() -> pd.Series:
    data = yf.download(
        "EURUSD=X", period="60d", interval="1h", progress=False,
        auto_adjust=True, prepost=True, threads=False
    )
    if data.empty:
        return pd.Series(dtype=float)
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    series = data["Close"].squeeze().dropna()
    series.index = pd.to_datetime(series.index, utc=True, errors="coerce")
    return series.sort_index()


def fx_at(timestamp: pd.Timestamp, currency: str, fx: pd.Series) -> float:
    if currency == "EUR":
        return 1.0
    if fx.empty:
        raise RuntimeError("EURUSD indisponible pour un titre USD")
    ts = timestamp.tz_convert("UTC") if timestamp.tzinfo else timestamp.tz_localize("UTC")
    eligible = fx[fx.index <= ts]
    return float(eligible.iloc[-1] if not eligible.empty else fx.iloc[0])


def local_zone(ticker: str) -> str:
    return "Europe/Paris" if ticker.endswith((".PA", ".AS")) else "America/New_York"


def add_local_fields(data: pd.DataFrame, ticker: str) -> pd.DataFrame:
    if data.empty:
        return data
    result = data.copy()
    result["LocalDatetime"] = result["Datetime"].dt.tz_convert(local_zone(ticker))
    result["LocalDate"] = result["LocalDatetime"].dt.strftime("%Y-%m-%d")
    result["LocalTime"] = result["LocalDatetime"].dt.strftime("%H:%M:%S")
    return result


def load_cash() -> Dict[str, float]:
    rows = read_csv_rows(CASH_FILE)
    if not rows:
        return {
            "InitialCapitalEUR": INITIAL_CAPITAL_EUR,
            "AvailableCashEUR": INITIAL_CAPITAL_EUR,
            "InvestedCostEUR": 0.0,
            "MarketValueEUR": 0.0,
            "RealizedPnLEUR": 0.0,
            "UnrealizedPnLEUR": 0.0,
            "FeesPaidEUR": 0.0,
            "TotalEquityEUR": INITIAL_CAPITAL_EUR,
            "OpenPositions": 0,
        }
    row = rows[0]
    return {
        key: number(row.get(key)) or 0.0
        for key in (
            "InitialCapitalEUR", "AvailableCashEUR", "InvestedCostEUR",
            "MarketValueEUR", "RealizedPnLEUR", "UnrealizedPnLEUR",
            "FeesPaidEUR", "TotalEquityEUR", "OpenPositions"
        )
    }


def load_all_signals() -> List[Dict[str, str]]:
    return read_csv_rows(SIGNALS_FILE)


def load_current_states() -> Dict[str, Dict[str, str]]:
    return {clean(row.get("Ticker")): row for row in read_csv_rows(STATE_FILE) if clean(row.get("Ticker"))}


def signal_rejection_reason(signal: Dict[str, str], state: Optional[Dict[str, str]]) -> str:
    if clean(signal.get("AgentVersion")) != REQUIRED_AGENT_VERSION:
        return "SIGNAL_REJETE_VERSION_OBSOLETE"
    if clean(signal.get("SignalValidityPolicy")) != REQUIRED_SIGNAL_POLICY:
        return "SIGNAL_REJETE_POLITIQUE_OBSOLETE"
    if clean(signal.get("SignalType")) != "SIGNAL_STRUCTURE_SHADOW":
        return "SIGNAL_TYPE_INVALIDE"
    if not is_truthy(signal.get("SignalFrozenBeforeOutcome")):
        return "SIGNAL_NON_FIGE"
    if not is_truthy(signal.get("ObservationOnly")):
        return "SIGNAL_NON_SHADOW"
    if is_truthy(signal.get("V1ExecutionChanged")) or is_truthy(signal.get("PortfolioV1Changed")):
        return "GARDE_FOU_V1_INVALIDE"
    if state is None:
        return "ETAT_STRUCTURE_ABSENT"
    if clean(state.get("StructureState")) != "SIGNAL_STRUCTURE_SHADOW":
        return "SIGNAL_INVALIDE_PAR_ETAT_STRUCTURE_COURANT"
    if clean(state.get("SessionDate")) != clean(signal.get("SessionDate")):
        return "SIGNAL_INVALIDE_PAR_SESSION"
    if clean(state.get("MajorResistanceTooClose")).lower() == "true":
        return "SIGNAL_INVALIDE_RESISTANCE_MAJEURE_PROCHE"
    if clean(state.get("VolumeEligible")).lower() != "true" or clean(state.get("VolumeConfirmed")).lower() != "true":
        return "SIGNAL_INVALIDE_VOLUME_NON_CONFIRME"
    return ""


def first_executable_bar(data: pd.DataFrame, signal_time: pd.Timestamp, ticker: str) -> Tuple[Optional[int], str]:
    if data.empty:
        return None, "NO_DATA"
    local = signal_time.tz_convert(local_zone(ticker))
    signal_clock = local.strftime("%H:%M:%S")
    future = data[data["Datetime"] > signal_time.tz_convert("UTC")]
    if future.empty:
        return None, "WAITING_NEXT_BAR"

    if signal_clock >= "15:55:00" and currency_of(ticker) == "USD":
        next_session = future[future["LocalDate"] != local.strftime("%Y-%m-%d")]
        return (int(next_session.index[0]), "NEXT_SESSION_OPEN") if not next_session.empty else (None, "WAITING_NEXT_SESSION")

    if signal_clock >= "17:25:00" and currency_of(ticker) == "EUR":
        next_session = future[future["LocalDate"] != local.strftime("%Y-%m-%d")]
        return (int(next_session.index[0]), "NEXT_SESSION_OPEN") if not next_session.empty else (None, "WAITING_NEXT_SESSION")

    return int(future.index[0]), "NEXT_BAR_OPEN"



def signal_is_stale(data: pd.DataFrame, entry_index: Optional[int]) -> bool:
    if entry_index is None or data.empty:
        return False
    latest_index = int(data.index[-1])
    return latest_index - int(entry_index) > MAX_STALE_FUTURE_BARS


def rejection_event(signal: Dict[str, str], reason: str) -> Dict[str, Any]:
    signal_id = clean(signal.get("SignalID"))
    ticker = clean(signal.get("Ticker"))
    event = make_event(
        "SHADOW_SIGNAL_REJECTED", "", signal_id, ticker,
        number(signal.get("ReferencePrice")) or 0.0, 0, 0.0, reason
    )
    event["NotificationPriority"] = "INFO"
    return event

def recalibrated_levels(signal: Dict[str, str], entry_price: float) -> Tuple[float, float, float]:
    breakout = number(signal.get("BreakoutLevel"))
    original_stop = number(signal.get("StopPrice"))
    if original_stop is not None and original_stop < entry_price:
        stop = original_stop
    elif breakout is not None:
        stop = breakout * 0.995
    else:
        stop = entry_price * 0.99
    target = entry_price * 1.02
    risk = entry_price - stop
    rr = (target - entry_price) / risk if risk > 0 else 0.0
    return stop, target, rr


def buy_price(raw_open: float) -> float:
    return raw_open * (1.0 + SLIPPAGE_BPS / 10000.0)


def sell_price(raw_price: float) -> float:
    return raw_price * (1.0 - SLIPPAGE_BPS / 10000.0)


def local_to_eur(local_amount: float, currency: str, eurusd: float, sell: bool = False) -> float:
    if currency == "EUR":
        return local_amount
    adjusted_fx = eurusd * (1.0 - FX_SLIPPAGE_BPS / 10000.0 if sell else 1.0 + FX_SLIPPAGE_BPS / 10000.0)
    return local_amount / adjusted_fx


def make_event(
    event_type: str,
    position_id: str,
    signal_id: str,
    ticker: str,
    price: float,
    quantity: int,
    amount_eur: float,
    reason: str,
) -> Dict[str, Any]:
    timestamp = now_utc()
    return {
        "EventID": f"{event_type}|{position_id}|{timestamp}",
        "EventTimeUTC": timestamp,
        "EventType": event_type,
        "PositionID": position_id,
        "SignalID": signal_id,
        "Ticker": ticker,
        "Price": round_money(price),
        "Quantity": quantity,
        "AmountEUR": round_money(amount_eur),
        "Reason": reason,
        "NotificationPriority": "IMMEDIATE" if event_type in {"SHADOW_BUY_EXECUTED", "SHADOW_SELL_EXECUTED"} else "INFO",
        "NotificationPending": "True",
        "ObservationOnly": "True",
        "V1ExecutionChanged": "False",
        "PortfolioV1Changed": "False",
    }


def update_open_position(
    position: Dict[str, Any],
    data: pd.DataFrame,
    fx: pd.Series,
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    ticker = clean(position.get("Ticker"))
    entry_time = parse_timestamp(clean(position.get("EntryTime")))
    if entry_time is None or data.empty:
        return position, None, None

    future = data[data["Datetime"] > entry_time.tz_convert("UTC")]
    if future.empty:
        return position, None, None

    entry_price = number(position.get("EntryPrice")) or 0.0
    stop = number(position.get("StopPrice")) or 0.0
    target = number(position.get("TargetPrice")) or 0.0
    quantity = int(number(position.get("Quantity")) or 0)
    currency = clean(position.get("Currency"))

    maximum = entry_price
    minimum = entry_price
    exit_index: Optional[int] = None
    exit_raw: Optional[float] = None
    reason = ""
    ambiguity = False

    unique_dates: List[str] = []
    for index, row in future.iterrows():
        local_date = clean(row["LocalDate"])
        if local_date not in unique_dates:
            unique_dates.append(local_date)
        op, hi, lo = float(row["Open"]), float(row["High"]), float(row["Low"])
        maximum = max(maximum, hi)
        minimum = min(minimum, lo)

        if op <= stop:
            exit_index, exit_raw, reason = int(index), op, "GAP_SOUS_STOP"
            break
        if op >= target:
            exit_index, exit_raw, reason = int(index), op, "GAP_AU_DESSUS_TP"
            break
        stop_inside = lo <= stop
        target_inside = hi >= target
        if stop_inside and target_inside:
            exit_index, exit_raw, reason, ambiguity = int(index), stop, "STOP_AVANT_TP_MEME_BOUGIE", True
            break
        if stop_inside:
            exit_index, exit_raw, reason = int(index), stop, "STOP"
            break
        if target_inside:
            exit_index, exit_raw, reason = int(index), target, "TAKE_PROFIT_2PCT"
            break

        if len(unique_dates) >= MAX_HOLDING_SESSIONS and local_date == unique_dates[-1] and clean(row["LocalTime"]) >= ("15:55:00" if currency == "USD" else "17:25:00"):
            exit_index, exit_raw, reason = int(index), float(row["Close"]), "TIME_J4"
            break

    last_row = future.iloc[-1]
    last_price = float(last_row["Close"])
    last_fx = fx_at(last_row["Datetime"], currency, fx)
    market_eur = local_to_eur(last_price * quantity, currency, last_fx, sell=True)
    buy_cost = number(position.get("TotalBuyCostEUR")) or 0.0

    position["LastPrice"] = round_money(last_price)
    position["MarketValueEUR"] = round_money(market_eur)
    position["UnrealizedPnLEUR"] = round_money(market_eur - buy_cost)
    position["HoldingSessions"] = len(unique_dates) if unique_dates else 1
    position["OvernightCount"] = max(0, (len(unique_dates) if unique_dates else 1) - 1)
    position["MFEPct"] = round_money((maximum / entry_price - 1.0) * 100.0)
    position["MAEPct"] = round_money((minimum / entry_price - 1.0) * 100.0)
    position["UpdatedAtUTC"] = now_utc()

    if exit_index is None or exit_raw is None:
        return position, None, None

    exit_row = data.loc[exit_index]
    execution_price = sell_price(exit_raw)
    exit_fx = fx_at(exit_row["Datetime"], currency, fx)
    gross_local = execution_price * quantity
    gross_eur = local_to_eur(gross_local, currency, exit_fx, sell=True)
    fee = fee_for(currency, gross_eur)
    net = gross_eur - fee
    pnl = net - buy_cost
    return_pct = pnl / buy_cost * 100.0 if buy_cost else 0.0

    position.update({
        "Status": "CLOSED",
        "ExitTime": pd.Timestamp(exit_row["Datetime"]).isoformat(),
        "ExitDate": clean(exit_row["LocalDate"]),
        "ExitPriceRaw": round_money(exit_raw),
        "ExitPrice": round_money(execution_price),
        "ExitReason": reason,
        "GrossSellValueLocal": round_money(gross_local),
        "GrossSellValueEUR": round_money(gross_eur),
        "SellFeeEUR": round_money(fee),
        "NetSellProceedsEUR": round_money(net),
        "RealizedPnLEUR": round_money(pnl),
        "ReturnPct": round_money(return_pct),
        "MarketValueEUR": 0.0,
        "UnrealizedPnLEUR": 0.0,
        "SameBarAmbiguity": str(ambiguity),
        "UpdatedAtUTC": now_utc(),
    })

    result = {field: position.get(field, "") for field in RESULT_FIELDS}
    event = make_event(
        "SHADOW_SELL_EXECUTED", clean(position.get("PositionID")),
        clean(position.get("SignalID")), ticker, execution_price, quantity,
        net, reason,
    )
    return position, result, event


def main() -> int:
    print("=" * 104)
    print("STRUCTURE SHADOW PORTFOLIO - PORTEFEUILLE VIRTUEL SÉPARÉ")
    print("=" * 104)

    signals = load_all_signals()
    current_states = load_current_states()
    positions = read_csv_rows(PORTFOLIO_FILE)
    cash = load_cash()
    results_existing = read_csv_rows(RESULTS_FILE)
    executed_signal_ids = {clean(row.get("SignalID")) for row in positions}
    open_tickers = {clean(row.get("Ticker")) for row in positions if clean(row.get("Status")) == "OPEN"}
    events_existing = read_csv_rows(EVENTS_FILE)
    processed_rejected_signal_ids = {clean(row.get("SignalID")) for row in events_existing if clean(row.get("EventType")) == "SHADOW_SIGNAL_REJECTED"}
    events: List[Dict[str, Any]] = []
    new_results: List[Dict[str, Any]] = []
    data_cache: Dict[str, pd.DataFrame] = {}
    fx = download_fx()

    def bars(ticker: str) -> pd.DataFrame:
        if ticker not in data_cache:
            data_cache[ticker] = add_local_fields(download_5m(ticker), ticker)
        return data_cache[ticker]

    # 1. Gérer les positions ouvertes avant d'en ouvrir de nouvelles.
    updated_positions: List[Dict[str, Any]] = []
    for position in positions:
        if clean(position.get("Status")) != "OPEN":
            updated_positions.append(position)
            continue
        ticker = clean(position.get("Ticker"))
        updated, result, event = update_open_position(position, bars(ticker), fx)
        updated_positions.append(updated)
        if result:
            new_results.append(result)
            cash["AvailableCashEUR"] += number(updated.get("NetSellProceedsEUR")) or 0.0
            cash["RealizedPnLEUR"] += number(updated.get("RealizedPnLEUR")) or 0.0
            cash["FeesPaidEUR"] += number(updated.get("SellFeeEUR")) or 0.0
            open_tickers.discard(ticker)
        if event:
            events.append(event)
    positions = updated_positions

    # 2. Ouvrir uniquement les nouveaux signaux figés, sans jamais toucher V1.
    for signal in sorted(signals, key=lambda row: clean(row.get("SignalTime"))):
        signal_id = clean(signal.get("SignalID"))
        ticker = clean(signal.get("Ticker"))
        if not signal_id or signal_id in executed_signal_ids or ticker in open_tickers:
            continue
        rejection = signal_rejection_reason(signal, current_states.get(ticker))
        if rejection:
            if signal_id not in processed_rejected_signal_ids:
                events.append(rejection_event(signal, rejection))
                processed_rejected_signal_ids.add(signal_id)
            print(f"SIGNAL REJETÉ {ticker} | {rejection}")
            continue
        open_count = sum(clean(row.get("Status")) == "OPEN" for row in positions)
        if open_count >= MAX_OPEN_POSITIONS:
            break
        if cash["AvailableCashEUR"] < 50.0:
            break

        signal_time = parse_timestamp(clean(signal.get("SignalTime")))
        if signal_time is None:
            continue
        data = bars(ticker)
        entry_index, policy = first_executable_bar(data, signal_time, ticker)
        if entry_index is None:
            print(f"EN ATTENTE {ticker} | {policy}")
            continue
        if signal_is_stale(data, entry_index):
            reason = "SIGNAL_EXPIRE_APRES_PREMIERE_BOUGIE_EXECUTABLE"
            if signal_id not in processed_rejected_signal_ids:
                events.append(rejection_event(signal, reason))
                processed_rejected_signal_ids.add(signal_id)
            print(f"SIGNAL EXPIRÉ {ticker} | {reason}")
            continue

        row = data.loc[entry_index]
        raw_open = float(row["Open"])
        entry = buy_price(raw_open)
        currency = currency_of(ticker)
        entry_fx = fx_at(row["Datetime"], currency, fx)
        stop, target, rr = recalibrated_levels(signal, entry)
        if rr < MIN_RR:
            print(f"SIGNAL IGNORÉ {ticker} | RR réel {rr:.2f} < {MIN_RR:.2f}")
            executed_signal_ids.add(signal_id)
            continue

        allocation = min(FIXED_ALLOCATION_EUR, cash["AvailableCashEUR"])
        local_budget = allocation if currency == "EUR" else allocation * entry_fx
        quantity = int(local_budget // entry)
        if quantity < 1:
            print(f"SIGNAL NON EXÉCUTABLE {ticker} | quantité entière nulle")
            continue

        gross_local = entry * quantity
        gross_eur = local_to_eur(gross_local, currency, entry_fx, sell=False)
        fee = fee_for(currency, gross_eur)
        total_cost = gross_eur + fee
        while quantity > 0 and total_cost > cash["AvailableCashEUR"]:
            quantity -= 1
            gross_local = entry * quantity
            gross_eur = local_to_eur(gross_local, currency, entry_fx, sell=False)
            fee = fee_for(currency, gross_eur)
            total_cost = gross_eur + fee
        if quantity < 1:
            continue

        position_id = f"STRUCTURE|{signal_id}"
        position = {field: "" for field in PORTFOLIO_FIELDS}
        position.update({
            "PositionID": position_id,
            "SignalID": signal_id,
            "Ticker": ticker,
            "Market": clean(signal.get("Market")),
            "Status": "OPEN",
            "SignalTime": clean(signal.get("SignalTime")),
            "SignalSessionDate": clean(signal.get("SessionDate")),
            "EntryPolicy": policy,
            "EntryTime": pd.Timestamp(row["Datetime"]).isoformat(),
            "EntryDate": clean(row["LocalDate"]),
            "EntryPriceRaw": round_money(raw_open),
            "EntryPrice": round_money(entry),
            "Currency": currency,
            "EURUSDEntry": round_money(entry_fx),
            "Quantity": quantity,
            "GrossBuyValueLocal": round_money(gross_local),
            "GrossBuyValueEUR": round_money(gross_eur),
            "BuyFeeEUR": round_money(fee),
            "TotalBuyCostEUR": round_money(total_cost),
            "BreakoutLevel": clean(signal.get("BreakoutLevel")),
            "OriginalStopPrice": clean(signal.get("StopPrice")),
            "OriginalTargetPrice": clean(signal.get("TargetPrice")),
            "StopPrice": round_money(stop),
            "TargetPrice": round_money(target),
            "InitialRiskReward": round_money(rr),
            "LastPrice": round_money(entry),
            "MarketValueEUR": round_money(gross_eur),
            "UnrealizedPnLEUR": round_money(-fee),
            "HoldingSessions": 1,
            "OvernightCount": 0,
            "SameBarAmbiguity": "False",
            "ObservationOnly": "True",
            "V1ExecutionChanged": "False",
            "PortfolioV1Changed": "False",
            "UpdatedAtUTC": now_utc(),
        })
        positions.append(position)
        executed_signal_ids.add(signal_id)
        open_tickers.add(ticker)
        cash["AvailableCashEUR"] -= total_cost
        cash["FeesPaidEUR"] += fee
        events.append(make_event(
            "SHADOW_BUY_EXECUTED", position_id, signal_id, ticker,
            entry, quantity, total_cost,
            f"{policy}; TP={target:.4f}; SL={stop:.4f}; RR={rr:.2f}",
        ))
        print(f"ACHAT VIRTUEL {ticker} | {quantity} @ {entry:.4f} | TP {target:.4f} | SL {stop:.4f} | RR {rr:.2f}")

    # 3. Recalculer l'état de trésorerie à partir du portefeuille Shadow seulement.
    open_positions = [row for row in positions if clean(row.get("Status")) == "OPEN"]
    invested = sum(number(row.get("TotalBuyCostEUR")) or 0.0 for row in open_positions)
    market_value = sum(number(row.get("MarketValueEUR")) or 0.0 for row in open_positions)
    unrealized = sum(number(row.get("UnrealizedPnLEUR")) or 0.0 for row in open_positions)
    cash["InvestedCostEUR"] = invested
    cash["MarketValueEUR"] = market_value
    cash["UnrealizedPnLEUR"] = unrealized
    cash["TotalEquityEUR"] = cash["AvailableCashEUR"] + market_value
    cash["OpenPositions"] = len(open_positions)

    write_csv_rows(PORTFOLIO_FILE, PORTFOLIO_FIELDS, positions)
    write_csv_rows(RESULTS_FILE, RESULT_FIELDS, results_existing + new_results)
    append_unique(EVENTS_FILE, EVENT_FIELDS, events, "EventID")

    cash_row = {
        **{key: round_money(value) for key, value in cash.items()},
        "UpdatedAtUTC": now_utc(),
        "ObservationOnly": "True",
        "V1ExecutionChanged": "False",
        "PortfolioV1Changed": "False",
    }
    write_csv_rows(CASH_FILE, CASH_FIELDS, [cash_row])

    summary = {
        "GeneratedAtUTC": now_utc(),
        "SignalsAvailable": len(signals),
        "PositionsTotal": len(positions),
        "OpenPositions": len(open_positions),
        "ClosedPositions": sum(clean(row.get("Status")) == "CLOSED" for row in positions),
        "NewEvents": len(events),
        "AvailableCashEUR": round_money(cash["AvailableCashEUR"]),
        "MarketValueEUR": round_money(market_value),
        "TotalEquityEUR": round_money(cash["TotalEquityEUR"]),
        "RealizedPnLEUR": round_money(cash["RealizedPnLEUR"]),
        "UnrealizedPnLEUR": round_money(unrealized),
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 104)
    print(f"Signaux Structure : {len(signals)}")
    print(f"Positions ouvertes : {len(open_positions)} / {MAX_OPEN_POSITIONS}")
    print(f"Cash Shadow : {cash['AvailableCashEUR']:.2f} EUR")
    print(f"Equity Shadow : {cash['TotalEquityEUR']:.2f} EUR")
    print(f"Événements nouveaux : {len(events)}")
    print(f"Portefeuille : {PORTFOLIO_FILE}")
    print(f"Cash : {CASH_FILE}")
    print(f"Résultats : {RESULTS_FILE}")
    print(f"Événements alertables : {EVENTS_FILE}")
    print("ObservationOnly : True")
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
