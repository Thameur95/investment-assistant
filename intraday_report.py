#!/usr/bin/env python3
"""Rapports intraday programmés vers Telegram et WhatsApp.

Lecture seule sur les données de trading. Anti-doublon indépendant par canal.
Une panne de messagerie ne modifie ni V1, ni Structure Shadow, ni les portefeuilles.
"""
from __future__ import annotations

import csv
import html
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "intraday_report_state.csv"
PARIS = ZoneInfo("Europe/Paris")
NOW = datetime.now(PARIS)
STATE_FIELDS = ["ReportID", "Channel", "ChunkIndex", "MessageID", "SentAtParis", "Status"]


def clean(v: Any) -> str:
    return "" if v is None else str(v).strip()


def esc(v: Any) -> str:
    return html.escape(clean(v))


def num(v: Any) -> str:
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def read(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def ts(v: Any):
    try:
        if not clean(v):
            return None
        x = pd.Timestamp(v)
        return x.tz_localize("Europe/Paris") if x.tzinfo is None else x.tz_convert("Europe/Paris")
    except Exception:
        return None


def slot() -> str:
    explicit = clean(os.getenv("REPORT_SLOT"))
    if explicit:
        return explicit.upper()
    h = NOW.hour
    return "MIDI" if h < 15 else "CLOTURE_EUROPE" if h < 20 else "CLOTURE_US"


def label() -> str:
    labels = {
        "MIDI": "MIDI",
        "CLOTURE_EUROPE": "CLÔTURE EUROPE",
        "CLOTURE_US": "CLÔTURE US",
    }
    return clean(os.getenv("REPORT_LABEL")) or labels.get(slot(), slot())


def plain(text: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        text = text.replace(tag, "")
    return html.unescape(text)


def whatsapp_safe_value(text: Any) -> str:
    """Nettoie une valeur injectée dans le paramètre BODY du template Meta."""
    value = clean(text).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    return " ".join(value.split())


def whatsapp_chunks_from_report(report: str, limit: int = 900) -> List[str]:
    """Transforme le rapport Telegram en blocs WhatsApp courts et lisibles.

    Le template Meta possède un unique paramètre BODY. On évite donc les retours
    à la ligne dans la variable et on conserve la structure grâce aux symboles
    •, ▪ et ━. Chaque section importante commence dans un nouveau message.
    """
    raw_lines = [plain(line).strip() for line in report.splitlines()]
    separator = "━━━━━━━━━━━━━━━━━━"
    sections: List[List[str]] = []
    current: List[str] = []

    for line in raw_lines:
        if not line:
            continue
        if line == separator:
            if current:
                sections.append(current)
                current = []
            continue
        current.append(line)
    if current:
        sections.append(current)

    blocks: List[str] = []
    for section in sections:
        if not section:
            continue
        title = whatsapp_safe_value(section[0])
        details = [whatsapp_safe_value(x) for x in section[1:] if whatsapp_safe_value(x)]
        block = f"{title}"
        if details:
            block += "  ▪  " + "  •  ".join(details)
        block = whatsapp_safe_value(block)

        while len(block) > limit:
            cut = block.rfind("  •  ", 0, limit + 1)
            if cut < 150:
                cut = block.rfind(" ", 0, limit + 1)
            if cut < 1:
                cut = limit
            blocks.append(block[:cut].strip())
            block = "↪ SUITE  ▪  " + block[cut:].lstrip(" •")
        if block:
            blocks.append(block)

    return blocks or ["📊 INVESTMENT ASSISTANT  ▪  Rapport vide."]


def build_report() -> str:
    today = NOW.strftime("%Y-%m-%d")
    decisions = read(ROOT / "intraday_decisions.csv")
    positions = read(ROOT / "virtual_portfolio.csv")
    cash_rows = read(ROOT / "portfolio_cash.csv")
    health_rows = read(ROOT / "monitor_health.csv")
    cash = cash_rows[0] if cash_rows else {}
    health = health_rows[0] if health_rows else {}

    active = set()
    stocks = ROOT / "stocks.txt"
    if stocks.exists():
        active = {
            x.strip()
            for x in stocks.read_text(encoding="utf-8").splitlines()
            if x.strip()
        }

    today_rows = [
        r
        for r in decisions
        if (not active or clean(r.get("ticker")) in active)
        and clean(r.get("session_date")) == today
    ]

    latest = {}
    for r in today_rows:
        x = ts(r.get("decision_time"))
        stamp = x.timestamp() if x is not None else 0
        ticker = clean(r.get("ticker"))
        if ticker not in latest or stamp > latest[ticker][0]:
            latest[ticker] = (stamp, r)

    current = [v[1] for v in latest.values()]
    confirmed = sorted(
        [
            r
            for r in current
            if clean(r.get("decision"))
            in ("ACHAT CONFIRME", "ACHAT CONFIRMÉ", "ACHAT REACTIVE", "ACHAT RÉACTIF")
        ],
        key=lambda r: clean(r.get("ticker")),
    )
    watching = sorted(
        [
            r
            for r in current
            if clean(r.get("next_state")) == "SURVEILLANCE CATALYSEUR"
            and clean(r.get("reason")) != "Aucune confirmation pendant une heure"
        ],
        key=lambda r: clean(r.get("ticker")),
    )
    expired = sorted(
        [
            r
            for r in current
            if clean(r.get("decision")) == "EXPIRE"
            or clean(r.get("reason")) == "Aucune confirmation pendant une heure"
        ],
        key=lambda r: clean(r.get("ticker")),
    )
    pending = sorted(
        [r for r in current if clean(r.get("decision")) == "EN ATTENTE"],
        key=lambda r: clean(r.get("ticker")),
    )

    lines = [
        f"<b>📊 INVESTMENT ASSISTANT | {esc(label())}</b>",
        f"📅 {NOW.strftime('%d/%m/%Y')} | Heure de Paris",
        "",
    ]

    last = ts(health.get("last_success_paris"))
    if last is None:
        known = [ts(r.get("decision_time")) for r in today_rows]
        known = [x for x in known if x is not None]
        last = max(known) if known else None

    if last is None:
        lines += [
            "<b>⚪ FRAÎCHEUR : INDISPONIBLE</b>",
            "Aucun contrôle horodaté disponible.",
            "",
        ]
    else:
        age = max(0, (NOW - last.to_pydatetime()).total_seconds() / 60)
        icon, state = (
            ("🟢", "À JOUR")
            if age <= 10
            else (("🟠", "RETARDÉE") if age <= 20 else ("🔴", "INTERROMPUE"))
        )
        lines += [
            f"<b>{icon} FRAÎCHEUR : {state}</b>",
            f"Dernière actualisation : {last.strftime('%d/%m/%Y à %H:%M')} Paris",
            f"Âge estimé : {age:.0f} min",
            "",
        ]

    lines += [
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        "<b>✅ DÉCISIONS DU JOUR</b>",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
    ]
    if confirmed:
        for r in confirmed:
            ticker = clean(r.get("ticker"))
            cur = "EUR" if ticker.endswith((".PA", ".AS")) else "USD"
            when = ts(r.get("decision_time"))
            lines += [
                "",
                f"<b>{esc(ticker)} | {esc(r.get('decision'))}</b>",
                f"Décision : {when.strftime('%H:%M Paris') if when else 'heure inconnue'}",
                f"Prix : {num(r.get('decision_close'))} {cur} | VWAP : {num(r.get('vwap'))}",
                f"OR haut : {num(r.get('opening_range_high'))} | OR bas : {num(r.get('opening_range_low'))}",
                f"Motif : {esc(r.get('reason'))}",
            ]
    else:
        lines.append("Aucune décision confirmée aujourd'hui.")

    lines += [
        "",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        "<b>👀 DOSSIERS DU JOUR</b>",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
    ]
    for r in watching:
        lines += [
            "",
            f"<b>{esc(r.get('ticker'))} | SURVEILLANCE CATALYSEUR</b>",
            f"Motif : {esc(r.get('reason'))}",
        ]
    for r in expired:
        lines += [
            "",
            f"<b>⌛ {esc(r.get('ticker'))} | SETUP EXPIRÉ</b>",
            "Aucune confirmation dans le délai prévu.",
        ]
    if pending:
        lines += [
            "",
            "<b>⏳ EN ATTENTE</b>",
            ", ".join(esc(r.get("ticker")) for r in pending),
        ]
    if not watching and not expired and not pending:
        lines.append("Aucun autre dossier actif aujourd'hui.")

    lines += [
        "",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        "<b>💼 PORTEFEUILLE VIRTUEL V1</b>",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
    ]
    if positions:
        for p in positions:
            ticker = clean(p.get("ticker") or p.get("Ticker"))
            status = clean(p.get("status") or p.get("Status")).upper()
            cur = clean(p.get("currency") or p.get("Currency")) or "EUR"
            if status == "OPEN":
                lines += [
                    "",
                    f"<b>✅ {esc(ticker)} | OUVERT</b>",
                    f"Quantité : {esc(p.get('quantity') or p.get('Quantity'))}",
                    f"Entrée : {num(p.get('entry_price') or p.get('EntryPrice'))} {cur} | Dernier : {num(p.get('last_price') or p.get('LastPrice'))} {cur}",
                    f"PnL latent : {num(p.get('unrealized_pnl_eur') or p.get('UnrealizedPnLEUR'))} EUR",
                    f"🎯 TP : {num(p.get('target_price') or p.get('TargetPrice'))} {cur}",
                    f"🛡 SL : {num(p.get('stop_price') or p.get('StopPrice'))} {cur}",
                ]
            elif status == "CLOSED":
                lines += [
                    "",
                    f"<b>❌ {esc(ticker)} | FERMÉ</b>",
                    f"Sortie : {num(p.get('exit_price') or p.get('ExitPrice'))} {cur}",
                    f"PnL réalisé : {num(p.get('realized_pnl_eur') or p.get('RealizedPnLEUR'))} EUR",
                    f"Motif : {esc(p.get('exit_reason') or p.get('ExitReason'))}",
                ]
    else:
        lines.append("Aucune position virtuelle.")

    initial = float(cash.get("initial_capital_eur") or cash.get("InitialCapitalEUR") or 3000)
    available = float(cash.get("available_cash_eur") or cash.get("AvailableCashEUR") or 0)
    market = float(cash.get("market_value_eur") or cash.get("MarketValueEUR") or 0)
    equity = float(
        cash.get("total_equity_eur")
        or cash.get("TotalEquityEUR")
        or available + market
    )
    total = equity - initial

    lines += [
        "",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        "<b>💶 CAPITAL</b>",
        "<b>━━━━━━━━━━━━━━━━━━</b>",
        f"Capital initial : {num(initial)} EUR",
        f"Liquidités : {num(available)} EUR",
        f"Valeur positions : {num(market)} EUR",
        f"<b>Equity nette : {num(equity)} EUR</b>",
        f"Résultat global : {total:+.2f} EUR",
        "",
        "<i>Paper trading uniquement. Aucun ordre réel.</i>",
    ]
    return "\n".join(lines)


def send_telegram(text: str) -> str:
    url = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"] + "/sendMessage"
    r = requests.post(
        url,
        data={
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    body = r.json() if r.content else {}
    if r.status_code != 200 or not body.get("ok"):
        raise RuntimeError(r.text)
    return clean(body.get("result", {}).get("message_id"))


def send_whatsapp(text: str) -> str:
    version = clean(os.getenv("WHATSAPP_GRAPH_VERSION")) or "v24.0"
    phone_number_id = clean(os.environ["WHATSAPP_PHONE_NUMBER_ID"])
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": clean(os.environ["WHATSAPP_RECIPIENT"]),
        "type": "template",
        "template": {
            "name": clean(os.environ["WHATSAPP_TEMPLATE_NAME"]),
            "language": {
                "code": clean(os.getenv("WHATSAPP_TEMPLATE_LANGUAGE")) or "fr"
            },
            "components": [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": whatsapp_safe_value(text)}],
                }
            ],
        },
    }
    r = requests.post(
        url,
        headers={
            "Authorization": "Bearer " + clean(os.environ["WHATSAPP_ACCESS_TOKEN"]),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    body = r.json() if r.content else {}
    msgs = body.get("messages") or []
    if r.status_code not in (200, 201) or not msgs:
        raise RuntimeError(r.text)
    return clean(msgs[0].get("id"))


def main() -> int:
    report_id = f"{NOW.strftime('%Y-%m-%d')}|{slot()}"
    message = build_report()
    old = read(STATE)
    sent = {
        (clean(r.get("ReportID")), clean(r.get("Channel")), clean(r.get("ChunkIndex")))
        for r in old
        if clean(r.get("MessageID"))
    }
    rows = list(old)
    errors = 0
    tg = 0
    wa = 0

    if (
        clean(os.getenv("TELEGRAM_BOT_TOKEN"))
        and clean(os.getenv("TELEGRAM_CHAT_ID"))
        and (report_id, "TELEGRAM", "0") not in sent
    ):
        try:
            mid = send_telegram(message)
            rows.append(
                {
                    "ReportID": report_id,
                    "Channel": "TELEGRAM",
                    "ChunkIndex": "0",
                    "MessageID": mid,
                    "SentAtParis": NOW.isoformat(timespec="seconds"),
                    "Status": "SENT",
                }
            )
            tg = 1
        except Exception as e:
            errors += 1
            print("Rapport Telegram ERROR :", e)

    required = (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_RECIPIENT",
        "WHATSAPP_TEMPLATE_NAME",
    )
    if all(clean(os.getenv(x)) for x in required):
        for i, part in enumerate(whatsapp_chunks_from_report(message), 1):
            key = (report_id, "WHATSAPP", str(i))
            if key in sent:
                continue
            try:
                mid = send_whatsapp(part)
                rows.append(
                    {
                        "ReportID": report_id,
                        "Channel": "WHATSAPP",
                        "ChunkIndex": str(i),
                        "MessageID": mid,
                        "SentAtParis": NOW.isoformat(timespec="seconds"),
                        "Status": "SENT",
                    }
                )
                wa += 1
            except Exception as e:
                errors += 1
                rows.append(
                    {
                        "ReportID": report_id,
                        "Channel": "WHATSAPP",
                        "ChunkIndex": str(i),
                        "MessageID": "",
                        "SentAtParis": "",
                        "Status": "ERROR",
                    }
                )
                print(f"Rapport WhatsApp chunk {i} ERROR :", e)
                break

    with STATE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=STATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    print("ReportID :", report_id)
    print("Rapport Telegram envoyé :", tg)
    print("Rapports WhatsApp envoyés :", wa)
    print("Erreurs de messagerie :", errors)
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
