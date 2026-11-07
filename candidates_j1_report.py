#!/usr/bin/env python3
"""Rapport autonome des candidats V1 à surveiller à la prochaine séance.

Source unique :
    signals_history.csv

Ce script ne recalcule aucun signal et ne modifie aucune règle V1.
Il présente uniquement les résultats déjà produits par scanner.py.
"""

from __future__ import annotations

import csv
import html
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from intraday_report import send_telegram, send_whatsapp


ROOT = Path(__file__).resolve().parent
SIGNALS_FILE = ROOT / "signals_history.csv"
STATE_FILE = ROOT / "candidates_j1_report_state.json"
FRESHNESS_FILE = ROOT / "scanner_freshness_status.json"

PARIS = ZoneInfo("Europe/Paris")
NOW_PARIS = datetime.now(PARIS)

SELECTED_SIGNALS = {
    "ACHAT",
    "SURVEILLANCE",
    "SURVEILLANCE HAUTE",
}


def clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(clean(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def next_weekday(source_date: date) -> date:
    """Retourne le prochain jour de semaine.

    Cette fonction gère les week-ends mais pas les jours fériés boursiers.
    """
    candidate = source_date + timedelta(days=1)

    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)

    return candidate


def load_latest_selection() -> tuple[str, list[dict[str, str]]]:
    if not SIGNALS_FILE.exists():
        raise FileNotFoundError(f"Fichier absent : {SIGNALS_FILE}")

    with SIGNALS_FILE.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        rows = list(csv.DictReader(handle))

    valid_rows = [
        row
        for row in rows
        if clean(row.get("date")) and clean(row.get("ticker"))
    ]

    if not valid_rows:
        raise RuntimeError("signals_history.csv ne contient aucune donnée exploitable.")

    latest_date = max(clean(row.get("date")) for row in valid_rows)

    latest_rows = [
        row for row in valid_rows
        if clean(row.get("date")) == latest_date
    ]

    selected = [
        row for row in latest_rows
        if clean(row.get("signal")).upper() in SELECTED_SIGNALS
    ]

    selected.sort(
        key=lambda row: (
            -as_float(row.get("score")),
            clean(row.get("ticker")),
        )
    )

    return latest_date, selected


def load_state() -> dict[str, object]:
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(report_id: str, telegram_id: str, whatsapp_id: str) -> None:
    payload = {
        "report_id": report_id,
        "sent_at_paris": NOW_PARIS.isoformat(timespec="seconds"),
        "telegram_message_id": telegram_id,
        "whatsapp_message_id": whatsapp_id,
    }

    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def signal_icon(signal: str) -> str:
    normalized = clean(signal).upper()

    if normalized == "ACHAT":
        return "🎯"

    if normalized == "SURVEILLANCE HAUTE":
        return "🔶"

    return "👀"


def build_report(
    source_date: str,
    candidates: list[dict[str, str]],
) -> str:
    source_day = date.fromisoformat(source_date)
    target_day = next_weekday(source_day)

    lines = [
        "<b>🔭 INVESTMENT ASSISTANT | CANDIDATS J+1</b>",
        f"📅 Prochaine séance : {target_day.strftime('%d/%m/%Y')}",
        f"Données de clôture : {source_day.strftime('%d/%m/%Y')}",
        f"Généré : {NOW_PARIS.strftime('%d/%m/%Y à %H:%M')} Paris",
        "",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        "<b>🎯 WATCHLIST V1 POUR LA PROCHAINE SÉANCE</b>",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
    ]

    if not candidates:
        lines.extend(
            [
                "Aucun candidat ACHAT ou SURVEILLANCE dans la dernière sélection.",
                "",
            ]
        )
    else:
        achat_count = sum(
            clean(row.get("signal")).upper() == "ACHAT"
            for row in candidates
        )

        surveillance_count = len(candidates) - achat_count

        lines.extend(
            [
                f"Candidats retenus : <b>{len(candidates)}</b>",
                f"ACHAT scanner J-1 : {achat_count}",
                f"SURVEILLANCE : {surveillance_count}",
                "",
            ]
        )

        for index, row in enumerate(candidates, start=1):
            ticker = html.escape(clean(row.get("ticker")))
            signal = html.escape(clean(row.get("signal")).upper())
            score = int(as_float(row.get("score")))
            price = as_float(row.get("price"))
            var5 = as_float(row.get("var5"))
            rsi14 = as_float(row.get("rsi14"))
            rvol = as_float(row.get("rvol"))
            atr14_pct = as_float(row.get("atr14_pct"))

            lines.extend(
                [
                    f"<b>{index}. {signal_icon(signal)} {ticker}</b>",
                    f"Signal scanner : <b>{signal}</b>",
                    f"Score : <b>{score}/100</b>",
                    f"Clôture : {price:.2f}",
                    f"Variation 5 jours : {var5:+.2f} %",
                    f"RSI14 : {rsi14:.1f}",
                    f"RVOL J-1 : {rvol:.2f}",
                    f"ATR14 : {atr14_pct:.2f} %",
                    "",
                ]
            )

    lines.extend(
        [
            "<b>━━━━━━━━━━━━━━━━━━</b>",
            "<b>📋 À CONTRÔLER À L'OUVERTURE</b>",
            "<b>━━━━━━━━━━━━━━━━━━</b>",
            "• gap d'ouverture",
            "• Opening Range",
            "• position par rapport au VWAP",
            "• RVOL intraday",
            "• validation ACHAT CONFIRMÉ ou ACHAT RÉACTIF",
            "",
            "<i>Watchlist préparatoire uniquement.</i>",
            "<i>La présence dans ce rapport ne constitue pas une entrée.</i>",
            "<i>La décision V1 reste soumise aux règles intraday existantes.</i>",
            "<i>Paper trading uniquement. Aucun ordre réel.</i>",
        ]
    )

    return "\n".join(lines)

def scanner_is_fresh() -> tuple[bool, str]:
    if not FRESHNESS_FILE.exists():
        return False, "scanner_freshness_status.json absent"

    try:
        status = json.loads(FRESHNESS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "scanner_freshness_status.json illisible"

    scanner_status = clean(status.get("status"))
    expected_date = clean(status.get("expected_session_date"))

    if scanner_status != "FRESH":
        return False, f"scanner {scanner_status or 'UNKNOWN'}"

    if expected_date != NOW_PARIS.date().isoformat():
        return False, f"fraîcheur datée du {expected_date}"

    return True, "FRESH"

def main() -> int:
    fresh, freshness_reason = scanner_is_fresh()

    if not fresh:
        print("Rapport J+1 bloqué :", freshness_reason)
        print("Aucun message Telegram ou WhatsApp envoyé.")
        print("V1ExecutionChanged : False")
        print("PortfolioV1Changed : False")
        return 0

    source_date, candidates = load_latest_selection()
    report_id = f"{source_date}|CANDIDATS_J1"
    report = build_report(source_date, candidates)

    if clean(os.getenv("CANDIDATES_DRY_RUN")) == "1":
        print(report)
        print()
        print("Mode : DRY RUN")
        print("ReportID :", report_id)
        print("Candidats :", len(candidates))
        return 0

    force_send = clean(os.getenv("FORCE_SEND")) == "1"
    previous_state = load_state()

    if (
        not force_send
        and clean(previous_state.get("report_id")) == report_id
    ):
        print("Rapport déjà envoyé :", report_id)
        return 0

    telegram_id = ""
    whatsapp_id = ""
    errors = 0

    try:
        telegram_id = send_telegram(report)
    except Exception as error:
        errors += 1
        print("Rapport candidats Telegram ERROR :", error)

    try:
        whatsapp_text = html.unescape(
            report
            .replace("<b>", "")
            .replace("</b>", "")
            .replace("<i>", "")
            .replace("</i>", "")
        )
        whatsapp_id = send_whatsapp(whatsapp_text)
    except Exception as error:
        errors += 1
        print("Rapport candidats WhatsApp ERROR :", error)

    if telegram_id or whatsapp_id:
        save_state(report_id, telegram_id, whatsapp_id)

    print("ReportID :", report_id)
    print("Candidats sélectionnés :", len(candidates))
    print("Rapport Telegram envoyé :", 1 if telegram_id else 0)
    print("Rapport WhatsApp envoyé :", 1 if whatsapp_id else 0)
    print("Erreurs de messagerie :", errors)
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
