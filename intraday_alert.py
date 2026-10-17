#!/usr/bin/env python3
"""Alertes Telegram immédiates et anti-doublon.

Sources :
- intraday_decisions.csv : décisions V1 récentes ;
- virtual_portfolio.csv : achats et ventes virtuels V1 réellement enregistrés ;
- historical_lab/results/structure_shadow_portfolio_events.csv : exécutions Shadow.

Le script n'exécute aucun ordre et ne modifie aucun portefeuille.
Une panne Telegram fait échouer uniquement l'étape d'alerte, sans modifier V1.
"""

from __future__ import annotations

import csv
import html
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DECISIONS = ROOT / "intraday_decisions.csv"
V1_PORTFOLIO = ROOT / "virtual_portfolio.csv"
STRUCTURE_EVENTS = ROOT / "historical_lab" / "results" / "structure_shadow_portfolio_events.csv"
STATE_FILE = ROOT / "intraday_alert_state.csv"

PARIS = ZoneInfo("Europe/Paris")
NOW_PARIS = datetime.now(PARIS)
NOW_UTC = pd.Timestamp.now(tz="UTC")
MAX_DECISION_AGE_MIN = 10
MAX_EXECUTION_AGE_MIN = 15

STATE_FIELDS = ["EventID", "EventType", "Ticker", "Source", "SentAtParis", "TelegramMessageID"]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def esc(value: Any) -> str:
    return html.escape(clean(value))


def number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "N/A"


def first(row: Dict[str, str], *names: str) -> str:
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        if name in row and clean(row[name]):
            return clean(row[name])
        value = lower.get(name.lower())
        if clean(value):
            return clean(value)
    return ""


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_state(rows: List[Dict[str, str]]) -> None:
    with STATE_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def timestamp(value: Any, default_zone: str = "Europe/Paris") -> Optional[pd.Timestamp]:
    if not clean(value):
        return None
    try:
        result = pd.Timestamp(value)
        if pd.isna(result):
            return None
        return result.tz_localize(default_zone) if result.tzinfo is None else result
    except Exception:
        return None


def age_minutes(value: Any, default_zone: str = "Europe/Paris") -> Optional[float]:
    stamp = timestamp(value, default_zone)
    if stamp is None:
        return None
    return (NOW_UTC - stamp.tz_convert("UTC")).total_seconds() / 60.0


def is_fresh(value: Any, limit: int, default_zone: str = "Europe/Paris") -> bool:
    age = age_minutes(value, default_zone)
    return age is not None and 0 <= age <= limit


def paris_text(value: Any, default_zone: str = "Europe/Paris") -> str:
    result = timestamp(value, default_zone)
    return result.tz_convert("Europe/Paris").strftime("%d/%m/%Y à %H:%M Paris") if result is not None else "heure inconnue"


