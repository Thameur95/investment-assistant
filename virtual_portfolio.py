#!/usr/bin/env python3
"""Gestion du portefeuille virtuel V1.

Lecture des décisions intraday, ouverture/suivi/clôture des positions virtuelles,
et mise à jour de virtual_portfolio.csv et portfolio_cash.csv.

Correctifs ciblés :
- normalisation robuste des libellés ACHAT CONFIRMÉ / ACHAT RÉACTIF ;
- fenêtre d'exécution configurable, fixée par défaut à 60 minutes ;
- diagnostics explicites pour chaque décision non retenue ;
- conservation stricte des noms de fichiers existants.
"""
from __future__ import annotations

import csv
import math
import os
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
CASH_FILE = ROOT / "portfolio_cash.csv"
PORTFOLIO_FILE = ROOT / "virtual_portfolio.csv"
DECISION_FILE = ROOT / "intraday_decisions.csv"
SIGNALS_FILE = ROOT / "signals_history.csv"

PARIS = ZoneInfo("Europe/Paris")
NOW_PARIS = datetime.now(PARIS)
NOW_UTC = pd.Timestamp.now(tz="UTC")

INITIAL_CAPITAL_EUR = 3000.0
FIXED_ALLOCATION_EUR = 1000.0
MAX_DECISION_AGE_MINUTES = float(os.getenv("MAX_DECISION_AGE_MINUTES", "60"))

PORTFOLIO_FIELDS = [
    "ticker", "signal_date", "decision", "decision_time", "entry_date",
    "entry_price", "currency", "eurusd_entry", "quantity",
    "gross_buy_value_local", "gross_buy_value_eur", "buy_fee_eur",
    "total_buy_cost_eur", "target_price", "stop_price", "status",
    "last_price", "market_value_eur", "unrealized_pnl_eur", "exit_date",
    "exit_price", "gross_sell_value_local", "gross_sell_value_eur",
    "sell_fee_eur", "net_sell_proceeds_eur", "realized_pnl_eur",
    "exit_reason",
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


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
    text = clean(value).upper()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.split())


def is_buy_decision(value: Any) -> bool:
    return normalize_label(value) in {"ACHAT CONFIRME", "ACHAT REACTIVE"}


