#!/usr/bin/env python3
"""Diagnostic en lecture seule du replay TSLA pour les sessions 27/10 et 29/10/2026.

Ne modifie aucun CSV et aucune regle V1. Le script explique a quel controle
le replay perd chaque entree TSLA attendue.
"""
from pathlib import Path
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
if not (BASE_DIR / "data").exists():
    BASE_DIR = Path(__file__).resolve().parent.parent

DAILY_FILE = BASE_DIR / "data" / "processed" / "historical_indicators.csv"
INTRADAY_FILE = BASE_DIR / "data" / "oos_v2" / "prospective" / "intraday" / "TSLA_5m.csv"
TARGETS = {
    "2026-10-27": "2026-10-26",
    "2026-10-29": "2026-10-28",
}
ENTRY_BARS = 12


def add_vwap(day: pd.DataFrame) -> pd.DataFrame:
    x = day.copy()
    typical = (x["High"] + x["Low"] + x["Close"]) / 3.0
    cv = x["Volume"].cumsum()
    x["VWAP"] = (typical * x["Volume"]).cumsum() / cv.replace(0, np.nan)
    return x


def parse_intraday(path: Path) -> pd.DataFrame:
    x = pd.read_csv(path)
    required = {"Datetime_Paris", "Date_Paris", "Open", "High", "Low", "Close", "Volume"}
    missing = required - set(x.columns)
    if missing:
        raise ValueError("Colonnes intraday manquantes: " + ", ".join(sorted(missing)))

    raw = x["Datetime_Paris"].astype(str).str.strip()
    parsed = pd.to_datetime(raw, errors="coerce", utc=True)
    x["Datetime_Paris"] = parsed.dt.tz_convert("Europe/Paris")
    x["Date_Paris"] = x["Date_Paris"].astype(str).str[:10]
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.dropna(subset=list(required)).sort_values("Datetime_Paris").reset_index(drop=True)


def diagnose_session(day: pd.DataFrame, signal_price: float) -> None:
    day = add_vwap(day.sort_values("Datetime_Paris").reset_index(drop=True))
    print("Bougies:", len(day))
    if len(day) < 4:
        print("RESULTAT: ECHEC_MOINS_DE_4_BOUGIES")
        return

    opening = float(day["Open"].iloc[0])
    gap = (opening / signal_price - 1.0) * 100.0
    first4 = day.iloc[:4]
    or_high = float(first4["High"].max())
    or_low = float(first4["Low"].min())
    close4 = float(first4["Close"].iloc[-1])
    red4 = int((first4["Close"] < first4["Open"]).sum())
    vols = first4.loc[first4["Volume"] > 0, "Volume"]
    vol4 = float(vols.mean()) if not vols.empty else 0.0

    print("Premiere bougie Paris:", day["Datetime_Paris"].iloc[0])
    print("Derniere bougie Paris:", day["Datetime_Paris"].iloc[-1])
    print("Opening:", round(opening, 6))
    print("GapPct:", round(gap, 6))
    print("ORHigh:", round(or_high, 6))
    print("ORLow:", round(or_low, 6))
    print("Close4:", round(close4, 6))
    print("RedCandles4:", red4)
    print("InitialAvgVolume:", round(vol4, 2))
    print("ReactiveVolumeThreshold:", round(vol4 * 1.5, 2))

    decision = "EN ATTENTE"
    reason = "Prix dans OR"
    invalid_pos = 3

    if gap <= -3:
        decision, reason = "INVALIDE", "Gap baissier >= 3%"
    elif close4 <= opening * 0.98:
        decision, reason = "INVALIDE", "Baisse >= 2% apres 20 minutes"
    elif red4 >= 3 and close4 < opening:
        decision, reason = "INVALIDE", "3 bougies rouges sur 4"
    else:
        for pos, row in day.iloc[:ENTRY_BARS].iloc[4:].iterrows():
            low, close, volume, vwap = float(row["Low"]), float(row["Close"]), float(row["Volume"]), row["VWAP"]
            if pd.isna(vwap):
                continue
            if low < or_low or close <= opening * 0.98:
                decision, reason, invalid_pos = "INVALIDE", "Cassure OR Low", int(pos)
                break
            if close > or_high and close > float(vwap) and volume >= vol4:
                print("RESULTAT: ACHAT CONFIRME", row["Datetime_Paris"], close, volume, float(vwap))
                return
        if decision == "EN ATTENTE" and len(day) >= ENTRY_BARS:
            decision, reason, invalid_pos = "INVALIDE", "Pas de confirmation en 60 min", ENTRY_BARS - 1

    print("Etat initial:", decision, "|", reason, "| invalid_pos:", invalid_pos)

    candidates = []
    for pos, row in day.iloc[invalid_pos + 1:].iterrows():
        close, volume, vwap = float(row["Close"]), float(row["Volume"]), row["VWAP"]
        if pd.isna(vwap):
            continue
        price_ok = close > or_high
        vwap_ok = close > float(vwap)
        volume_ok = volume >= vol4 * 1.5
        if price_ok and vwap_ok:
            candidates.append((row["Datetime_Paris"], close, volume, float(vwap), volume_ok))
        if price_ok and vwap_ok and volume_ok:
            print("RESULTAT: ACHAT REACTIVE", row["Datetime_Paris"], close, volume, float(vwap))
            return

    if candidates:
        print("RESULTAT: PAS_ENTREE_VOLUME")
        print("Derniers passages ORHigh+VWAP:")
        for item in candidates[-5:]:
            print(item)
    else:
        max_close = float(day["Close"].max())
        print("RESULTAT: PAS_REPRISE_ORHIGH_ET_VWAP")
        print("MaxClose:", round(max_close, 6), "| ORHigh:", round(or_high, 6))


def main() -> int:
    print("=" * 100)
    print("DIAGNOSTIC REPLAY TSLA - LECTURE SEULE")
    print("Daily:", DAILY_FILE)
    print("Intraday:", INTRADAY_FILE)
    print("=" * 100)

    daily = pd.read_csv(DAILY_FILE)
    daily["Date"] = pd.to_datetime(daily["Date"], errors="coerce")
    daily["Ticker"] = daily["Ticker"].astype(str).str.strip().str.upper()
    intraday = parse_intraday(INTRADAY_FILE)

    for session_date, signal_date in TARGETS.items():
        print("\n" + "=" * 100)
        print("SESSION", session_date, "| SIGNAL", signal_date)
        signal = daily.loc[(daily["Ticker"] == "TSLA") & (daily["Date"].dt.strftime("%Y-%m-%d") == signal_date)]
        if signal.empty:
            print("RESULTAT: SIGNAL_DAILY_ABSENT")
            continue
        row = signal.iloc[-1]
        print("Signal:", row.get("Signal"), "| Score:", row.get("Score"), "| Close J-1:", row.get("Close"))
        if str(row.get("Signal")).strip().upper() != "ACHAT":
            print("RESULTAT: SIGNAL_DAILY_NON_ACHAT")
            continue
        day = intraday.loc[intraday["Date_Paris"] == session_date].copy()
        if day.empty:
            print("RESULTAT: SESSION_INTRADAY_ABSENTE")
            continue
        diagnose_session(day, float(row["Close"]))

    print("\nAucun fichier n'a ete modifie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