def send_telegram(text: str) -> str:
    response = requests.post(
        "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/sendMessage",
        data={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    payload = response.json() if response.content else {}
    if response.status_code != 200 or not payload.get("ok"):
        raise RuntimeError("Échec Telegram: " + response.text)
    return clean(payload.get("result", {}).get("message_id"))


def decision_events() -> List[Dict[str, str]]:
    events = []
    allowed = {"ACHAT CONFIRME", "ACHAT REACTIVE", "ACHAT RÉACTIF", "SETUP INITIAL INVALIDE", "EXPIRE"}
    for row in read_csv(DECISIONS):
        decision = first(row, "decision", "Decision")
        if decision not in allowed:
            continue
        ticker = first(row, "ticker", "Ticker")
        signal_date = first(row, "signal_date", "SignalDate")
        session_date = first(row, "session_date", "SessionDate")
        decision_time = first(row, "decision_time", "DecisionTime")
        event_id = f"V1_DECISION|{ticker}|{signal_date}|{session_date}|{decision}"
        if not is_fresh(decision_time, MAX_DECISION_AGE_MIN):
            continue
        currency = "EUR" if ticker.endswith((".PA", ".AS")) else "USD"
        icon = "✅" if decision == "ACHAT CONFIRME" else "🔄" if "REACT" in decision or "RÉACT" in decision else "⚠️" if decision == "SETUP INITIAL INVALIDE" else "⌛"
        text = (
            f"<b>{icon} SIGNAL V1 | {esc(decision)}</b>\n\n"
            f"<b>{esc(ticker)}</b>\n"
            f"Décision : {paris_text(decision_time)}\n"
            f"Prix de clôture/décision : <b>{number(first(row, 'decision_close', 'DecisionClose'))} {currency}</b>\n"
            f"VWAP : {number(first(row, 'vwap', 'VWAP'))}\n"
            f"OR haut : {number(first(row, 'opening_range_high'))} {currency}\n"
            f"OR bas : {number(first(row, 'opening_range_low'))} {currency}\n"
            f"Volume : {number(first(row, 'decision_volume'), 0)}\n\n"
            f"Motif : {esc(first(row, 'reason', 'Reason'))}\n"
            f"État suivant : {esc(first(row, 'next_state', 'NextState'))}\n\n"
            "<i>Paper trading uniquement.</i>"
        )
        events.append({"EventID": event_id, "EventType": "V1_DECISION", "Ticker": ticker, "Source": "V1", "Text": text})
    return events


def v1_execution_events() -> List[Dict[str, str]]:
    events = []
    for row in read_csv(V1_PORTFOLIO):
        ticker = first(row, "ticker", "Ticker")
        status = first(row, "status", "Status").upper()
        currency = first(row, "currency", "Currency") or ("EUR" if ticker.endswith((".PA", ".AS")) else "USD")
        entry_time = first(row, "entry_time", "EntryTime", "entry_date", "EntryDate")
        entry_price = first(row, "entry_price", "EntryPrice")
        quantity = first(row, "quantity", "Quantity")
        position_id = first(row, "position_id", "PositionID") or f"{ticker}|{entry_time}|{entry_price}"

        if entry_time and is_fresh(entry_time, MAX_EXECUTION_AGE_MIN):
            event_id = f"V1_BUY|{position_id}|{entry_time}"
            text = (
                "<b>🟢 ACHAT V1 EXÉCUTÉ</b>\n\n"
                f"<b>{esc(ticker)}</b>\n"
                f"Heure : {paris_text(entry_time)}\n"
                f"Prix : <b>{number(entry_price)} {esc(currency)}</b>\n"
                f"Quantité : {esc(quantity)} action(s)\n"
                f"Montant total : {number(first(row, 'total_buy_cost_eur', 'TotalBuyCostEUR'))} EUR\n"
                f"🎯 TP : {number(first(row, 'target_price', 'TargetPrice'))} {esc(currency)}\n"
                f"🛡 SL : {number(first(row, 'stop_price', 'StopPrice'))} {esc(currency)}\n"
                f"Cash restant : {number(first(row, 'available_cash_eur', 'AvailableCashEUR'))} EUR\n\n"
                "<i>Exécution virtuelle V1. Aucun ordre réel.</i>"
            )
            events.append({"EventID": event_id, "EventType": "V1_BUY_EXECUTED", "Ticker": ticker, "Source": "V1", "Text": text})

        exit_time = first(row, "exit_time", "ExitTime", "exit_date", "ExitDate")
        if status == "CLOSED" and exit_time and is_fresh(exit_time, MAX_EXECUTION_AGE_MIN):
            pnl = first(row, "realized_pnl_eur", "RealizedPnLEUR")
            ret = first(row, "return_pct", "ReturnPct")
            event_id = f"V1_SELL|{position_id}|{exit_time}"
            text = (
                "<b>🔴 VENTE V1 EXÉCUTÉE</b>\n\n"
                f"<b>{esc(ticker)}</b>\n"
                f"Heure : {paris_text(exit_time)}\n"
                f"Entrée : {number(entry_price)} {esc(currency)}\n"
                f"Sortie : <b>{number(first(row, 'exit_price', 'ExitPrice'))} {esc(currency)}</b>\n"
                f"Quantité : {esc(quantity)} action(s)\n"
                f"Motif : {esc(first(row, 'exit_reason', 'ExitReason'))}\n"
                f"PnL réalisé : <b>{number(pnl)} EUR</b>\n"
                f"Rendement : <b>{number(ret)} %</b>\n\n"
                "<i>Exécution virtuelle V1. Aucun ordre réel.</i>"
            )
            events.append({"EventID": event_id, "EventType": "V1_SELL_EXECUTED", "Ticker": ticker, "Source": "V1", "Text": text})
    return events


def structure_execution_events() -> List[Dict[str, str]]:
    events = []
    for row in read_csv(STRUCTURE_EVENTS):
        event_type = first(row, "EventType")
        if event_type not in {"SHADOW_BUY_EXECUTED", "SHADOW_SELL_EXECUTED"}:
            continue
        event_time = first(row, "EventTimeUTC")
        if not is_fresh(event_time, MAX_EXECUTION_AGE_MIN, "UTC"):
            continue
        event_id = first(row, "EventID")
        ticker = first(row, "Ticker")
        buy = event_type == "SHADOW_BUY_EXECUTED"
        title = "🟡 ACHAT VIRTUEL STRUCTURE SHADOW" if buy else "🟡 VENTE VIRTUELLE STRUCTURE SHADOW"
        text = (
            f"<b>{title}</b>\n\n"
            f"<b>{esc(ticker)}</b>\n"
            f"Heure : {paris_text(event_time, 'UTC')}\n"
            f"Prix : <b>{number(first(row, 'Price'))}</b>\n"
            f"Quantité : {esc(first(row, 'Quantity'))} action(s)\n"
            f"Montant : {number(first(row, 'AmountEUR'))} EUR\n"
            f"Détail : {esc(first(row, 'Reason'))}\n\n"
            "<i>Portefeuille Structure Shadow séparé. Aucun ordre réel.</i>"
        )
        events.append({"EventID": event_id, "EventType": event_type, "Ticker": ticker, "Source": "STRUCTURE_SHADOW", "Text": text})
    return events


def main() -> int:
    token = clean(os.environ.get("TELEGRAM_BOT_TOKEN"))
    chat = clean(os.environ.get("TELEGRAM_CHAT_ID"))
    if not token or not chat:
        print("Telegram non configuré : aucune alerte envoyée.")
        return 0

    state = read_csv(STATE_FILE)
    sent_ids = {first(row, "EventID") for row in state if first(row, "EventID")}
    candidates = decision_events() + v1_execution_events() + structure_execution_events()
    sent = 0

    for event in candidates:
        if event["EventID"] in sent_ids:
            continue
        message_id = send_telegram(event["Text"])
        state.append({
            "EventID": event["EventID"],
            "EventType": event["EventType"],
            "Ticker": event["Ticker"],
            "Source": event["Source"],
            "SentAtParis": NOW_PARIS.isoformat(timespec="seconds"),
            "TelegramMessageID": message_id,
        })
        sent_ids.add(event["EventID"])
        sent += 1
        print(f"TELEGRAM OK | {event['EventType']} | {event['Ticker']} | message {message_id}")

    write_state(state)
    print(f"Alertes Telegram immédiates envoyées : {sent}")
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