def parse_time(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("Europe/Paris")
    return timestamp.tz_convert("Europe/Paris")


def euro_fee(gross_eur: float) -> float:
    return 1.0 if gross_eur <= 1000 else 2.55


def max_positions(total_equity_eur: float) -> int:
    if total_equity_eur < 4000:
        return 3
    if total_equity_eur < 5000:
        return 4
    return 5


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
    if not data.empty and hasattr(data.columns, "levels"):
        data.columns = data.columns.get_level_values(0)
    return data


def load_fx() -> pd.Series:
    data = download_prices("EURUSD=X", period="1mo")
    if data.empty or "Close" not in data:
        raise RuntimeError("Impossible de télécharger EURUSD=X.")
    return data["Close"].squeeze().dropna()


def fx_at(fx: pd.Series, date: str) -> float:
    eligible = fx[fx.index.strftime("%Y-%m-%d") <= date]
    return float(eligible.iloc[-1]) if len(eligible) else float(fx.iloc[-1])


def update_open_positions(
    positions: List[Dict[str, Any]], available: float, fx: pd.Series
) -> float:
    for position in positions:
        if clean(position.get("status")).upper() != "OPEN":
            continue

        ticker = clean(position.get("ticker"))
        data = download_prices(
            ticker, period="1mo", interval="5m", prepost=False
        )
        if data.empty:
            print("PAS DE DONNEES", ticker)
            continue

        entry_ts = parse_time(position["decision_time"])
        bars = data[data.index > entry_ts]
        exit_price = None
        exit_date = ""
        exit_reason = ""

        for index, row in bars.iterrows():
            opening = float(row["Open"])
            high = float(row["High"])
            low = float(row["Low"])
            target = float(position["target_price"])
            stop = float(position["stop_price"])

            if opening <= stop:
                exit_price = opening
                exit_reason = "GAP SOUS STOP"
            elif low <= stop:
                exit_price = stop
                exit_reason = "STOP NET -2%"
            elif opening >= target:
                exit_price = opening
                exit_reason = "GAP AU-DESSUS OBJECTIF"
            elif high >= target:
                exit_price = target
                exit_reason = "OBJECTIF NET +3%"

            if exit_price is not None:
                exit_date = index.strftime("%Y-%m-%d")
                break

        if exit_price is not None:
            quantity = int(float(position["quantity"]))
            gross_local = quantity * exit_price
            rate = 1.0 if position["currency"] == "EUR" else fx_at(fx, exit_date)
            gross_eur = gross_local if position["currency"] == "EUR" else gross_local / rate
            sell_fee = euro_fee(gross_eur) if position["currency"] == "EUR" else 10.5
            net = gross_eur - sell_fee
            realized = net - float(position["total_buy_cost_eur"])
            available += net
            position.update({
                "status": "CLOSED",
                "last_price": round(exit_price, 4),
                "market_value_eur": 0,
                "unrealized_pnl_eur": 0,
                "exit_date": exit_date,
                "exit_price": round(exit_price, 4),
                "gross_sell_value_local": round(gross_local, 2),
                "gross_sell_value_eur": round(gross_eur, 2),
                "sell_fee_eur": round(sell_fee, 2),
                "net_sell_proceeds_eur": round(net, 2),
                "realized_pnl_eur": round(realized, 2),
                "exit_reason": exit_reason,
            })
            print("VENTE", ticker, "|", exit_reason, "| PnL", round(realized, 2), "EUR")
        else:
            last = float(data["Close"].dropna().iloc[-1])
            date = data.index[-1].strftime("%Y-%m-%d")
            rate = 1.0 if position["currency"] == "EUR" else fx_at(fx, date)
            gross = (
                float(position["quantity"]) * last
                if position["currency"] == "EUR"
                else float(position["quantity"]) * last / rate
            )
            estimated_fee = euro_fee(gross) if position["currency"] == "EUR" else 10.5
            unrealized = gross - estimated_fee - float(position["total_buy_cost_eur"])
            position["last_price"] = round(last, 4)
            position["market_value_eur"] = round(gross, 2)
            position["unrealized_pnl_eur"] = round(unrealized, 2)
            print("SUIVI", ticker, "| cours", round(last, 2), position["currency"], "| PnL net", round(unrealized, 2), "EUR")

    return available


def build_candidates(
    decisions: List[Dict[str, str]],
    positions: List[Dict[str, Any]],
    score_map: Dict[tuple[str, str], int],
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
        next_state = normalize_label(decision.get("next_state"))

        if next_state != "POSITION A OUVRIR":
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

    candidates.sort(
        key=lambda row: (
            score_map.get((clean(row.get("ticker")), clean(row.get("signal_date"))), 0),
            float(row.get("decision_volume") or 0),
        ),
        reverse=True,
    )
    return candidates


def open_new_positions(
    positions: List[Dict[str, Any]],
    decisions: List[Dict[str, str]],
    available: float,
    total_equity: float,
    score_map: Dict[tuple[str, str], int],
    fx: pd.Series,
) -> float:
    candidates = build_candidates(decisions, positions, score_map)
    open_tickers = {
        clean(position.get("ticker"))
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    }
    open_count = len(open_tickers)
    position_limit = max_positions(total_equity)

    print("CANDIDATS EXECUTABLES :", len(candidates))
    print("FENETRE DECISION :", MAX_DECISION_AGE_MINUTES, "minutes")

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
            print("DECISION FUTURE", ticker, "| age", round(age_minutes, 1), "min | aucun achat")
            continue
        if age_minutes > MAX_DECISION_AGE_MINUTES:
            print("DECISION PERIMEE", ticker, "| age", round(age_minutes, 1), "min | aucun achat")
            continue
        if open_count >= position_limit:
            print("REFUSE", ticker, "| maximum", position_limit, "positions")
            continue

        price = float(decision["decision_close"])
        currency = "EUR" if ticker.endswith((".PA", ".AS")) else "USD"
        rate = 1.0 if currency == "EUR" else fx_at(fx, decision["session_date"])
        unit_eur = price if currency == "EUR" else price / rate
        budget = min(FIXED_ALLOCATION_EUR, available)
        base_fee = 1.0 if currency == "EUR" else 10.5
        quantity = math.floor(max(0, budget - base_fee) / unit_eur)

        if (
            quantity == 0
            and FIXED_ALLOCATION_EUR < unit_eur < 2000
            and available >= unit_eur + (2.55 if currency == "EUR" else 10.5)
        ):
            quantity = 1

        gross_local = quantity * price
        gross_eur = quantity * unit_eur
        buy_fee = (euro_fee(gross_eur) if currency == "EUR" else 10.5) if quantity else 0
        total_cost = gross_eur + buy_fee

        if quantity < 1 or total_cost > available + 0.0001:
            print("NON EXECUTABLE", ticker, "| cash", round(available, 2), "EUR | prix unitaire", round(unit_eur, 2), "EUR")
            continue

        estimated_target_fee = 10.5 if currency == "USD" else euro_fee(total_cost * 1.03)
        estimated_stop_fee = 10.5 if currency == "USD" else euro_fee(total_cost * 0.98)
        target_local = ((total_cost * 1.03 + estimated_target_fee) / quantity) * (rate if currency == "USD" else 1)
        stop_local = ((total_cost * 0.98 + estimated_stop_fee) / quantity) * (rate if currency == "USD" else 1)

        if stop_local >= price or target_local <= price:
            print("ORDRE REFUSE", ticker, "| frais incompatibles avec stop/objectif | entree", round(price, 2), "stop", round(stop_local, 2), "objectif", round(target_local, 2))
            continue

        available -= total_cost
        positions.append({
            "ticker": ticker,
            "signal_date": decision["signal_date"],
            "decision": decision["decision"],
            "decision_time": decision["decision_time"],
            "entry_date": decision["session_date"],
            "entry_price": round(price, 4),
            "currency": currency,
            "eurusd_entry": round(rate, 6),
            "quantity": quantity,
            "gross_buy_value_local": round(gross_local, 2),
            "gross_buy_value_eur": round(gross_eur, 2),
            "buy_fee_eur": round(buy_fee, 2),
            "total_buy_cost_eur": round(total_cost, 2),
            "target_price": round(target_local, 4),
            "stop_price": round(stop_local, 4),
            "status": "OPEN",
            "last_price": round(price, 4),
            "market_value_eur": round(gross_eur, 2),
            "unrealized_pnl_eur": round(-buy_fee - estimated_stop_fee, 2),
            "exit_date": "",
            "exit_price": "",
            "gross_sell_value_local": "",
            "gross_sell_value_eur": "",
            "sell_fee_eur": "",
            "net_sell_proceeds_eur": "",
            "realized_pnl_eur": "",
            "exit_reason": "",
        })
        open_count += 1
        open_tickers.add(ticker)
        print("ACHAT", ticker, "|", quantity, "action(s) | cout", round(total_cost, 2), "EUR | cash", round(available, 2), "EUR")

    return available


def main() -> int:
    cash = load_cash()
    available = float(cash.get("available_cash_eur") or INITIAL_CAPITAL_EUR)
    total_equity = float(cash.get("total_equity_eur") or INITIAL_CAPITAL_EUR)
    positions: List[Dict[str, Any]] = read_csv(PORTFOLIO_FILE)
    decisions = read_csv(DECISION_FILE)
    signals = read_csv(SIGNALS_FILE)
    score_map = {
        (clean(row.get("ticker")), clean(row.get("date"))): int(row.get("score") or 0)
        for row in signals
    }

    print("DECISIONS LUES :", len(decisions))
    print("POSITIONS LUES :", len(positions))

    fx = load_fx()
    available = update_open_positions(positions, available, fx)
    available = open_new_positions(
        positions, decisions, available, total_equity, score_map, fx
    )

    market_value = sum(
        float(position.get("market_value_eur") or 0)
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    )
    estimated_exit_fees = sum(
        euro_fee(float(position.get("market_value_eur") or 0))
        if position.get("currency") == "EUR"
        else 10.5
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    )
    invested = sum(
        float(position.get("total_buy_cost_eur") or 0)
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    )
    realized = sum(
        float(position.get("realized_pnl_eur") or 0)
        for position in positions
        if clean(position.get("status")).upper() == "CLOSED"
    )
    unrealized = sum(
        float(position.get("unrealized_pnl_eur") or 0)
        for position in positions
        if clean(position.get("status")).upper() == "OPEN"
    )
    fees = sum(
        float(position.get("buy_fee_eur") or 0)
        + float(position.get("sell_fee_eur") or 0)
        for position in positions
    )
    equity = available + market_value - estimated_exit_fees

    write_csv(PORTFOLIO_FILE, positions, PORTFOLIO_FIELDS)

    cash_out = {
        "initial_capital_eur": INITIAL_CAPITAL_EUR,
        "available_cash_eur": round(available, 2),
        "invested_cost_eur": round(invested, 2),
        "market_value_eur": round(market_value, 2),
        "realized_pnl_eur": round(realized, 2),
        "unrealized_pnl_eur": round(unrealized, 2),
        "fees_paid_eur": round(fees, 2),
        "total_equity_eur": round(equity, 2),
    }
    write_csv(CASH_FILE, [cash_out], list(cash_out))

    print("CAPITAL | cash", round(available, 2), "EUR | positions", round(market_value, 2), "EUR | equity nette", round(equity, 2), "EUR")
    print("V1ExecutionChanged : False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
