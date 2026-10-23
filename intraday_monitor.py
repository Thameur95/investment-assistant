#!/usr/bin/env python3
"""Moniteur intraday V1.

Logique V1 inchangée : Opening Range, VWAP, volume, confirmation dans les 12
bougies de 5 minutes et achat réactif après invalidation.

Ajout ciblé : transporte ATR14Pct J-1 depuis signals_history.csv vers
intraday_decisions.csv afin que virtual_portfolio.py puisse appliquer les
TP/SL ATR de la V1 figée.
"""
import csv
import os

import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

T = {x.strip() for x in open("stocks.txt", encoding="utf-8") if x.strip()}
rows = [
    r
    for r in csv.DictReader(open("signals_history.csv", encoding="utf-8"))
    if r["ticker"] in T
]
latest = {}
for r in rows:
    if r["ticker"] not in latest or r["date"] > latest[r["ticker"]]["date"]:
        latest[r["ticker"]] = r

candidates = [r for r in latest.values() if r["signal"] == "ACHAT"]
decisions = []
print("===== CONFIRMATION INTRADAY - SIMULATION =====")
if not candidates:
    print("Aucun candidat ACHAT disponible.")

for r in candidates:
    t = r["ticker"]
    signal_date = r["date"]
    signal_price = float(r["price"])
    atr14_pct_j1 = r.get("atr14_pct", "")

    try:
        d = yf.download(
            t,
            period="5d",
            interval="5m",
            progress=False,
            auto_adjust=True,
            prepost=False,
        )
        if d.empty:
            print("\n" + t + " | AUCUNE DONNEE")
            continue
        if hasattr(d.columns, "levels"):
            d.columns = d.columns.get_level_values(0)

        sessions = sorted(
            {
                x.strftime("%Y-%m-%d")
                for x in d.index
                if x.strftime("%Y-%m-%d") > signal_date
            }
        )
        if not sessions:
            print("\n" + t + " | EN ATTENTE DU JOUR J")
            continue

        session = sessions[0]
        s = d[d.index.strftime("%Y-%m-%d") == session].copy()
        if len(s) < 4:
            print(
                "\n"
                + t
                + " | EN ATTENTE DES 4 BOUGIES | Disponibles : "
                + str(len(s))
            )
            continue

        opening = float(s["Open"].iloc[0])
        gap = (opening / signal_price - 1) * 100
        first4 = s.iloc[:4]
        or_high = float(first4["High"].max())
        or_low = float(first4["Low"].min())
        close4 = float(first4["Close"].iloc[-1])
        red4 = int((first4["Close"] < first4["Open"]).sum())
        vv = first4["Volume"][first4["Volume"] > 0]
        vol4 = float(vv.mean()) if len(vv) > 0 else 0

        decision = "EN ATTENTE"
        next_state = "PENDING"
        reason = "Prix dans Opening Range"
        dt = ""
        op = ""
        hi = ""
        lo = ""
        cl = ""
        vol = ""
        vwap_value = ""
        invalid_pos = 3

        full = s.copy()
        typical_full = (full["High"] + full["Low"] + full["Close"]) / 3
        full["VWAP_FULL"] = (
            (typical_full * full["Volume"]).cumsum()
            / full["Volume"].cumsum().replace(0, float("nan"))
        )

        if gap <= -3:
            decision = "SETUP INITIAL INVALIDE"
            next_state = "SURVEILLANCE CATALYSEUR"
            reason = "Gap baissier >= 3%"
        elif close4 <= opening * 0.98:
            decision = "SETUP INITIAL INVALIDE"
            next_state = "SURVEILLANCE CATALYSEUR"
            reason = "Baisse >= 2% apres 20 minutes"
        elif red4 >= 3 and close4 < opening:
            decision = "SETUP INITIAL INVALIDE"
            next_state = "SURVEILLANCE CATALYSEUR"
            reason = "Au moins 3 bougies rouges sur 4"
        else:
            win = full.iloc[:12].copy()
            for pos, (idx, row) in enumerate(win.iloc[4:].iterrows(), start=4):
                op = float(row["Open"])
                hi = float(row["High"])
                lo = float(row["Low"])
                cl = float(row["Close"])
                vol = float(row["Volume"])
                vw = float(row["VWAP_FULL"])

                if lo < or_low or cl <= opening * 0.98:
                    decision = "SETUP INITIAL INVALIDE"
                    next_state = "SURVEILLANCE CATALYSEUR"
                    reason = "Cassure baissiere Opening Range"
                    dt = str(idx)
                    vwap_value = round(vw, 4)
                    invalid_pos = pos
                    break

                if cl > or_high and cl > vw and vol >= vol4:
                    decision = "ACHAT CONFIRME"
                    next_state = "POSITION A OUVRIR"
                    reason = "Cassure haussiere + VWAP + volume"
                    dt = str(idx)
                    vwap_value = round(vw, 4)
                    break

            if decision == "EN ATTENTE" and len(s) >= 12:
                decision = "SETUP INITIAL INVALIDE"
                next_state = "SURVEILLANCE CATALYSEUR"
                reason = "Aucune confirmation pendant une heure"
                row = win.iloc[-1]
                dt = str(win.index[-1])
                op = float(row["Open"])
                hi = float(row["High"])
                lo = float(row["Low"])
                cl = float(row["Close"])
                vol = float(row["Volume"])
                vwap_value = round(float(row["VWAP_FULL"]), 4)
                invalid_pos = 11

        if decision == "SETUP INITIAL INVALIDE":
            for idx2, row2 in full.iloc[invalid_pos + 1 :].iterrows():
                close2 = float(row2["Close"])
                vol2 = float(row2["Volume"])
                vw2 = float(row2["VWAP_FULL"])
                if close2 > or_high and close2 > vw2 and vol2 >= vol4 * 1.5:
                    decision = "ACHAT REACTIVE"
                    next_state = "POSITION A OUVRIR"
                    reason = "Retournement confirme : OR High + VWAP + volume"
                    dt = str(idx2)
                    op = float(row2["Open"])
                    hi = float(row2["High"])
                    lo = float(row2["Low"])
                    cl = close2
                    vol = vol2
                    vwap_value = round(vw2, 4)
                    break

        decisions.append(
            {
                "ticker": t,
                "signal_date": signal_date,
                "session_date": session,
                "decision_time": dt,
                "signal_price": round(signal_price, 4),
                "atr14_pct_j1": atr14_pct_j1,
                "opening_price": round(opening, 4),
                "gap_pct": round(gap, 2),
                "opening_range_high": round(or_high, 4),
                "opening_range_low": round(or_low, 4),
                "close_bougie_4": round(close4, 4),
                "red_candles_4": red4,
                "initial_avg_volume": round(vol4),
                "decision_open": op,
                "decision_high": hi,
                "decision_low": lo,
                "decision_close": cl,
                "decision_volume": vol,
                "vwap": vwap_value,
                "decision": decision,
                "next_state": next_state,
                "reason": reason,
            }
        )

        print("\n" + t + " | Jour J " + session + " | " + decision)
        print("Etat suivant : " + next_state + " | Motif : " + reason)
        print(
            "Opening "
            + str(round(opening, 2))
            + " | OR Haut "
            + str(round(or_high, 2))
            + " | OR Bas "
            + str(round(or_low, 2))
            + " | Gap "
            + format(gap, "+.2f")
            + "%"
        )
        if atr14_pct_j1:
            print("ATR14Pct J-1 : " + str(atr14_pct_j1))
        if dt:
            print(
                "Bougie decision "
                + dt
                + " | O "
                + str(op)
                + " | H "
                + str(hi)
                + " | L "
                + str(lo)
                + " | C "
                + str(cl)
                + " | Volume "
                + str(vol)
                + " | VWAP "
                + str(vwap_value)
            )
    except Exception as e:
        print("\n" + t + " | ERREUR : " + str(e))

