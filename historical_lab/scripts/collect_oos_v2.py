#!/usr/bin/env python3
"""Collecte prospective OOS en bougies 5 minutes et cache cumulatif.

La collecte utilise la liste des tickers de historical_indicators.csv, telecharge
la fenetre intraday disponible, convertit les timestamps en Europe/Paris puis
fusionne les nouvelles bougies avec le cache existant sans doublons.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

PARIS_TZ = "Europe/Paris"
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--daily-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--period", default="60d")
    parser.add_argument("--retries", type=int, default=3)
    return parser.parse_args()


def normalize_download(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()

    df = raw.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [str(col[0]) for col in df.columns]

    missing = set(PRICE_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError("Colonnes intraday absentes: " + ", ".join(sorted(missing)))

    idx = pd.to_datetime(df.index, errors="coerce", utc=True)
    valid = ~idx.isna()
    df = df.loc[valid, PRICE_COLUMNS].copy()
    idx = idx[valid].tz_convert(PARIS_TZ)

    df.insert(0, "Datetime_Paris", idx.astype(str))
    df.insert(1, "Date_Paris", idx.strftime("%Y-%m-%d"))

    for col in PRICE_COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    return (
        df.dropna(subset=["Datetime_Paris", "Open", "High", "Low", "Close"])
        .drop_duplicates("Datetime_Paris", keep="last")
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )


def merge_cache(path: Path, fresh: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if path.exists():
        old = pd.read_csv(path)
        frames.append(old)
    if not fresh.empty:
        frames.append(fresh)
    if not frames:
        return pd.DataFrame(columns=["Datetime_Paris", "Date_Paris"] + PRICE_COLUMNS)

    merged = pd.concat(frames, ignore_index=True)
    merged["Datetime_Paris"] = pd.to_datetime(
        merged["Datetime_Paris"], errors="coerce", utc=True
    ).dt.tz_convert(PARIS_TZ)
    merged = merged.dropna(subset=["Datetime_Paris"])
    merged["Datetime_Paris"] = merged["Datetime_Paris"].astype(str)
    merged["Date_Paris"] = merged["Datetime_Paris"].str[:10]
    for col in PRICE_COLUMNS:
        merged[col] = pd.to_numeric(merged[col], errors="coerce")
    return (
        merged.drop_duplicates("Datetime_Paris", keep="last")
        .sort_values("Datetime_Paris")
        .reset_index(drop=True)
    )


def download_ticker(ticker: str, period: str, retries: int) -> pd.DataFrame:
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            raw = yf.download(
                ticker,
                period=period,
                interval="5m",
                auto_adjust=True,
                prepost=False,
                progress=False,
                threads=False,
                timeout=30,
                multi_level_index=False,
            )
            return normalize_download(raw)
        except Exception as exc:
            last_error = exc
            print(f"{ticker}: tentative {attempt}/{retries} echouee: {exc}")
            time.sleep(attempt * 2)
    raise RuntimeError(f"{ticker}: echec du telechargement: {last_error}")


def main():
    args = parse_args()
    daily_file = Path(args.daily_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not daily_file.exists():
        raise FileNotFoundError(daily_file)

    daily = pd.read_csv(daily_file)
    if "Ticker" not in daily.columns:
        raise ValueError("Colonne Ticker absente du fichier quotidien")

    tickers = sorted(daily["Ticker"].dropna().astype(str).unique())
    if not tickers:
        raise ValueError("Aucun ticker trouve")

    failures = []
    inventory = []
    print("Tickers a collecter:", len(tickers))

    for ticker in tickers:
        path = output_dir / f"{ticker}_5m.csv"
        try:
            fresh = download_ticker(ticker, args.period, args.retries)
            merged = merge_cache(path, fresh)
            merged.to_csv(path, index=False, encoding="utf-8")
            inventory.append({
                "Ticker": ticker,
                "FreshBars": len(fresh),
                "CachedBars": len(merged),
                "Start": merged["Datetime_Paris"].min() if len(merged) else None,
                "End": merged["Datetime_Paris"].max() if len(merged) else None,
            })
            print(f"{ticker}: {len(fresh)} nouvelles, {len(merged)} en cache")
        except Exception as exc:
            failures.append({"Ticker": ticker, "Error": str(exc)})
            print(f"{ticker}: ECHEC: {exc}")

    pd.DataFrame(inventory).to_csv(
        output_dir.parent / "collection_inventory.csv", index=False, encoding="utf-8"
    )
    pd.DataFrame(failures).to_csv(
        output_dir.parent / "collection_failures.csv", index=False, encoding="utf-8"
    )

    success_ratio = len(inventory) / len(tickers)
    print(f"Succes collecte: {len(inventory)}/{len(tickers)} ({success_ratio:.1%})")
    if success_ratio < 0.70:
        raise RuntimeError("Moins de 70% des tickers ont ete collectes")


if __name__ == "__main__":
    main()
