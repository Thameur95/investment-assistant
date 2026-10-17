#!/usr/bin/env python3
"""
structure_shadow_agent.py - Agent Structure Shadow V2 multi-horizon

Observe en permanence toute la watchlist, qu'un titre soit candidat V1 ou non.
Analyse multi-horizon : structure intraday locale, séance, Daily 20 et 60 séances.
Mémorise les breakouts entre les exécutions et fige les signaux Shadow avant résultat.

OBSERVATION UNIQUEMENT :
- ne modifie jamais V1 ;
- ne modifie jamais intraday_decisions.csv ;
- ne modifie jamais virtual_portfolio.csv ni portfolio_cash.csv ;
- le Catalyst reste informatif et sans pouvoir décisionnel ;
- aucun achat virtuel n'est exécuté par cette version.

Entrées lues :
- stocks.txt, sinon historical_lab/data/tickers.txt ;
- intraday_decisions.csv, uniquement pour comparaison V1 sur la même séance.

Sorties propres à l'agent :
- historical_lab/results/structure_observations.csv
- historical_lab/results/structure_shadow_state.csv
- historical_lab/results/structure_shadow_signals.csv
- historical_lab/results/structure_shadow_latest.json
- historical_lab/results/structure_shadow_summary.json
"""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "historical_lab" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)
DAILY_CACHE_DIR = ROOT / "historical_lab" / "data" / "structure_daily_cache"
DAILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

DECISIONS_FILE = ROOT / "intraday_decisions.csv"
OBSERVATIONS_FILE = RESULTS_DIR / "structure_observations.csv"
STATE_FILE = RESULTS_DIR / "structure_shadow_state.csv"
SIGNALS_FILE = RESULTS_DIR / "structure_shadow_signals.csv"
LATEST_FILE = RESULTS_DIR / "structure_shadow_latest.json"
SUMMARY_FILE = RESULTS_DIR / "structure_shadow_summary.json"

INTRADAY_PERIOD = "5d"
INTRADAY_INTERVAL = "5m"
DAILY_PERIOD = "6mo"
DAILY_INTERVAL = "1d"
LOCAL_BARS = 12
DAILY_SHORT_BARS = 20
DAILY_LONG_BARS = 60
BREAKOUT_BUFFER_PCT = 0.10
VOLUME_MULTIPLIER = 1.50
PULLBACK_TOLERANCE_PCT = 0.35
PULLBACK_INVALIDATION_PCT = 0.50
PULLBACK_MAX_BARS = 24
TARGET_PCT = 2.0
MIN_RR = 2.0
MAJOR_RESISTANCE_REQUIRED_ROOM_PCT = 2.20
BREAKOUT_EXPIRY_SESSIONS = 4
AGENT_VERSION = "V2_1_MULTI_HORIZON_CACHED"
SIGNAL_VALIDITY_POLICY = "NEXT_EXECUTABLE_BAR_ONLY"

# Bougies de clôture régulière : le volume reste enregistré mais ne confirme pas seul un breakout.
MARKET_CLOSE_TIMES = {
    "US": {"15:55:00"},
    "EUROPE": {"17:25:00"},
}

OBS_FIELDS = [
    "ObservationID", "ObservedAtUTC", "Ticker", "Market", "SessionDate", "BarTime",
    "LastPrice", "SessionOpen", "SessionHigh", "SessionLow", "SessionVWAP",
    "LocalSupport12", "LocalResistance12", "SessionSupport", "SessionResistance",
    "DailySupport20", "DailyResistance20", "DailySupport60", "DailyResistance60",
    "NextMajorResistance", "RoomToMajorResistancePct", "MajorResistanceTooClose",
    "DistanceLocalSupportPct", "DistanceLocalResistancePct", "MedianVolume12",
    "LastVolume", "VolumeRatio", "VolumeEligible", "VolumeConfirmed",
    "ConsolidationRangePct", "BreakoutLocal", "PullbackCandidate", "StructureState",
    "PersistedBreakoutLevel", "PersistedBreakoutTime", "PersistedBreakoutConfirmed",
    "BreakoutAgeSessions", "InvalidationLevel", "V1StatusCurrentSession",
    "V1LastKnownDecision", "V1LastKnownSession", "V1Reason", "V1DecisionTime",
    "V1ContextSessionAligned", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed", "CatalystDecisionPower", "DailyDataSource"
]

