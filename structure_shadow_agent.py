#!/usr/bin/env python3
"""
structure_shadow_agent.py - Agent Structure Shadow V1

Observe en permanence toute la watchlist, qu'un titre soit candidat V1 ou non.
Cette première brique crée la mémoire Structure et fige les signaux Shadow.
Elle ne passe aucun ordre et ne modifie aucun fichier V1.

Entrées lues uniquement :
  stocks.txt ou historical_lab/data/tickers.txt
  intraday_decisions.csv (optionnel, pour comparaison V1)

Sorties propres à l'agent :
  historical_lab/results/structure_observations.csv
  historical_lab/results/structure_shadow_state.csv
  historical_lab/results/structure_shadow_signals.csv
  historical_lab/results/structure_shadow_latest.json
  historical_lab/results/structure_shadow_summary.json

Garde-fous :
  ObservationOnly = True
  V1ExecutionChanged = False
  PortfolioV1Changed = False
  CatalystDecisionPower = False
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "historical_lab" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

OBSERVATIONS_FILE = RESULTS_DIR / "structure_observations.csv"
STATE_FILE = RESULTS_DIR / "structure_shadow_state.csv"
SIGNALS_FILE = RESULTS_DIR / "structure_shadow_signals.csv"
LATEST_FILE = RESULTS_DIR / "structure_shadow_latest.json"
SUMMARY_FILE = RESULTS_DIR / "structure_shadow_summary.json"
DECISIONS_FILE = ROOT / "intraday_decisions.csv"

INTERVAL = "5m"
PERIOD = "5d"
ROLLING_BARS = 12
BREAKOUT_BUFFER_PCT = 0.10
VOLUME_MULTIPLIER = 1.50
PULLBACK_TOLERANCE_PCT = 0.35
PULLBACK_INVALIDATION_PCT = 0.50
MIN_RR = 2.0
TARGET_PCT = 2.0
OBSERVATION_MINUTES = 5

OBS_FIELDS = [
    "ObservationID", "ObservedAtUTC", "Ticker", "Market", "SessionDate",
    "BarTime", "LastPrice", "SessionOpen", "SessionHigh", "SessionLow",
    "SessionVWAP", "Support12", "Resistance12", "DistanceSupportPct",
    "DistanceResistancePct", "MedianVolume12", "LastVolume", "VolumeRatio",
    "ConsolidationRangePct", "Breakout", "VolumeConfirmed", "PullbackCandidate",
    "StructureState", "V1Status", "V1Decision", "V1Reason", "V1DecisionTime",
    "ObservationOnly", "V1ExecutionChanged", "PortfolioV1Changed",
    "CatalystDecisionPower"
]

STATE_FIELDS = [
    "Ticker", "Market", "SessionDate", "LastObservedAtUTC", "LastBarTime",
    "StructureState", "PreviousStructureState", "LastPrice", "Support12",
    "Resistance12", "VolumeRatio", "Breakout", "VolumeConfirmed",
    "PullbackCandidate", "BreakoutLevel", "BreakoutTime", "InvalidationLevel",
    "V1Status", "V1Decision", "V1Reason", "ObservationOnly",
    "V1ExecutionChanged", "PortfolioV1Changed", "CatalystDecisionPower"
]

SIGNAL_FIELDS = [
    "SignalID", "Ticker", "Market", "SignalTime", "SessionDate", "SignalType",
    "StructureState", "BreakoutLevel", "ReferencePrice", "StopPrice",
    "TargetPrice", "RiskReward", "VolumeRatio", "V1Status", "V1Decision",
    "V1Reason", "SignalFrozenBeforeOutcome", "ObservationOnly",
    "V1ExecutionChanged", "PortfolioV1Changed", "CatalystDecisionPower"
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def num(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def fmt(value: Optional[float], digits: int = 4) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def pct(value: float, reference: float) -> float:
    return (value / reference - 1.0) * 100.0 if reference else 0.0


def market_of(ticker: str) -> str:
    return "EUROPE" if ticker.endswith((".PA", ".AS")) else "US"


def ticker_file() -> Path:
    candidates = [ROOT / "stocks.txt", ROOT / "historical_lab" / "data" / "tickers.txt"]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError("stocks.txt et historical_lab/data/tickers.txt introuvables")


def read_tickers() -> List[str]:
    path = ticker_file()
    values = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        ticker = line.strip()
        if ticker and ticker not in seen:
            values.append(ticker)
            seen.add(ticker)
    return values


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv_rows(path: Path, fields: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def append_unique(path: Path, fields: List[str], rows: List[Dict[str, Any]], key: str) -> int:
    old = read_csv_rows(path)
    known = {clean(row.get(key)) for row in old}
    fresh = [row for row in rows if clean(row.get(key)) not in known]
    write_csv_rows(path, fields, old + fresh)
    return len(fresh)


def load_v1_context() -> Dict[str, Dict[str, str]]:
    rows = read_csv_rows(DECISIONS_FILE)
    latest: Dict[str, Dict[str, str]] = {}
    for row in rows:
        ticker = clean(row.get("ticker"))
        ordering = (clean(row.get("session_date")), clean(row.get("decision_time")))
        prior = latest.get(ticker)
        prior_ordering = (
            clean(prior.get("session_date")), clean(prior.get("decision_time"))
        ) if prior else ("", "")
        if ticker and ordering >= prior_ordering:
            latest[ticker] = row
    return latest


def download_bars(ticker: str) -> pd.DataFrame:
    data = yf.download(
        ticker, period=PERIOD, interval=INTERVAL, progress=False,
        auto_adjust=True, prepost=False, threads=False
    )
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.reset_index()
    first = data.columns[0]
    data = data.rename(columns={first: "Datetime"})
    data["Datetime"] = pd.to_datetime(data["Datetime"], utc=True, errors="coerce")
    data = data.dropna(subset=["Datetime"]).copy()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["Open", "High", "Low", "Close", "Volume"])


def latest_session(data: pd.DataFrame, ticker: str) -> Tuple[pd.DataFrame, str]:
    zone = "Europe/Paris" if market_of(ticker) == "EUROPE" else "America/New_York"
    local = data["Datetime"].dt.tz_convert(zone)
    data = data.copy()
    data["LocalDatetime"] = local
    data["LocalDate"] = local.dt.strftime("%Y-%m-%d")
    session_date = clean(data.iloc[-1]["LocalDate"])
    session = data[data["LocalDate"] == session_date].copy().reset_index(drop=True)
    return session, session_date


def structure_metrics(session: pd.DataFrame) -> Dict[str, Any]:
    if len(session) < ROLLING_BARS + 1:
        return {"state": "DONNEES_INSUFFISANTES"}

    current = session.iloc[-1]
    prior = session.iloc[-(ROLLING_BARS + 1):-1]
    last_price = float(current["Close"])
    support = float(prior["Low"].min())
    resistance = float(prior["High"].max())
    median_volume = float(prior["Volume"].median())
    volume = float(current["Volume"])
    volume_ratio = volume / median_volume if median_volume > 0 else 0.0
    breakout = last_price > resistance * (1 + BREAKOUT_BUFFER_PCT / 100.0)
    volume_confirmed = volume_ratio >= VOLUME_MULTIPLIER
    consolidation_range = pct(float(prior["High"].max()), float(prior["Low"].min()))

    typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
    session_vwap = float((typical * session["Volume"]).sum() / session["Volume"].sum()) if session["Volume"].sum() > 0 else last_price

    tolerance_low = resistance * (1 - PULLBACK_TOLERANCE_PCT / 100.0)
    tolerance_high = resistance * (1 + PULLBACK_TOLERANCE_PCT / 100.0)
    pullback_candidate = (
        not breakout
        and float(current["Low"]) <= tolerance_high
        and float(current["High"]) >= tolerance_low
        and last_price >= resistance
    )

    if breakout and volume_confirmed:
        state = "BREAKOUT_CONFIRME"
    elif breakout:
        state = "BREAKOUT_NON_CONFIRME"
    elif pullback_candidate and volume_confirmed:
        state = "SIGNAL_STRUCTURE_SHADOW"
    elif pullback_candidate:
        state = "PULLBACK_EN_COURS"
    elif last_price < support * (1 - PULLBACK_INVALIDATION_PCT / 100.0):
        state = "SUPPORT_CASSE"
    elif abs(pct(last_price, resistance)) <= 0.50:
        state = "PROXIMITE_RESISTANCE"
    elif consolidation_range <= 2.0:
        state = "CONSOLIDATION"
    else:
        state = "AUCUNE_STRUCTURE"

    return {
        "state": state,
        "last_price": last_price,
        "session_open": float(session.iloc[0]["Open"]),
        "session_high": float(session["High"].max()),
        "session_low": float(session["Low"].min()),
        "session_vwap": session_vwap,
        "support": support,
        "resistance": resistance,
        "distance_support": pct(last_price, support),
        "distance_resistance": pct(last_price, resistance),
        "median_volume": median_volume,
        "last_volume": volume,
        "volume_ratio": volume_ratio,
        "consolidation_range": consolidation_range,
        "breakout": breakout,
        "volume_confirmed": volume_confirmed,
        "pullback_candidate": pullback_candidate,
        "bar_time": session.iloc[-1]["LocalDatetime"].isoformat(),
    }


def v1_status(context: Optional[Dict[str, str]]) -> Tuple[str, str, str, str]:
    if not context:
        return "NON_CANDIDAT", "", "", ""
    decision = clean(context.get("decision"))
    if decision in {"ACHAT CONFIRME", "ACHAT REACTIVE", "ACHAT RÉACTIF"}:
        status = "ACHAT_V1"
    elif decision:
        status = "REJET_V1"
    else:
        status = "EN_SURVEILLANCE"
    return status, decision, clean(context.get("reason")), clean(context.get("decision_time"))


def main() -> int:
    started = datetime.now(timezone.utc)
    observed_at = started.isoformat()
    tickers = read_tickers()
    contexts = load_v1_context()
    previous_states = {row["Ticker"]: row for row in read_csv_rows(STATE_FILE)}

    observations: List[Dict[str, Any]] = []
    states: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    print("=" * 96)
    print("STRUCTURE SHADOW AGENT - OBSERVATION PERMANENTE DE LA WATCHLIST")
    print("=" * 96)
    print(f"Tickers à observer : {len(tickers)}")

    for ticker in tickers:
        try:
            bars = download_bars(ticker)
            if bars.empty:
                raise RuntimeError("Aucune donnée intraday reçue")
            session, session_date = latest_session(bars, ticker)
            metrics = structure_metrics(session)
            status, decision, reason, decision_time = v1_status(contexts.get(ticker))
            previous = previous_states.get(ticker, {})
            prior_state = clean(previous.get("StructureState"))
            state = clean(metrics.get("state"))
            bar_time = clean(metrics.get("bar_time"))
            observation_id = f"{ticker}|{bar_time}|{state}"

            observation = {
                "ObservationID": observation_id,
                "ObservedAtUTC": observed_at,
                "Ticker": ticker,
                "Market": market_of(ticker),
                "SessionDate": session_date,
                "BarTime": bar_time,
                "LastPrice": fmt(num(metrics.get("last_price"))),
                "SessionOpen": fmt(num(metrics.get("session_open"))),
                "SessionHigh": fmt(num(metrics.get("session_high"))),
                "SessionLow": fmt(num(metrics.get("session_low"))),
                "SessionVWAP": fmt(num(metrics.get("session_vwap"))),
                "Support12": fmt(num(metrics.get("support"))),
                "Resistance12": fmt(num(metrics.get("resistance"))),
                "DistanceSupportPct": fmt(num(metrics.get("distance_support"))),
                "DistanceResistancePct": fmt(num(metrics.get("distance_resistance"))),
                "MedianVolume12": fmt(num(metrics.get("median_volume")), 0),
                "LastVolume": fmt(num(metrics.get("last_volume")), 0),
                "VolumeRatio": fmt(num(metrics.get("volume_ratio"))),
                "ConsolidationRangePct": fmt(num(metrics.get("consolidation_range"))),
                "Breakout": str(bool(metrics.get("breakout", False))),
                "VolumeConfirmed": str(bool(metrics.get("volume_confirmed", False))),
                "PullbackCandidate": str(bool(metrics.get("pullback_candidate", False))),
                "StructureState": state,
                "V1Status": status,
                "V1Decision": decision,
                "V1Reason": reason,
                "V1DecisionTime": decision_time,
                "ObservationOnly": "True",
                "V1ExecutionChanged": "False",
                "PortfolioV1Changed": "False",
                "CatalystDecisionPower": "False",
            }
            observations.append(observation)

            breakout_level = num(metrics.get("resistance"))
            invalidation = breakout_level * (1 - PULLBACK_INVALIDATION_PCT / 100.0) if breakout_level else None
            state_row = {
                "Ticker": ticker,
                "Market": market_of(ticker),
                "SessionDate": session_date,
                "LastObservedAtUTC": observed_at,
                "LastBarTime": bar_time,
                "StructureState": state,
                "PreviousStructureState": prior_state,
                "LastPrice": observation["LastPrice"],
                "Support12": observation["Support12"],
                "Resistance12": observation["Resistance12"],
                "VolumeRatio": observation["VolumeRatio"],
                "Breakout": observation["Breakout"],
                "VolumeConfirmed": observation["VolumeConfirmed"],
                "PullbackCandidate": observation["PullbackCandidate"],
                "BreakoutLevel": fmt(breakout_level),
                "BreakoutTime": bar_time if bool(metrics.get("breakout")) else clean(previous.get("BreakoutTime")),
                "InvalidationLevel": fmt(invalidation),
                "V1Status": status,
                "V1Decision": decision,
                "V1Reason": reason,
                "ObservationOnly": "True",
                "V1ExecutionChanged": "False",
                "PortfolioV1Changed": "False",
                "CatalystDecisionPower": "False",
            }
            states.append(state_row)

            # Un signal n'est figé que sur l'état explicite SIGNAL_STRUCTURE_SHADOW.
            if state == "SIGNAL_STRUCTURE_SHADOW" and prior_state != state:
                entry = num(metrics.get("last_price"))
                stop = invalidation
                target = entry * (1 + TARGET_PCT / 100.0) if entry else None
                risk = entry - stop if entry is not None and stop is not None else None
                rr = (target - entry) / risk if risk and risk > 0 else None
                if rr is not None and rr >= MIN_RR:
                    signal_id = f"{ticker}|{bar_time}|SIGNAL_STRUCTURE_SHADOW"
                    signals.append({
                        "SignalID": signal_id,
                        "Ticker": ticker,
                        "Market": market_of(ticker),
                        "SignalTime": bar_time,
                        "SessionDate": session_date,
                        "SignalType": "SIGNAL_STRUCTURE_SHADOW",
                        "StructureState": state,
                        "BreakoutLevel": fmt(breakout_level),
                        "ReferencePrice": fmt(entry),
                        "StopPrice": fmt(stop),
                        "TargetPrice": fmt(target),
                        "RiskReward": fmt(rr),
                        "VolumeRatio": observation["VolumeRatio"],
                        "V1Status": status,
                        "V1Decision": decision,
                        "V1Reason": reason,
                        "SignalFrozenBeforeOutcome": "True",
                        "ObservationOnly": "True",
                        "V1ExecutionChanged": "False",
                        "PortfolioV1Changed": "False",
                        "CatalystDecisionPower": "False",
                    })

            print(f"{ticker:<8} | {status:<13} | {state:<27} | prix {observation['LastPrice']} | vol x{observation['VolumeRatio']}")

        except Exception as error:
            failures.append({"ticker": ticker, "error": str(error)})
            print(f"{ticker:<8} | ERREUR | {error}")

    new_observations = append_unique(OBSERVATIONS_FILE, OBS_FIELDS, observations, "ObservationID")
    write_csv_rows(STATE_FILE, STATE_FIELDS, states)
    new_signals = append_unique(SIGNALS_FILE, SIGNAL_FIELDS, signals, "SignalID")

    latest_payload = {
        "GeneratedAtUTC": observed_at,
        "Agent": "StructureShadow",
        "WatchlistSize": len(tickers),
        "Observed": len(observations),
        "Failures": failures,
        "States": states,
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
        "CatalystDecisionPower": False,
    }
    LATEST_FILE.write_text(json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    counts: Dict[str, int] = {}
    for row in states:
        value = clean(row.get("StructureState"))
        counts[value] = counts.get(value, 0) + 1
    summary = {
        "GeneratedAtUTC": observed_at,
        "WatchlistSize": len(tickers),
        "Observed": len(observations),
        "Failed": len(failures),
        "NewObservations": new_observations,
        "NewSignals": new_signals,
        "StateCounts": counts,
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
        "CatalystDecisionPower": False,
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 96)
    print(f"Observés : {len(observations)}/{len(tickers)} | erreurs : {len(failures)}")
    print(f"Nouvelles observations : {new_observations} | nouveaux signaux : {new_signals}")
    print(f"État : {STATE_FILE}")
    print(f"Mémoire : {OBSERVATIONS_FILE}")
    print(f"Signaux : {SIGNALS_FILE}")
    print("ObservationOnly : True")
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")
    print("CatalystDecisionPower : False")
    print("=" * 96)

    # Une erreur isolée ne bloque pas V1. Un échec total, oui, car l'agent est inutilisable.
    return 1 if len(observations) == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
