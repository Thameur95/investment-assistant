#!/usr/bin/env python3
"""Scanner J-1 V1 avec ATR14Pct.

La logique de score et de signal V1 reste inchangée.
Ajoute uniquement ATR14Pct calcule selon la definition du Historical Lab
et migre signals_history.csv sans perdre l'historique existant.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
STOCKS_FILE = ROOT / "stocks.txt"
HISTORY_FILE = ROOT / "signals_history.csv"
HISTORY_FIELDS = [
    "date", "ticker", "price", "score", "signal",
    "var5", "rsi14", "rvol", "atr14_pct",
]


def calculate_atr14_pct(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Meme definition ATR14 que replay_intraday_monitor.py."""
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14 = true_range.rolling(14, min_periods=14).mean()
    return atr14 / close * 100.0


def load_existing_history() -> tuple[list[dict[str, str]], set[tuple[str, str]]]:
    if not HISTORY_FILE.exists() or HISTORY_FILE.stat().st_size == 0:
        return [], set()

    with HISTORY_FILE.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    migrated = []
    existing = set()
    for row in rows:
        date = str(row.get("date", "")).strip()
        ticker = str(row.get("ticker", "")).strip()
        if date and ticker:
            existing.add((date, ticker))
        migrated.append({field: row.get(field, "") for field in HISTORY_FIELDS})
    return migrated, existing


def write_history(rows: list[dict[str, object]]) -> None:
    with HISTORY_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    tickers = [
        line.strip()
        for line in STOCKS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not tickers:
        print("Aucun ticker dans stocks.txt")
        return 0

    data = yf.download(
        tickers,
        period="3mo",
        progress=False,
        auto_adjust=True,
    )

    close_all = data["Close"]
    high_all = data["High"]
    low_all = data["Low"]
    volume_all = data["Volume"]

    ranking = []

    for ticker in tickers:
        timezone = "Europe/Paris" if ticker.endswith((".PA", ".AS")) else "America/New_York"
        today = datetime.now(ZoneInfo(timezone)).date()

        close = close_all[ticker].dropna()
        close = close[[timestamp.date() < today for timestamp in close.index]]
        if close.empty:
            continue

        high = high_all[ticker].reindex(close.index)
        low = low_all[ticker].reindex(close.index)
        volume = volume_all[ticker].reindex(close.index)

        market = pd.DataFrame(
            {"Close": close, "High": high, "Low": low, "Volume": volume}
        ).dropna()

        if len(market) < 21:
            continue

        close = market["Close"]
        high = market["High"]
        low = market["Low"]
        volume = market["Volume"]

        date = close.index[-1].strftime("%Y-%m-%d")
        price = float(close.iloc[-1])
        var5 = (price / float(close.iloc[-6]) - 1.0) * 100.0
        mm20 = float(close.iloc[-20:].mean())
        rvol = float(volume.iloc[-1]) / float(volume.iloc[-21:-1].mean())

        delta = close.diff().iloc[-14:]
        gain = float(delta.clip(lower=0).mean())
        loss = float((-delta.clip(upper=0)).mean())
        rsi14 = 100.0 if loss == 0 else 100.0 - (100.0 / (1.0 + gain / loss))

        atr14_pct_series = calculate_atr14_pct(high, low, close)
        atr14_pct = float(atr14_pct_series.iloc[-1]) if pd.notna(atr14_pct_series.iloc[-1]) else float("nan")

        # Logique V1 d'origine conservee strictement.
        score = (
            (35 if price > mm20 else -20)
            + (25 if 3 < var5 < 8 else 10 if 0 <= var5 <= 3 else -25 if var5 <= -3 else 0)
            + (20 if rvol >= 1.5 else 10 if rvol >= 1 else 0)
            + (20 if 50 <= rsi14 <= 65 else 10 if 45 <= rsi14 < 75 else -30)
        )
        score = max(0, min(100, score))

        signal = (
            "SURCHAUFFE" if rsi14 >= 75 and var5 > 3
            else "SURVEILLANCE HAUTE" if rsi14 >= 75
            else "FAIBLESSE FORTE" if var5 <= -5
            else "ACHAT" if price > mm20 and 3 < var5 < 8 and 50 <= rsi14 < 70
            else "SURVEILLANCE" if price > mm20 and rsi14 >= 40
            else "AUCUN SIGNAL"
        )

        ranking.append(
            (ticker, score, signal, var5, rsi14, rvol, atr14_pct, price, date)
        )

    ranking.sort(key=lambda row: row[1], reverse=True)

    print("CLASSEMENT AJUSTE AU RISQUE")
    for index, row in enumerate(ranking, 1):
        ticker, score, signal, var5, rsi14, rvol, atr14_pct, price, date = row
        atr_text = f"{atr14_pct:.2f}%" if pd.notna(atr14_pct) else "N/A"
        print(
            "{}. {} | Date {} | Cloture {:.2f} | Score {}/100 | {} | "
            "Var5j {:+.2f}% | RSI {:.1f} | RVOL {:.2f} | ATR14Pct {}".format(
                index, ticker, date, price, score, signal, var5, rsi14, rvol, atr_text
            )
        )

    old_rows, existing = load_existing_history()
    new_rows = []

    for row in ranking:
        ticker, score, signal, var5, rsi14, rvol, atr14_pct, price, date = row
        if (date, ticker) in existing:
            continue
        new_rows.append(
            {
                "date": date,
                "ticker": ticker,
                "price": round(price, 2),
                "score": score,
                "signal": signal,
                "var5": round(var5, 2),
                "rsi14": round(rsi14, 1),
                "rvol": round(rvol, 2),
                "atr14_pct": round(atr14_pct, 4) if pd.notna(atr14_pct) else "",
            }
        )

    # Reecriture volontaire: migre l'ancien schema 8 colonnes vers 9 colonnes.
    write_history(old_rows + new_rows)

    print("Historique mis a jour :", HISTORY_FILE.name)
    print("Anciennes lignes conservees :", len(old_rows))
    print("Nouvelles lignes ajoutees :", len(new_rows))
    print("Schema :", ",".join(HISTORY_FIELDS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
