#!/usr/bin/env python3
"""Gestion du portefeuille virtuel V1 figé.

Règles reproduites depuis replay_intraday_monitor.py :
- capital initial : 3 000 EUR ;
- allocation FIXED_1000 ;
- maximum 3 positions ;
- TP hybride selon ATR14Pct J-1 ;
- SL = 1 x ATR, borné entre 0,75 % et 3,00 % ;
- horizon maximal J à J+4, en séances boursières ;
- week-end non compté comme séance ;
- gaps gérés au prix d'ouverture ;
- si TP et SL sont touchés dans la même bougie : sortie au SL ;
- frais de 0,05 % par côté et slippage de 0,05 % par côté.

Le script lit intraday_decisions.csv et écrit uniquement :
- virtual_portfolio.csv
- portfolio_cash.csv
"""
from __future__ import annotations

import csv
import math
import os
import unicodedata
from datetime import datetime, time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CASH_FILE = ROOT / "portfolio_cash.csv"
PORTFOLIO_FILE = ROOT / "virtual_portfolio.csv"
DECISION_FILE = ROOT / "intraday_decisions.csv"
SIGNALS_FILE = ROOT / "signals_history.csv"

PARIS = ZoneInfo("Europe/Paris")
NEW_YORK = ZoneInfo("America/New_York")
NOW_PARIS = datetime.now(PARIS)
NOW_UTC = pd.Timestamp.now(tz="UTC")

INITIAL_CAPITAL_EUR = 3000.0
FIXED_ALLOCATION_EUR = 1000.0
MAX_OPEN_POSITIONS = 3
MAX_SHARE_PRICE_EUR = 2000.0
MAX_HOLDING_SESSIONS = 5  # séance d'entrée incluse : J à J+4
HORIZON = 4
FEE_SIDE_PCT = 0.05
SLIPPAGE_SIDE_PCT = 0.05
MAX_DECISION_AGE_MINUTES = float(os.getenv("MAX_DECISION_AGE_MINUTES", "60"))

PORTFOLIO_FIELDS = [
    "position_id", "ticker", "signal_date", "session_date", "decision",
    "decision_time", "entry_time", "entry_date", "atr14_pct_j1", "tp_pct",
    "sl_pct", "raw_entry_price", "entry_price", "currency", "eurusd_entry",
    "quantity", "gross_buy_value_local", "gross_buy_value_eur", "buy_fee_eur",
    "total_buy_cost_eur", "target_price", "stop_price", "status",
    "last_price", "market_value_eur", "unrealized_pnl_eur", "holding_session",
    "holding_sessions", "exit_time", "exit_date", "raw_exit_price", "exit_price",
    "eurusd_exit", "gross_sell_value_local", "gross_sell_value_eur",
    "sell_fee_eur", "net_sell_proceeds_eur", "realized_pnl_eur",
    "return_pct", "exit_reason", "ambiguous_bar", "gap_exit",
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(clean(value).replace(",", "."))
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean(value).upper())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())


def is_buy_decision(value: Any) -> bool:
    return normalize_label(value) in {
        "ACHAT CONFIRME",
        "ACHAT REACTIVE",
        "ACHAT REACTIF",
    }