STATE_FIELDS = [
    "Ticker", "Market", "SessionDate", "LastObservedAtUTC", "LastBarTime",
    "StructureState", "PreviousStructureState", "LastPrice", "LocalSupport12",
    "LocalResistance12", "SessionSupport", "SessionResistance", "DailySupport20",
    "DailyResistance20", "DailySupport60", "DailyResistance60", "NextMajorResistance",
    "RoomToMajorResistancePct", "MajorResistanceTooClose", "VolumeRatio",
    "VolumeEligible", "VolumeConfirmed", "BreakoutLocal", "PullbackCandidate",
    "PersistedBreakoutLevel", "PersistedBreakoutTime", "PersistedBreakoutConfirmed",
    "BreakoutAgeSessions", "InvalidationLevel", "V1StatusCurrentSession",
    "V1LastKnownDecision", "V1LastKnownSession", "V1Reason", "V1ContextSessionAligned",
    "ObservationOnly", "V1ExecutionChanged", "PortfolioV1Changed", "CatalystDecisionPower",
    "DailyDataSource"
]

SIGNAL_FIELDS = [
    "SignalID", "AgentVersion", "SignalValidityPolicy", "Ticker", "Market", "SignalTime", "SessionDate", "SignalType",
    "StructureState", "BreakoutLevel", "ReferencePrice", "StopPrice", "TargetPrice",
    "RiskReward", "VolumeRatio", "NextMajorResistance", "RoomToMajorResistancePct",
    "V1StatusCurrentSession", "V1LastKnownDecision", "V1LastKnownSession", "V1Reason",
    "SignalFrozenBeforeOutcome", "ObservationOnly", "V1ExecutionChanged",
    "PortfolioV1Changed", "CatalystDecisionPower"
]


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def number(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def format_number(value: Optional[float], digits: int = 4) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def percentage(value: float, reference: float) -> float:
    return (value / reference - 1.0) * 100.0 if reference else 0.0


def market_of(ticker: str) -> str:
    return "EUROPE" if ticker.endswith((".PA", ".AS")) else "US"


def timezone_of(ticker: str) -> str:
    return "Europe/Paris" if market_of(ticker) == "EUROPE" else "America/New_York"


def ticker_file() -> Path:
    for candidate in (ROOT / "stocks.txt", ROOT / "historical_lab" / "data" / "tickers.txt"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError("stocks.txt et historical_lab/data/tickers.txt introuvables")


def read_tickers() -> List[str]:
    seen = set()
    tickers: List[str] = []
    for line in ticker_file().read_text(encoding="utf-8").splitlines():
        ticker = line.strip()
        if ticker and ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    return tickers


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
    existing = read_csv_rows(path)
    known = {clean(row.get(key)) for row in existing}
    fresh = [row for row in rows if clean(row.get(key)) not in known]
    write_csv_rows(path, fields, existing + fresh)
    return len(fresh)


def load_v1_contexts() -> Tuple[Dict[Tuple[str, str], Dict[str, str]], Dict[str, Dict[str, str]]]:
    by_session: Dict[Tuple[str, str], Dict[str, str]] = {}
    latest: Dict[str, Dict[str, str]] = {}
    for row in read_csv_rows(DECISIONS_FILE):
        ticker = clean(row.get("ticker"))
        session = clean(row.get("session_date"))[:10]
        if not ticker or not session:
            continue
        by_session[(ticker, session)] = row
        previous = latest.get(ticker)
        if previous is None or (session, clean(row.get("decision_time"))) >= (
            clean(previous.get("session_date")), clean(previous.get("decision_time"))
        ):
            latest[ticker] = row
    return by_session, latest


def normalize_download(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data = data.reset_index()
    data = data.rename(columns={data.columns[0]: "Datetime"})
    data["Datetime"] = pd.to_datetime(data["Datetime"], utc=True, errors="coerce")
    data = data.dropna(subset=["Datetime"]).copy()
    for column in ("Open", "High", "Low", "Close", "Volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["Open", "High", "Low", "Close", "Volume"])


def download_intraday(ticker: str) -> pd.DataFrame:
    return normalize_download(yf.download(
        ticker, period=INTRADAY_PERIOD, interval=INTRADAY_INTERVAL,
        progress=False, auto_adjust=True, prepost=False, threads=False
    ))


def raw_daily_path(ticker: str) -> Path:
    return ROOT / "historical_lab" / "data" / "raw" / f"{ticker}.csv"


def cached_daily_path(ticker: str) -> Path:
    safe = ticker.replace("/", "_")
    return DAILY_CACHE_DIR / f"{safe}.csv"


def read_local_daily(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        data = pd.read_csv(path)
        date_column = "Date" if "Date" in data.columns else data.columns[0]
        data[date_column] = pd.to_datetime(data[date_column], errors="coerce")
        data = data.dropna(subset=[date_column]).set_index(date_column).sort_index()
        for column in ("Open", "High", "Low", "Close", "Volume"):
            if column in data.columns:
                data[column] = pd.to_numeric(data[column], errors="coerce")
        required = [column for column in ("High", "Low", "Close") if column in data.columns]
        return data.dropna(subset=required) if len(required) == 3 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def write_daily_cache(path: Path, data: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    output = data.copy()
    output.index.name = "Date"
    output.reset_index().to_csv(path, index=False, encoding="utf-8")


def download_daily_remote(ticker: str) -> pd.DataFrame:
    data = yf.download(
        ticker, period=DAILY_PERIOD, interval=DAILY_INTERVAL,
        progress=False, auto_adjust=True, prepost=False, threads=False
    )
    if data.empty:
        return data
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)
    data.index = pd.to_datetime(data.index, errors="coerce").tz_localize(None)
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["High", "Low", "Close"]).sort_index()


def download_daily(ticker: str, required_session_date: str) -> Tuple[pd.DataFrame, str]:
    """Utilise le cache local; un seul appel distant si la séance requise est absente."""
    cache_path = cached_daily_path(ticker)
    raw_path = raw_daily_path(ticker)

    cached = read_local_daily(cache_path)
    raw = read_local_daily(raw_path)
    local = cached if not cached.empty else raw

    required = pd.Timestamp(required_session_date).tz_localize(None)
    if not local.empty and pd.Timestamp(local.index.max()).tz_localize(None) >= required:
        source = "CACHE" if not cached.empty else "RAW"
        if cached.empty:
            write_daily_cache(cache_path, local)
        return local, source

    remote = download_daily_remote(ticker)
    if remote.empty:
        if not local.empty:
            return local, "LOCAL_STALE"
        return remote, "UNAVAILABLE"

    if not local.empty:
        combined = pd.concat([local, remote])
        combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    else:
        combined = remote
    write_daily_cache(cache_path, combined)
    return combined, "REMOTE_REFRESH"


def latest_session(data: pd.DataFrame, ticker: str) -> Tuple[pd.DataFrame, str]:
    local = data["Datetime"].dt.tz_convert(timezone_of(ticker))
    copy = data.copy()
    copy["LocalDatetime"] = local
    copy["LocalDate"] = local.dt.strftime("%Y-%m-%d")
    date = clean(copy.iloc[-1]["LocalDate"])
    return copy[copy["LocalDate"] == date].copy().reset_index(drop=True), date


def daily_levels(data: pd.DataFrame, current_price: float) -> Dict[str, Any]:
    if data.empty or len(data) < DAILY_SHORT_BARS:
        return {}
    short = data.iloc[-DAILY_SHORT_BARS:]
    long = data.iloc[-min(DAILY_LONG_BARS, len(data)):]
    support20 = float(short["Low"].min())
    resistance20 = float(short["High"].max())
    support60 = float(long["Low"].min())
    resistance60 = float(long["High"].max())
    candidates = sorted({level for level in (resistance20, resistance60) if level > current_price})
    next_major = candidates[0] if candidates else None
    room = percentage(next_major, current_price) if next_major else None
    return {
        "support20": support20,
        "resistance20": resistance20,
        "support60": support60,
        "resistance60": resistance60,
        "next_major": next_major,
        "room": room,
        "too_close": room is not None and room < MAJOR_RESISTANCE_REQUIRED_ROOM_PCT,
    }


def sessions_between(start_date: str, end_date: str) -> int:
    try:
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        return max(0, len(pd.bdate_range(start, end)) - 1)
    except Exception:
        return 999


def v1_status(current: Optional[Dict[str, str]], latest: Optional[Dict[str, str]]) -> Dict[str, str]:
    last_decision = clean(latest.get("decision")) if latest else ""
    last_session = clean(latest.get("session_date")) if latest else ""
    if current:
        decision = clean(current.get("decision"))
        if decision in {"ACHAT CONFIRME", "ACHAT REACTIVE", "ACHAT RÉACTIF"}:
            status = "ACHAT_V1"
        elif decision:
            status = "REJET_V1"
        else:
            status = "EN_SURVEILLANCE"
        return {
            "status": status,
            "last_decision": decision,
            "last_session": clean(current.get("session_date")),
            "reason": clean(current.get("reason")),
            "decision_time": clean(current.get("decision_time")),
            "aligned": "True",
        }
    return {
        "status": "NON_CANDIDAT",
        "last_decision": last_decision,
        "last_session": last_session,
        "reason": clean(latest.get("reason")) if latest else "",
        "decision_time": clean(latest.get("decision_time")) if latest else "",
        "aligned": "False",
    }


def calculate_structure(
    session: pd.DataFrame,
    daily: Dict[str, Any],
    previous: Dict[str, str],
    session_date: str,
    ticker: str,
) -> Dict[str, Any]:
    if len(session) < LOCAL_BARS + 1:
        return {"state": "DONNEES_INSUFFISANTES", "bar_time": clean(session.iloc[-1]["LocalDatetime"].isoformat())}

    current = session.iloc[-1]
    prior = session.iloc[-(LOCAL_BARS + 1):-1]
    price = float(current["Close"])
    local_support = float(prior["Low"].min())
    local_resistance = float(prior["High"].max())
    session_support = float(session["Low"].min())
    session_resistance = float(session["High"].max())
    median_volume = float(prior["Volume"].median())
    last_volume = float(current["Volume"])
    volume_ratio = last_volume / median_volume if median_volume > 0 else 0.0
    clock = current["LocalDatetime"].strftime("%H:%M:%S")
    volume_eligible = clock not in MARKET_CLOSE_TIMES[market_of(ticker)]
    volume_confirmed = volume_eligible and volume_ratio >= VOLUME_MULTIPLIER
    breakout_local = price > local_resistance * (1 + BREAKOUT_BUFFER_PCT / 100.0)
    consolidation_range = percentage(float(prior["High"].max()), float(prior["Low"].min()))

    typical = (session["High"] + session["Low"] + session["Close"]) / 3.0
    vwap = float((typical * session["Volume"]).sum() / session["Volume"].sum()) if session["Volume"].sum() > 0 else price

    persisted_level = number(previous.get("PersistedBreakoutLevel"))
    persisted_time = clean(previous.get("PersistedBreakoutTime"))
    persisted_confirmed = clean(previous.get("PersistedBreakoutConfirmed")) == "True"
    breakout_age = sessions_between(clean(previous.get("SessionDate")), session_date) if persisted_time else 0

    # Expiration ou invalidation de la structure mémorisée.
    invalidated = bool(persisted_level and price < persisted_level * (1 - PULLBACK_INVALIDATION_PCT / 100.0))
    expired = bool(persisted_time and breakout_age > BREAKOUT_EXPIRY_SESSIONS)
    if invalidated or expired:
        persisted_level = None
        persisted_time = ""
        persisted_confirmed = False
        breakout_age = 0

    if breakout_local:
        persisted_level = local_resistance
        persisted_time = current["LocalDatetime"].isoformat()
        persisted_confirmed = volume_confirmed
        breakout_age = 0

    pullback_candidate = False
    if persisted_level and not breakout_local:
        pullback_candidate = (
            float(current["Low"]) <= persisted_level * (1 + PULLBACK_TOLERANCE_PCT / 100.0)
            and float(current["High"]) >= persisted_level * (1 - PULLBACK_TOLERANCE_PCT / 100.0)
            and price >= persisted_level
        )

    major_close = bool(daily.get("too_close", False))
    if invalidated:
        state = "FAUX_BREAKOUT"
    elif breakout_local and major_close:
        state = "BREAKOUT_LOCAL_RESISTANCE_MAJEURE_PROCHE"
    elif breakout_local and volume_confirmed:
        state = "BREAKOUT_CONFIRME"
    elif breakout_local:
        state = "BREAKOUT_NON_CONFIRME"
    elif pullback_candidate and persisted_confirmed and volume_confirmed and not major_close:
        state = "SIGNAL_STRUCTURE_SHADOW"
    elif pullback_candidate and persisted_confirmed:
        state = "PULLBACK_EN_COURS"
    elif price < local_support * (1 - PULLBACK_INVALIDATION_PCT / 100.0):
        state = "SUPPORT_CASSE"
    elif abs(percentage(price, local_resistance)) <= 0.50:
        state = "PROXIMITE_RESISTANCE"
    elif consolidation_range <= 2.0:
        state = "CONSOLIDATION"
    else:
        state = "AUCUNE_STRUCTURE"

    invalidation = persisted_level * (1 - PULLBACK_INVALIDATION_PCT / 100.0) if persisted_level else None
    return {
        "state": state,
        "bar_time": current["LocalDatetime"].isoformat(),
        "price": price,
        "session_open": float(session.iloc[0]["Open"]),
        "session_high": float(session["High"].max()),
        "session_low": float(session["Low"].min()),
        "vwap": vwap,
        "local_support": local_support,
        "local_resistance": local_resistance,
        "session_support": session_support,
        "session_resistance": session_resistance,
        "distance_support": percentage(price, local_support),
        "distance_resistance": percentage(price, local_resistance),
        "median_volume": median_volume,
        "last_volume": last_volume,
        "volume_ratio": volume_ratio,
        "volume_eligible": volume_eligible,
        "volume_confirmed": volume_confirmed,
        "consolidation_range": consolidation_range,
        "breakout_local": breakout_local,
        "pullback_candidate": pullback_candidate,
        "persisted_level": persisted_level,
        "persisted_time": persisted_time,
        "persisted_confirmed": persisted_confirmed,
        "breakout_age": breakout_age,
        "invalidation": invalidation,
    }


def main() -> int:
    observed_at = datetime.now(timezone.utc).isoformat()
    tickers = read_tickers()
    by_session, latest_v1 = load_v1_contexts()
    previous_states = {row["Ticker"]: row for row in read_csv_rows(STATE_FILE)}

    observations: List[Dict[str, Any]] = []
    states: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    print("=" * 112)
    print("STRUCTURE SHADOW AGENT V2 - MULTI-HORIZON ET MÉMOIRE PERSISTANTE")
    print("=" * 112)
    print(f"Tickers à observer : {len(tickers)}")

    for ticker in tickers:
        try:
            intraday = download_intraday(ticker)
            if intraday.empty:
                raise RuntimeError("Aucune donnée intraday reçue")
            session, session_date = latest_session(intraday, ticker)
            daily_data, daily_source = download_daily(ticker, session_date)
            last_price = float(session.iloc[-1]["Close"])
            daily = daily_levels(daily_data, last_price)
            previous = previous_states.get(ticker, {})
            metrics = calculate_structure(session, daily, previous, session_date, ticker)
            current_v1 = by_session.get((ticker, session_date))
            v1 = v1_status(current_v1, latest_v1.get(ticker))
            state = clean(metrics.get("state"))
            previous_state = clean(previous.get("StructureState"))
            bar_time = clean(metrics.get("bar_time"))
            observation_id = f"{ticker}|{bar_time}|{state}"

            common = {
                "Ticker": ticker,
                "Market": market_of(ticker),
                "SessionDate": session_date,
                "LastPrice": format_number(number(metrics.get("price"))),
                "LocalSupport12": format_number(number(metrics.get("local_support"))),
                "LocalResistance12": format_number(number(metrics.get("local_resistance"))),
                "SessionSupport": format_number(number(metrics.get("session_support"))),
                "SessionResistance": format_number(number(metrics.get("session_resistance"))),
                "DailySupport20": format_number(number(daily.get("support20"))),
                "DailyResistance20": format_number(number(daily.get("resistance20"))),
                "DailySupport60": format_number(number(daily.get("support60"))),
                "DailyResistance60": format_number(number(daily.get("resistance60"))),
                "NextMajorResistance": format_number(number(daily.get("next_major"))),
                "RoomToMajorResistancePct": format_number(number(daily.get("room"))),
                "MajorResistanceTooClose": str(bool(daily.get("too_close", False))),
                "VolumeRatio": format_number(number(metrics.get("volume_ratio"))),
                "VolumeEligible": str(bool(metrics.get("volume_eligible", False))),
                "VolumeConfirmed": str(bool(metrics.get("volume_confirmed", False))),
                "BreakoutLocal": str(bool(metrics.get("breakout_local", False))),
                "PullbackCandidate": str(bool(metrics.get("pullback_candidate", False))),
                "PersistedBreakoutLevel": format_number(number(metrics.get("persisted_level"))),
                "PersistedBreakoutTime": clean(metrics.get("persisted_time")),
                "PersistedBreakoutConfirmed": str(bool(metrics.get("persisted_confirmed", False))),
                "BreakoutAgeSessions": metrics.get("breakout_age", 0),
                "InvalidationLevel": format_number(number(metrics.get("invalidation"))),
                "V1StatusCurrentSession": v1["status"],
                "V1LastKnownDecision": v1["last_decision"],
                "V1LastKnownSession": v1["last_session"],
                "V1Reason": v1["reason"],
                "V1ContextSessionAligned": v1["aligned"],
                "ObservationOnly": "True",
                "V1ExecutionChanged": "False",
                "PortfolioV1Changed": "False",
                "CatalystDecisionPower": "False",
                "DailyDataSource": daily_source,
            }

            observation = {
                "ObservationID": observation_id,
                "ObservedAtUTC": observed_at,
                "BarTime": bar_time,
                "SessionOpen": format_number(number(metrics.get("session_open"))),
                "SessionHigh": format_number(number(metrics.get("session_high"))),
                "SessionLow": format_number(number(metrics.get("session_low"))),
                "SessionVWAP": format_number(number(metrics.get("vwap"))),
                "DistanceLocalSupportPct": format_number(number(metrics.get("distance_support"))),
                "DistanceLocalResistancePct": format_number(number(metrics.get("distance_resistance"))),
                "MedianVolume12": format_number(number(metrics.get("median_volume")), 0),
                "LastVolume": format_number(number(metrics.get("last_volume")), 0),
                "ConsolidationRangePct": format_number(number(metrics.get("consolidation_range"))),
                "StructureState": state,
                "V1DecisionTime": v1["decision_time"],
                **common,
            }
            observations.append(observation)

            state_row = {
                "LastObservedAtUTC": observed_at,
                "LastBarTime": bar_time,
                "StructureState": state,
                "PreviousStructureState": previous_state,
                **common,
            }
            states.append(state_row)

            if state == "SIGNAL_STRUCTURE_SHADOW" and previous_state != state:
                entry = number(metrics.get("price"))
                stop = number(metrics.get("invalidation"))
                target = entry * (1 + TARGET_PCT / 100.0) if entry else None
                risk = entry - stop if entry is not None and stop is not None else None
                rr = (target - entry) / risk if risk and risk > 0 else None
                if rr is not None and rr >= MIN_RR and not bool(daily.get("too_close", False)):
                    signals.append({
                        "SignalID": f"{ticker}|{bar_time}|SIGNAL_STRUCTURE_SHADOW",
                        "AgentVersion": AGENT_VERSION,
                        "SignalValidityPolicy": SIGNAL_VALIDITY_POLICY,
                        "Ticker": ticker,
                        "Market": market_of(ticker),
                        "SignalTime": bar_time,
                        "SessionDate": session_date,
                        "SignalType": "SIGNAL_STRUCTURE_SHADOW",
                        "StructureState": state,
                        "BreakoutLevel": format_number(number(metrics.get("persisted_level"))),
                        "ReferencePrice": format_number(entry),
                        "StopPrice": format_number(stop),
                        "TargetPrice": format_number(target),
                        "RiskReward": format_number(rr),
                        "VolumeRatio": common["VolumeRatio"],
                        "NextMajorResistance": common["NextMajorResistance"],
                        "RoomToMajorResistancePct": common["RoomToMajorResistancePct"],
                        "V1StatusCurrentSession": v1["status"],
                        "V1LastKnownDecision": v1["last_decision"],
                        "V1LastKnownSession": v1["last_session"],
                        "V1Reason": v1["reason"],
                        "SignalFrozenBeforeOutcome": "True",
                        "ObservationOnly": "True",
                        "V1ExecutionChanged": "False",
                        "PortfolioV1Changed": "False",
                        "CatalystDecisionPower": "False",
                    })

            print(
                f"{ticker:<8} | {v1['status']:<13} | {state:<45} | "
                f"prix {common['LastPrice']} | vol x{common['VolumeRatio']} | "
                f"room {common['RoomToMajorResistancePct']}% | daily {daily_source}"
            )
        except Exception as error:
            failures.append({"ticker": ticker, "error": str(error)})
            print(f"{ticker:<8} | ERREUR | {error}")

    new_observations = append_unique(OBSERVATIONS_FILE, OBS_FIELDS, observations, "ObservationID")
    write_csv_rows(STATE_FILE, STATE_FIELDS, states)
    new_signals = append_unique(SIGNALS_FILE, SIGNAL_FIELDS, signals, "SignalID")

    counts: Dict[str, int] = {}
    for row in states:
        key = clean(row.get("StructureState"))
        counts[key] = counts.get(key, 0) + 1

    latest_payload = {
        "GeneratedAtUTC": observed_at,
        "Agent": "StructureShadowV2MultiHorizon",
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

    summary = {
        "GeneratedAtUTC": observed_at,
        "AgentVersion": AGENT_VERSION,
        "WatchlistSize": len(tickers),
        "Observed": len(observations),
        "Failed": len(failures),
        "NewObservations": new_observations,
        "NewSignals": new_signals,
        "DailyRemoteRefreshes": sum(row.get("DailyDataSource") == "REMOTE_REFRESH" for row in states),
        "DailyCacheHits": sum(row.get("DailyDataSource") in {"CACHE", "RAW"} for row in states),
        "StateCounts": counts,
        "ObservationOnly": True,
        "V1ExecutionChanged": False,
        "PortfolioV1Changed": False,
        "CatalystDecisionPower": False,
    }
    SUMMARY_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 112)
    print(f"Observés : {len(observations)}/{len(tickers)} | erreurs : {len(failures)}")
    print(f"Nouvelles observations : {new_observations} | nouveaux signaux : {new_signals}")
    print(f"État : {STATE_FILE}")
    print(f"Mémoire : {OBSERVATIONS_FILE}")
    print(f"Signaux : {SIGNALS_FILE}")
    print("ObservationOnly : True")
    print("V1ExecutionChanged : False")
    print("PortfolioV1Changed : False")
    print("CatalystDecisionPower : False")
    print("=" * 112)
    return 1 if len(observations) == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