f = "intraday_decisions.csv"
fields = [
    "ticker",
    "signal_date",
    "session_date",
    "decision_time",
    "signal_price",
    "atr14_pct_j1",
    "opening_price",
    "gap_pct",
    "opening_range_high",
    "opening_range_low",
    "close_bougie_4",
    "red_candles_4",
    "initial_avg_volume",
    "decision_open",
    "decision_high",
    "decision_low",
    "decision_close",
    "decision_volume",
    "vwap",
    "decision",
    "next_state",
    "reason",
]
old = []
if os.path.exists(f):
    old = list(csv.DictReader(open(f, encoding="utf-8")))
records = {(r["ticker"], r["signal_date"], r["session_date"]): r for r in old}
for r in decisions:
    records[(r["ticker"], r["signal_date"], r["session_date"])] = r
with open(f, "w", newline="", encoding="utf-8") as q:
    w = csv.DictWriter(q, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(records.values())
print("\nJournal intraday sauvegarde :", f)

now_paris = datetime.now(ZoneInfo("Europe/Paris")).isoformat(timespec="seconds")
with open("monitor_health.csv", "w", newline="", encoding="utf-8") as health_file:
    w = csv.DictWriter(health_file, fieldnames=["last_success_paris", "workflow_status"])
    w.writeheader()
    w.writerow({"last_success_paris": now_paris, "workflow_status": "OK"})
print("MONITOR HEALTH |", now_paris, "| OK")