def parse_time(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("Europe/Paris")
    return timestamp.tz_convert("Europe/Paris")


def hybrid_tp(atr_pct: float) -> float:
    if atr_pct < 1.5:
        return 1.5
    if atr_pct < 2.5:
        return 2.0
    return min(3.0, max(2.0, atr_pct * 0.75))


def atr_sl(atr_pct: float) -> float:
    return min(3.0, max(0.75, atr_pct))


def percentage_fee(gross_eur: float) -> float:
    return gross_eur * FEE_SIDE_PCT / 100.0


def load_cash() -> Dict[str, str]:
    rows = read_csv(CASH_FILE)
    if rows:
        return rows[0]
    return {
        "initial_capital_eur": str(INITIAL_CAPITAL_EUR),
        "available_cash_eur": str(INITIAL_CAPITAL_EUR),
        "invested_cost_eur": "0",
        "market_value_eur": "0",
        "realized_pnl_eur": "0",
        "unrealized_pnl_eur": "0",
        "fees_paid_eur": "0",
        "total_equity_eur": str(INITIAL_CAPITAL_EUR),
    }


def download_prices(ticker: str, **kwargs: Any) -> pd.DataFrame:
    data = yf.download(ticker, progress=False, auto_adjust=True, **kwargs)
    if not data.empty and isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    return data


def load_fx() -> pd.Series:
    data = download_prices("EURUSD=X", period="3mo")
    if data.empty or "Close" not in data:
        raise RuntimeError("Impossible de télécharger EURUSD=X.")
    return data["Close"].squeeze().dropna()


def fx_at(fx: pd.Series, date: str) -> float:
    eligible = fx[fx.index.strftime("%Y-%m-%d") <= date]
    if len(eligible):
        return float(eligible.iloc[-1])
    return float(fx.iloc[-1])


def currency_for(ticker: str) -> str:
    return "EUR" if ticker.endswith((".PA", ".AS")) else "USD"


def local_to_eur(value_local: float, currency: str, rate: float) -> float:
    return value_local if currency == "EUR" else value_local / rate


def effective_buy_price(raw_price: float) -> float:
    return raw_price * (1.0 + SLIPPAGE_SIDE_PCT / 100.0)


def effective_sell_price(raw_price: float) -> float:
    return raw_price * (1.0 - SLIPPAGE_SIDE_PCT / 100.0)


def session_dates(data: pd.DataFrame) -> List[str]:
    return sorted({index.strftime("%Y-%m-%d") for index in data.index})


def market_closed_for_session(ticker: str, session_date: str) -> bool:
    """Retourne vrai si la séance J+4 peut être considérée terminée."""
    today_paris = NOW_PARIS.date().isoformat()
    if session_date < today_paris:
        return True
    if session_date > today_paris:
        return False
    if ticker.endswith((".PA", ".AS")):
        return NOW_PARIS.time() >= time(17, 35)
    now_ny = datetime.now(NEW_YORK)
    return now_ny.date().isoformat() > session_date or (
        now_ny.date().isoformat() == session_date and now_ny.time() >= time(16, 0)
    )


def holding_info(data: pd.DataFrame, entry_date: str) -> Tuple[str, int, Optional[str]]:
    dates = session_dates(data)
    if entry_date not in dates:
        return "J", 1, None
    start = dates.index(entry_date)
    current = max(start, len(dates) - 1)
    offset = max(0, current - start)
    label = "J" if offset == 0 else f"J+{offset}"
    end_date = dates[start + HORIZON] if start + HORIZON < len(dates) else None
    return label, min(offset + 1, MAX_HOLDING_SESSIONS), end_date


def find_exit(
    ticker: str,
    position: Dict[str, Any],
    data: pd.DataFrame,
) -> Optional[Dict[str, Any]]:
    decision_time = parse_time(position.get("entry_time") or position["decision_time"])
    entry_date = clean(position.get("entry_date") or position.get("session_date"))
    target = float(position["target_price"])
    stop = float(position["stop_price"])

    dates = session_dates(data)
    if entry_date not in dates:
        print("SESSION ENTREE INTROUVABLE", ticker, "|", entry_date)
        return None
    start = dates.index(entry_date)
    end_date = dates[start + HORIZON] if start + HORIZON < len(dates) else None

    bars = data.loc[data.index > decision_time].copy()
    if end_date is not None:
        bars = bars.loc[bars.index.strftime("%Y-%m-%d") <= end_date]
    if bars.empty:
        return None

    for date, session in bars.groupby(bars.index.strftime("%Y-%m-%d"), sort=True):
        session = session.sort_index()
        first = session.iloc[0]
        opening = float(first["Open"])

        if opening <= stop:
            return {
                "raw_exit_price": opening,
                "exit_time": session.index[0],
                "exit_reason": "GAP SOUS STOP",
                "gap_exit": True,
                "ambiguous_bar": False,
            }
        if opening >= target:
            return {
                "raw_exit_price": opening,
                "exit_time": session.index[0],
                "exit_reason": "GAP AU-DESSUS TARGET",
                "gap_exit": True,
                "ambiguous_bar": False,
            }

        for index, row in session.iterrows():
            target_hit = float(row["High"]) >= target
            stop_hit = float(row["Low"]) <= stop
            if target_hit and stop_hit:
                return {
                    "raw_exit_price": stop,
                    "exit_time": index,
                    "exit_reason": "TP ET SL MEME BOUGIE - SL",
                    "gap_exit": False,
                    "ambiguous_bar": True,
                }
            if stop_hit:
                return {
                    "raw_exit_price": stop,
                    "exit_time": index,
                    "exit_reason": "STOP",
                    "gap_exit": False,
                    "ambiguous_bar": False,
                }
            if target_hit:
                return {
                    "raw_exit_price": target,
                    "exit_time": index,
                    "exit_reason": "TARGET",
                    "gap_exit": False,
                    "ambiguous_bar": False,
                }

    if end_date is not None and market_closed_for_session(ticker, end_date):
        end_bars = bars.loc[bars.index.strftime("%Y-%m-%d") == end_date]
        if not end_bars.empty:
            last_index = end_bars.index[-1]
            return {
                "raw_exit_price": float(end_bars["Close"].iloc[-1]),
                "exit_time": last_index,
                "exit_reason": f"SORTIE J+{HORIZON}",
                "gap_exit": False,
                "ambiguous_bar": False,
            }
    return None


def close_position(
    position: Dict[str, Any],
    exit_data: Dict[str, Any],
    available: float,
    fx: pd.Series,
) -> float:
    raw_exit = float(exit_data["raw_exit_price"])
    effective_exit = effective_sell_price(raw_exit)
    exit_timestamp = pd.Timestamp(exit_data["exit_time"])
    exit_date = exit_timestamp.strftime("%Y-%m-%d")
    currency = clean(position.get("currency")) or currency_for(clean(position["ticker"]))
    rate = 1.0 if currency == "EUR" else fx_at(fx, exit_date)
    quantity = int(float(position["quantity"]))

    gross_local = quantity * effective_exit
    gross_eur = local_to_eur(gross_local, currency, rate)
    sell_fee = percentage_fee(gross_eur)
    net = gross_eur - sell_fee
    total_buy_cost = float(position["total_buy_cost_eur"])
    realized = net - total_buy_cost
    return_pct = realized / total_buy_cost * 100.0 if total_buy_cost else 0.0
    available += net

    position.update(
        {
            "status": "CLOSED",
            "last_price": round(raw_exit, 4),
            "market_value_eur": 0,
            "unrealized_pnl_eur": 0,
            "exit_time": exit_timestamp.isoformat(),
            "exit_date": exit_date,
            "raw_exit_price": round(raw_exit, 4),
            "exit_price": round(effective_exit, 4),
            "eurusd_exit": round(rate, 6),
            "gross_sell_value_local": round(gross_local, 2),
            "gross_sell_value_eur": round(gross_eur, 2),
            "sell_fee_eur": round(sell_fee, 2),
            "net_sell_proceeds_eur": round(net, 2),
            "realized_pnl_eur": round(realized, 2),
            "return_pct": round(return_pct, 4),
            "exit_reason": exit_data["exit_reason"],
            "ambiguous_bar": bool(exit_data["ambiguous_bar"]),
            "gap_exit": bool(exit_data["gap_exit"]),
        }
    )
    print(
        "VENTE",
        position["ticker"],
        "|",
        exit_data["exit_reason"],
        "| PnL",
        round(realized, 2),
        "EUR",
    )
    return available


def update_open_positions(
    positions: List[Dict[str, Any]], available: float, fx: pd.Series
) -> float:
    for position in positions:
        if clean(position.get("status")).upper() != "OPEN":
            continue

        ticker = clean(position.get("ticker"))
        data = download_prices(ticker, period="1mo", interval="5m", prepost=False)
        if data.empty:
            print("PAS DE DONNEES", ticker)
            continue

        exit_data = find_exit(ticker, position, data)
        if exit_data is not None:
            available = close_position(position, exit_data, available, fx)
            continue

        last = float(data["Close"].dropna().iloc[-1])
        date = data.index[-1].strftime("%Y-%m-%d")
        currency = clean(position.get("currency")) or currency_for(ticker)
        rate = 1.0 if currency == "EUR" else fx_at(fx, date)
        effective_last = effective_sell_price(last)
        gross_eur = local_to_eur(float(position["quantity"]) * effective_last, currency, rate)
        estimated_fee = percentage_fee(gross_eur)
        unrealized = gross_eur - estimated_fee - float(position["total_buy_cost_eur"])
        label, count, _ = holding_info(
            data, clean(position.get("entry_date") or position.get("session_date"))
        )

        position["last_price"] = round(last, 4)
        position["market_value_eur"] = round(gross_eur, 2)
        position["unrealized_pnl_eur"] = round(unrealized, 2)
        position["holding_session"] = label
        position["holding_sessions"] = count
        print(
            "SUIVI",
            ticker,
            "|",
            label,
            "| cours",
            round(last, 2),
            currency,
            "| PnL net",
            round(unrealized, 2),
            "EUR",
        )
    return available


def score_map_from_signals() -> Dict[Tuple[str, str], int]:
    result: Dict[Tuple[str, str], int] = {}
    for row in read_csv(SIGNALS_FILE):
        try:
            result[(clean(row.get("ticker")), clean(row.get("date")))] = int(
                float(row.get("score") or 0)
            )
        except (TypeError, ValueError):
            result[(clean(row.get("ticker")), clean(row.get("date")))] = 0
    return result


def build_candidates(
    decisions: List[Dict[str, str]], positions: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    open_tickers = {
        clean(position.get("ticker"))
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    }
    used = {
        (clean(position.get("ticker")), clean(position.get("signal_date")))
        for position in positions
    }
    candidates = []

    for decision in decisions:
        ticker = clean(decision.get("ticker"))
        signal_date = clean(decision.get("signal_date"))
        if normalize_label(decision.get("next_state")) != "POSITION A OUVRIR":
            continue
        if not is_buy_decision(decision.get("decision")):
            print("DECISION NON RECONNUE", ticker, "|", clean(decision.get("decision")))
            continue
        if ticker in open_tickers:
            print("DEJA OUVERT", ticker)
            continue
        if (ticker, signal_date) in used:
            print("DEJA TRAITE", ticker, "| signal", signal_date)
            continue
        candidates.append(decision)

    scores = score_map_from_signals()
    candidates.sort(
        key=lambda row: (
            scores.get((clean(row.get("ticker")), clean(row.get("signal_date"))), 0),
            float(row.get("decision_volume") or 0),
        ),
        reverse=True,
    )
    return candidates


def open_new_positions(
    positions: List[Dict[str, Any]],
    decisions: List[Dict[str, str]],
    available: float,
    fx: pd.Series,
) -> float:
    candidates = build_candidates(decisions, positions)
    open_tickers = {
        clean(position.get("ticker"))
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    }
    open_count = len(open_tickers)

    print("CANDIDATS EXECUTABLES :", len(candidates))
    print("FENETRE DECISION :", MAX_DECISION_AGE_MINUTES, "minutes")
    print("MAX POSITIONS :", MAX_OPEN_POSITIONS)

    for decision in candidates:
        ticker = clean(decision.get("ticker"))
        try:
            decision_ts = parse_time(decision["decision_time"])
        except Exception as exc:
            print("HORODATAGE INVALIDE", ticker, "|", exc)
            continue

        age_minutes = (NOW_UTC - decision_ts.tz_convert("UTC")).total_seconds() / 60
        decision_paris = decision_ts.tz_convert("Europe/Paris")
        if decision_paris.date() != NOW_PARIS.date():
            print("DECISION HORS SEANCE", ticker, "|", decision_paris.date(), "| aucun achat")
            continue
        if age_minutes < 0:
            print("DECISION FUTURE", ticker, "| age", round(age_minutes, 1), "min")
            continue
        if age_minutes > MAX_DECISION_AGE_MINUTES:
            print("DECISION PERIMEE", ticker, "| age", round(age_minutes, 1), "min")
            continue
        if open_count >= MAX_OPEN_POSITIONS:
            print("REFUSE", ticker, "| maximum", MAX_OPEN_POSITIONS, "positions")
            continue

        atr_pct = as_float(decision.get("atr14_pct_j1"))
        if atr_pct is None or atr_pct <= 0:
            print("ATR J-1 MANQUANT", ticker, "| aucun achat")
            continue

        raw_entry = as_float(decision.get("decision_close"))
        if raw_entry is None or raw_entry <= 0:
            print("PRIX ENTREE INVALIDE", ticker, "| aucun achat")
            continue

        currency = currency_for(ticker)
        session_date = clean(decision.get("session_date"))
        rate = 1.0 if currency == "EUR" else fx_at(fx, session_date)
        entry_price = effective_buy_price(raw_entry)
        unit_eur = local_to_eur(entry_price, currency, rate)
        if unit_eur > MAX_SHARE_PRICE_EUR:
            print("PRIX SUPERIEUR A 2000 EUR", ticker, "|", round(unit_eur, 2))
            continue

        budget = min(FIXED_ALLOCATION_EUR, available)
        quantity = math.floor(budget / unit_eur)
        if quantity == 0 and unit_eur <= available:
            quantity = 1

        gross_local = quantity * entry_price
        gross_eur = local_to_eur(gross_local, currency, rate)
        buy_fee = percentage_fee(gross_eur) if quantity else 0.0
        total_cost = gross_eur + buy_fee
        while quantity > 0 and total_cost > available:
            quantity -= 1
            gross_local = quantity * entry_price
            gross_eur = local_to_eur(gross_local, currency, rate)
            buy_fee = percentage_fee(gross_eur) if quantity else 0.0
            total_cost = gross_eur + buy_fee

        if quantity < 1:
            print(
                "NON EXECUTABLE",
                ticker,
                "| cash",
                round(available, 2),
                "EUR | prix unitaire",
                round(unit_eur, 2),
                "EUR",
            )
            continue

        tp_pct = hybrid_tp(atr_pct)
        sl_pct = atr_sl(atr_pct)
        target = raw_entry * (1.0 + tp_pct / 100.0)
        stop = raw_entry * (1.0 - sl_pct / 100.0)
        position_id = f"{ticker}|{session_date}|{decision_ts.isoformat()}"

        available -= total_cost
        positions.append(
            {
                "position_id": position_id,
                "ticker": ticker,
                "signal_date": clean(decision.get("signal_date")),
                "session_date": session_date,
                "decision": clean(decision.get("decision")),
                "decision_time": clean(decision.get("decision_time")),
                "entry_time": decision_ts.isoformat(),
                "entry_date": session_date,
                "atr14_pct_j1": round(atr_pct, 6),
                "tp_pct": round(tp_pct, 6),
                "sl_pct": round(sl_pct, 6),
                "raw_entry_price": round(raw_entry, 4),
                "entry_price": round(entry_price, 4),
                "currency": currency,
                "eurusd_entry": round(rate, 6),
                "quantity": quantity,
                "gross_buy_value_local": round(gross_local, 2),
                "gross_buy_value_eur": round(gross_eur, 2),
                "buy_fee_eur": round(buy_fee, 2),
                "total_buy_cost_eur": round(total_cost, 2),
                "target_price": round(target, 4),
                "stop_price": round(stop, 4),
                "status": "OPEN",
                "last_price": round(raw_entry, 4),
                "market_value_eur": round(gross_eur, 2),
                "unrealized_pnl_eur": round(-buy_fee, 2),
                "holding_session": "J",
                "holding_sessions": 1,
                "exit_time": "",
                "exit_date": "",
                "raw_exit_price": "",
                "exit_price": "",
                "eurusd_exit": "",
                "gross_sell_value_local": "",
                "gross_sell_value_eur": "",
                "sell_fee_eur": "",
                "net_sell_proceeds_eur": "",
                "realized_pnl_eur": "",
                "return_pct": "",
                "exit_reason": "",
                "ambiguous_bar": False,
                "gap_exit": False,
            }
        )
        open_count += 1
        open_tickers.add(ticker)
        print(
            "ACHAT",
            ticker,
            "|",
            quantity,
            "action(s) | ATR",
            round(atr_pct, 3),
            "% | TP",
            round(tp_pct, 3),
            "% | SL",
            round(sl_pct, 3),
            "% | cout",
            round(total_cost, 2),
            "EUR | cash",
            round(available, 2),
            "EUR",
        )
    return available


def portfolio_totals(
    positions: List[Dict[str, Any]], available: float
) -> Tuple[Dict[str, float], float]:
    open_positions = [
        p for p in positions if clean(p.get("status")).upper() == "OPEN"
    ]
    market_value = sum(as_float(p.get("market_value_eur"), 0.0) or 0.0 for p in open_positions)
    invested = sum(as_float(p.get("total_buy_cost_eur"), 0.0) or 0.0 for p in open_positions)
    realized = sum(
        as_float(p.get("realized_pnl_eur"), 0.0) or 0.0
        for p in positions
        if clean(p.get("status")).upper() == "CLOSED"
    )
    unrealized = sum(as_float(p.get("unrealized_pnl_eur"), 0.0) or 0.0 for p in open_positions)
    fees = sum(
        (as_float(p.get("buy_fee_eur"), 0.0) or 0.0)
        + (as_float(p.get("sell_fee_eur"), 0.0) or 0.0)
        for p in positions
    )
    equity = available + market_value
    return {
        "initial_capital_eur": INITIAL_CAPITAL_EUR,
        "available_cash_eur": round(available, 2),
        "invested_cost_eur": round(invested, 2),
        "market_value_eur": round(market_value, 2),
        "realized_pnl_eur": round(realized, 2),
        "unrealized_pnl_eur": round(unrealized, 2),
        "fees_paid_eur": round(fees, 2),
        "total_equity_eur": round(equity, 2),
    }, equity


def main() -> int:
    cash = load_cash()
    available = as_float(cash.get("available_cash_eur"), INITIAL_CAPITAL_EUR)
    if available is None:
        available = INITIAL_CAPITAL_EUR
    positions: List[Dict[str, Any]] = read_csv(PORTFOLIO_FILE)
    decisions = read_csv(DECISION_FILE)

    print("DECISIONS LUES :", len(decisions))
    print("POSITIONS LUES :", len(positions))

    fx = load_fx()
    available = update_open_positions(positions, available, fx)
    available = open_new_positions(positions, decisions, available, fx)

    cash_out, equity = portfolio_totals(positions, available)
    write_csv(PORTFOLIO_FILE, positions, PORTFOLIO_FIELDS)
    write_csv(CASH_FILE, [cash_out], list(cash_out))

    print(
        "CAPITAL | cash",
        round(available, 2),
        "EUR | positions",
        cash_out["market_value_eur"],
        "EUR | equity nette",
        round(equity, 2),
        "EUR",
    )
    print("V1ExecutionChanged : False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
