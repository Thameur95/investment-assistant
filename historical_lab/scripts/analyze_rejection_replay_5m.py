#!/usr/bin/env python3
"""
analyze_rejection_replay_5m.py - Analyse intégrée V2

But
---
Répondre en un seul passage à toutes les questions nécessaires avant la création
de l'Agent Structure :
  1. trajectoire après rejet V1 ;
  2. détection sans lookahead d'un breakout, du volume et d'un pullback ;
  3. construction d'une entrée Shadow ;
  4. traitement conservateur des signaux tardifs à 15:55 ;
  5. simulation chronologique TP/SL bougie par bougie avec gaps ;
  6. hypothèse conservatrice STOP d'abord si TP et SL sont touchés dans la même bougie ;
  7. MFE/MAE après l'entrée, durée, nuits détenues et ordre TP/SL ;
  8. verdict de qualité de structure et état de préparation de l'Agent Structure.

OBSERVATION UNIQUEMENT. Aucun impact sur V1, V2 Shadow ou le portefeuille.

Entrées
-------
  intraday_decisions.csv
  historical_lab/data/rejection_replay/*_J0_J4_5m.csv

Sorties
-------
  historical_lab/results/rejection_replay_5m_analysis.csv
  historical_lab/results/rejection_replay_5m_events.csv
  historical_lab/results/rejection_replay_5m_execution.csv
  historical_lab/results/rejection_replay_5m_summary.csv
  historical_lab/results/rejection_replay_5m_report.txt
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

# Hypothèses Structure Shadow, indépendantes de V1.
ROLLING_BARS = 12
BREAKOUT_BUFFER_PCT = 0.10
VOLUME_MULTIPLIER = 1.50
PULLBACK_TOLERANCE_PCT = 0.35
PULLBACK_INVALIDATION_PCT = 0.50
PULLBACK_WINDOW_BARS = 24
TARGET_PCT = 2.0
LATE_SIGNAL_TIME = "15:55:00"


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def to_float(value: Any) -> Optional[float]:
    text = clean(value).replace(",", ".")
    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pct(value: Optional[float], reference: Optional[float]) -> Optional[float]:
    if value is None or reference is None or reference == 0:
        return None
    return (value / reference - 1.0) * 100.0


def fmt(value: Optional[float], decimals: int = 4) -> str:
    return "" if value is None else f"{value:.{decimals}f}"


def yesno(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def dt_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).isoformat()


def first_true_index(mask: pd.Series) -> Optional[int]:
    indices = mask[mask].index.tolist()
    return int(indices[0]) if indices else None


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_decisions(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_decision(decisions: Sequence[Dict[str, str]], ticker: str, date: str) -> Optional[Dict[str, str]]:
    matches = [
        row for row in decisions
        if clean(row.get("ticker")) == ticker
        and clean(row.get("session_date"))[:10] == date
        and "ACHAT" not in clean(row.get("decision")).upper()
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: clean(row.get("decision_time")))
    return matches[-1]


def load_event(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path)
    required = {
        "EventID", "EventRole", "Ticker", "RejectionDate", "DecisionTime_NewYork",
        "SessionOffset", "SessionLabel", "Datetime_NewYork", "Date_NewYork",
        "Time_NewYork", "Open", "High", "Low", "Close", "Volume",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"Colonnes manquantes dans {path}: {', '.join(missing)}")

    data["Datetime_NewYork"] = pd.to_datetime(data["Datetime_NewYork"], utc=True, errors="coerce").dt.tz_convert("America/New_York")
    data = data.dropna(subset=["Datetime_NewYork"]).copy()
    for column in ("Open", "High", "Low", "Close", "Volume", "SessionOffset"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    data = data.sort_values("Datetime_NewYork").reset_index(drop=True)

    typical = (data["High"] + data["Low"] + data["Close"]) / 3.0
    pv = typical * data["Volume"]
    data["SessionVWAP"] = pv.groupby(data["Date_NewYork"]).cumsum() / data.groupby("Date_NewYork")["Volume"].cumsum().replace(0, pd.NA)
    data["PriorResistance12"] = data["High"].shift(1).rolling(ROLLING_BARS, min_periods=ROLLING_BARS).max()
    data["PriorMedianVolume12"] = data["Volume"].shift(1).rolling(ROLLING_BARS, min_periods=ROLLING_BARS).median()
    return data


def post_rejection(data: pd.DataFrame) -> pd.DataFrame:
    date = clean(data.iloc[0]["RejectionDate"])
    time = clean(data.iloc[0]["DecisionTime_NewYork"])
    decision_dt = pd.Timestamp(f"{date} {time}", tz="America/New_York")
    return data[data["Datetime_NewYork"] >= decision_dt].copy().reset_index(drop=True)


def locate_threshold(post: pd.DataFrame, reference: float, threshold_pct: float) -> Tuple[bool, Optional[int]]:
    target = reference * (1 + threshold_pct / 100.0)
    index = first_true_index(post["High"] >= target)
    return index is not None, index


def detect_reclaim(post: pd.DataFrame, level_series: pd.Series) -> Optional[int]:
    mask = (post["Close"].shift(1) <= level_series.shift(1)) & (post["Close"] > level_series)
    return first_true_index(mask)


def detect_structure(post: pd.DataFrame) -> Dict[str, Any]:
    breakout_mask = (
        post["PriorResistance12"].notna()
        & (post["Close"] > post["PriorResistance12"] * (1 + BREAKOUT_BUFFER_PCT / 100.0))
    )
    breakout_index = first_true_index(breakout_mask)
    result = {
        "breakout_index": None, "level": None, "volume_ratio": None,
        "volume_confirmed": False, "pullback_index": None,
        "pullback_held": False, "pullback_invalidated": False,
    }
    if breakout_index is None:
        return result

    row = post.loc[breakout_index]
    level = float(row["PriorResistance12"])
    median_volume = to_float(row["PriorMedianVolume12"])
    volume_ratio = float(row["Volume"]) / median_volume if median_volume and median_volume > 0 else None
    result.update({
        "breakout_index": breakout_index,
        "level": level,
        "volume_ratio": volume_ratio,
        "volume_confirmed": volume_ratio is not None and volume_ratio >= VOLUME_MULTIPLIER,
    })

    end = min(len(post) - 1, breakout_index + PULLBACK_WINDOW_BARS)
    for index, candidate in post.loc[breakout_index + 1:end].iterrows():
        if float(candidate["Close"]) < level * (1 - PULLBACK_INVALIDATION_PCT / 100.0):
            result["pullback_invalidated"] = True
            break
        touched = (
            float(candidate["Low"]) <= level * (1 + PULLBACK_TOLERANCE_PCT / 100.0)
            and float(candidate["High"]) >= level * (1 - PULLBACK_TOLERANCE_PCT / 100.0)
        )
        if touched and float(candidate["Close"]) >= level:
            result["pullback_index"] = int(index)
            result["pullback_held"] = True
            break
    return result


def build_signal(post: pd.DataFrame, structure: Dict[str, Any]) -> Dict[str, Any]:
    breakout_index = structure["breakout_index"]
    pullback_index = structure["pullback_index"]
    signal_index = pullback_index if pullback_index is not None else breakout_index
    if signal_index is None:
        return {"signal_index": None}

    signal_row = post.loc[signal_index]
    signal_time = clean(signal_row["Time_NewYork"])
    is_late = signal_time >= LATE_SIGNAL_TIME

    # Signal normal : exécution à l'ouverture de la bougie suivante afin d'éviter
    # d'utiliser la clôture de confirmation comme prix exécutable rétroactif.
    executable_index = signal_index + 1
    entry_policy = "NEXT_BAR_OPEN"

    # Signal 15:55 ou plus tard : exécution conservatrice à l'ouverture de J+1.
    if is_late:
        current_date = clean(signal_row["Date_NewYork"])
        later = post[(post.index > signal_index) & (post["Date_NewYork"] != current_date)]
        executable_index = int(later.index[0]) if not later.empty else None
        entry_policy = "NEXT_SESSION_OPEN"

    if executable_index is None or executable_index >= len(post):
        return {
            "signal_index": signal_index,
            "is_late": is_late,
            "entry_policy": entry_policy,
            "executable_index": None,
        }

    entry_row = post.loc[executable_index]
    entry_price = float(entry_row["Open"])
    level = to_float(structure["level"])

    # Stop déterminé uniquement avec les informations connues au signal.
    recent_start = max(0, signal_index - 2)
    recent_low = float(post.loc[recent_start:signal_index, "Low"].min())
    structural_floor = level * (1 - PULLBACK_INVALIDATION_PCT / 100.0) if level else recent_low
    stop = min(recent_low, structural_floor)
    target = entry_price * (1 + TARGET_PCT / 100.0)
    risk = entry_price - stop
    rr = (target - entry_price) / risk if risk > 0 else None

    return {
        "signal_index": signal_index,
        "is_late": is_late,
        "entry_policy": entry_policy,
        "executable_index": executable_index,
        "entry_price": entry_price,
        "stop": stop,
        "target": target,
        "rr": rr,
    }


def simulate_execution(post: pd.DataFrame, signal: Dict[str, Any]) -> Dict[str, Any]:
    entry_index = signal.get("executable_index")
    if entry_index is None:
        return {
            "status": "NON_EXECUTABLE", "exit_reason": "Aucune bougie exécutable après signal",
            "target_hit": False, "stop_hit": False, "target_before_stop": False,
        }

    entry_price = float(signal["entry_price"])
    stop = float(signal["stop"])
    target = float(signal["target"])
    entry_row = post.loc[entry_index]
    entry_date = clean(entry_row["Date_NewYork"])

    # Le trade commence à l'Open de entry_index. Cette même bougie est donc simulée.
    maximum = entry_price
    minimum = entry_price
    exit_index: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason = "TIME_J4"
    same_bar_ambiguity = False
    target_hit = False
    stop_hit = False

    for index, row in post.loc[entry_index:].iterrows():
        op = float(row["Open"])
        hi = float(row["High"])
        lo = float(row["Low"])
        maximum = max(maximum, hi)
        minimum = min(minimum, lo)

        # Gaps prioritaires à l'ouverture.
        if op <= stop:
            exit_index, exit_price, exit_reason, stop_hit = int(index), op, "GAP_SOUS_STOP", True
            break
        if op >= target:
            exit_index, exit_price, exit_reason, target_hit = int(index), op, "GAP_AU_DESSUS_TP", True
            break

        stop_inside = lo <= stop
        target_inside = hi >= target
        if stop_inside and target_inside:
            # Hypothèse conservatrice imposée : stop d'abord.
            exit_index, exit_price, exit_reason = int(index), stop, "STOP_AVANT_TP_MEME_BOUGIE"
            stop_hit, target_hit, same_bar_ambiguity = True, True, True
            break
        if stop_inside:
            exit_index, exit_price, exit_reason, stop_hit = int(index), stop, "STOP", True
            break
        if target_inside:
            exit_index, exit_price, exit_reason, target_hit = int(index), target, "TAKE_PROFIT_2PCT", True
            break

    if exit_index is None:
        exit_index = int(post.index[-1])
        exit_price = float(post.iloc[-1]["Close"])

    exit_row = post.loc[exit_index]
    exit_date = clean(exit_row["Date_NewYork"])
    unique_dates = post.loc[entry_index:exit_index, "Date_NewYork"].drop_duplicates().tolist()
    overnight_count = max(0, len(unique_dates) - 1)

    return {
        "status": "EXECUTED",
        "entry_index": entry_index,
        "entry_time": dt_text(entry_row["Datetime_NewYork"]),
        "entry_date": entry_date,
        "entry_price": entry_price,
        "stop": stop,
        "target": target,
        "rr": signal.get("rr"),
        "exit_index": exit_index,
        "exit_time": dt_text(exit_row["Datetime_NewYork"]),
        "exit_date": exit_date,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "return_pct": pct(exit_price, entry_price),
        "mfe_pct": pct(maximum, entry_price),
        "mae_pct": pct(minimum, entry_price),
        "holding_bars": exit_index - entry_index + 1,
        "holding_sessions": len(unique_dates),
        "overnight_count": overnight_count,
        "target_hit": target_hit,
        "stop_hit": stop_hit,
        "target_before_stop": target_hit and exit_reason in {"TAKE_PROFIT_2PCT", "GAP_AU_DESSUS_TP"},
        "same_bar_ambiguity": same_bar_ambiguity,
    }


def quality_verdict(structure: Dict[str, Any], signal: Dict[str, Any], execution: Dict[str, Any]) -> Tuple[str, str]:
    if structure["breakout_index"] is None:
        return "PAS_DE_SECONDE_CHANCE", "Aucun breakout détecté sans lookahead"
    if signal.get("executable_index") is None:
        return "SIGNAL_NON_EXECUTABLE", "Aucune entrée réalisable après confirmation"
    if execution.get("target_before_stop"):
        if structure["volume_confirmed"] and structure["pullback_held"] and (signal.get("rr") or 0) >= 2:
            return "SECONDE_CHANCE_VALIDEE_SHADOW", "TP atteint avant SL avec breakout, volume, pullback et RR >= 2"
        return "SECONDE_CHANCE_GAGNANTE_FRAGILE", "TP atteint avant SL mais confirmations incomplètes"
    if execution.get("same_bar_ambiguity"):
        return "SECONDE_CHANCE_REJETEE_CONSERVATRICE", "TP et SL dans la même bougie, hypothèse STOP d'abord"
    if execution.get("stop_hit"):
        return "SECONDE_CHANCE_PERDANTE", "SL atteint avant TP"
    return "SECONDE_CHANCE_NON_CONCLUANTE", "Ni TP ni SL avant la fin de J+4"


def analyze(path: Path, decisions: Sequence[Dict[str, str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    data = load_event(path)
    post = post_rejection(data)
    if post.empty:
        raise ValueError(f"Aucune bougie post-rejet dans {path}")

    event_id = clean(data.iloc[0]["EventID"])
    ticker = clean(data.iloc[0]["Ticker"])
    rejection_date = clean(data.iloc[0]["RejectionDate"])
    role = clean(data.iloc[0]["EventRole"])
    decision = find_decision(decisions, ticker, rejection_date)

    reference = to_float(decision.get("decision_close")) if decision else None
    reference_source = "intraday_decisions.decision_close"
    if reference is None:
        reference = float(post.iloc[0]["Close"])
        reference_source = "premiere_bougie_post_rejet"

    or_high = to_float(decision.get("opening_range_high")) if decision else None
    saved_vwap = to_float(decision.get("vwap")) if decision else None
    reason = clean(decision.get("reason")) if decision else clean(data.iloc[0].get("RejectionReason"))

    reached2, index2 = locate_threshold(post, reference, 2.0)
    reached3, index3 = locate_threshold(post, reference, 3.0)
    mfe_rejection = pct(float(post["High"].max()), reference)
    mae_rejection = pct(float(post["Low"].min()), reference)

    vwap_index = detect_reclaim(post, post["SessionVWAP"])
    if or_high is not None:
        or_series = pd.Series(or_high, index=post.index, dtype=float)
        or_index = detect_reclaim(post, or_series)
    else:
        or_index = None

    structure = detect_structure(post)
    signal = build_signal(post, structure)
    execution = simulate_execution(post, signal)
    verdict, verdict_reason = quality_verdict(structure, signal, execution)

    bi = structure["breakout_index"]
    pi = structure["pullback_index"]
    si = signal.get("signal_index")

    analysis = {
        "EventID": event_id,
        "EventRole": role,
        "Ticker": ticker,
        "RejectionDate": rejection_date,
        "DecisionTime_NewYork": clean(data.iloc[0]["DecisionTime_NewYork"]),
        "RejectionReason": reason,
        "ReferencePrice": fmt(reference),
        "ReferenceSource": reference_source,
        "OpeningRangeHigh": fmt(or_high),
        "SavedDecisionVWAP": fmt(saved_vwap),
        "PostRejectionBars": len(post),
        "MFEFromRejectionPct": fmt(mfe_rejection),
        "MAEFromRejectionPct": fmt(mae_rejection),
        "ReachedPlus2FromRejection": yesno(reached2),
        "Plus2FromRejectionTimeNY": dt_text(post.loc[index2, "Datetime_NewYork"]) if index2 is not None else "",
        "ReachedPlus3FromRejection": yesno(reached3),
        "Plus3FromRejectionTimeNY": dt_text(post.loc[index3, "Datetime_NewYork"]) if index3 is not None else "",
        "VWAPReclaim": yesno(vwap_index is not None),
        "VWAPReclaimTimeNY": dt_text(post.loc[vwap_index, "Datetime_NewYork"]) if vwap_index is not None else "",
        "ORHighReclaim": yesno(or_index is not None),
        "ORHighReclaimTimeNY": dt_text(post.loc[or_index, "Datetime_NewYork"]) if or_index is not None else "",
        "BreakoutDetected": yesno(bi is not None),
        "BreakoutTimeNY": dt_text(post.loc[bi, "Datetime_NewYork"]) if bi is not None else "",
        "BreakoutLevel": fmt(structure["level"]),
        "BreakoutVolumeRatio": fmt(structure["volume_ratio"]),
        "VolumeConfirmed": yesno(bool(structure["volume_confirmed"])),
        "PullbackDetected": yesno(pi is not None),
        "PullbackTimeNY": dt_text(post.loc[pi, "Datetime_NewYork"]) if pi is not None else "",
        "PullbackHeld": yesno(bool(structure["pullback_held"])),
        "PullbackInvalidated": yesno(bool(structure["pullback_invalidated"])),
        "StructureSignalTimeNY": dt_text(post.loc[si, "Datetime_NewYork"]) if si is not None else "",
        "LateSignal": yesno(bool(signal.get("is_late", False))),
        "EntryPolicy": clean(signal.get("entry_policy")),
        "ShadowEntryExecutableTimeNY": clean(execution.get("entry_time")),
        "ShadowEntryPrice": fmt(to_float(execution.get("entry_price"))),
        "ShadowStopPrice": fmt(to_float(execution.get("stop"))),
        "ShadowTargetPrice": fmt(to_float(execution.get("target"))),
        "ShadowInitialRiskReward": fmt(to_float(execution.get("rr"))),
        "ShadowExitTimeNY": clean(execution.get("exit_time")),
        "ShadowExitPrice": fmt(to_float(execution.get("exit_price"))),
        "ShadowExitReason": clean(execution.get("exit_reason")),
        "ShadowReturnPct": fmt(to_float(execution.get("return_pct"))),
        "ShadowMFEPctAfterEntry": fmt(to_float(execution.get("mfe_pct"))),
        "ShadowMAEPctAfterEntry": fmt(to_float(execution.get("mae_pct"))),
        "TargetHit": yesno(bool(execution.get("target_hit", False))),
        "StopHit": yesno(bool(execution.get("stop_hit", False))),
        "TargetBeforeStop": yesno(bool(execution.get("target_before_stop", False))),
        "SameBarAmbiguity": yesno(bool(execution.get("same_bar_ambiguity", False))),
        "HoldingBars": execution.get("holding_bars", ""),
        "HoldingSessions": execution.get("holding_sessions", ""),
        "OvernightCount": execution.get("overnight_count", ""),
        "ShadowVerdict": verdict,
        "ShadowVerdictReason": verdict_reason,
        "ObservationOnly": "TRUE",
        "V1ExecutionChanged": "FALSE",
    }

    technical_events: List[Dict[str, Any]] = []
    specs = [
        ("REJET_V1", 0), ("REPRISE_VWAP", vwap_index), ("REPRISE_OR_HIGH", or_index),
        ("BREAKOUT_12_BARS", bi), ("PULLBACK_TENU", pi),
        ("SIGNAL_STRUCTURE", si), ("ENTREE_EXECUTABLE", execution.get("entry_index")),
        ("SORTIE_SHADOW", execution.get("exit_index")),
    ]
    for event_type, index in specs:
        if index is None:
            continue
        row = post.loc[int(index)]
        technical_events.append({
            "EventID": event_id,
            "Ticker": ticker,
            "TechnicalEvent": event_type,
            "DatetimeNewYork": dt_text(row["Datetime_NewYork"]),
            "SessionLabel": clean(row["SessionLabel"]),
            "Open": fmt(to_float(row["Open"])),
            "High": fmt(to_float(row["High"])),
            "Low": fmt(to_float(row["Low"])),
            "Close": fmt(to_float(row["Close"])),
            "Volume": fmt(to_float(row["Volume"]), 0),
            "SessionVWAP": fmt(to_float(row["SessionVWAP"])),
            "PriorResistance12": fmt(to_float(row["PriorResistance12"])),
        })

    execution_row = {
        "EventID": event_id,
        "Ticker": ticker,
        "EntryPolicy": clean(signal.get("entry_policy")),
        "LateSignal": yesno(bool(signal.get("is_late", False))),
        "EntryTime": clean(execution.get("entry_time")),
        "EntryPrice": fmt(to_float(execution.get("entry_price"))),
        "StopPrice": fmt(to_float(execution.get("stop"))),
        "TargetPrice": fmt(to_float(execution.get("target"))),
        "InitialRR": fmt(to_float(execution.get("rr"))),
        "ExitTime": clean(execution.get("exit_time")),
        "ExitPrice": fmt(to_float(execution.get("exit_price"))),
        "ExitReason": clean(execution.get("exit_reason")),
        "ReturnPct": fmt(to_float(execution.get("return_pct"))),
        "MFEPctAfterEntry": fmt(to_float(execution.get("mfe_pct"))),
        "MAEPctAfterEntry": fmt(to_float(execution.get("mae_pct"))),
        "TargetBeforeStop": yesno(bool(execution.get("target_before_stop", False))),
        "SameBarAmbiguity": yesno(bool(execution.get("same_bar_ambiguity", False))),
        "HoldingBars": execution.get("holding_bars", ""),
        "HoldingSessions": execution.get("holding_sessions", ""),
        "OvernightCount": execution.get("overnight_count", ""),
        "Verdict": verdict,
    }
    return analysis, technical_events, execution_row


def build_summary(analyses: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    executed = [row for row in analyses if row["ShadowEntryExecutableTimeNY"]]
    winners = [row for row in executed if row["TargetBeforeStop"] == "TRUE"]
    losers = [row for row in executed if row["StopHit"] == "TRUE" and row["TargetBeforeStop"] == "FALSE"]
    returns = [to_float(row["ShadowReturnPct"]) for row in executed]
    returns = [value for value in returns if value is not None]
    strong = [row for row in analyses if row["ShadowVerdict"] == "SECONDE_CHANCE_VALIDEE_SHADOW"]

    controls = [row for row in analyses if row["EventRole"] == "CONTROLE"]
    suspects = [row for row in analyses if row["EventRole"] == "SUSPECT"]
    control_wins = sum(row["TargetBeforeStop"] == "TRUE" for row in controls)
    suspect_wins = sum(row["TargetBeforeStop"] == "TRUE" for row in suspects)

    readiness = "PRET_POUR_PROTOTYPE_AGENT_STRUCTURE"
    reason = "Exécution chronologique complète disponible, mais échantillon insuffisant pour promotion"
    if not executed or len(executed) != len(analyses):
        readiness = "DONNEES_INCOMPLETES"
        reason = "Au moins une seconde chance n'est pas exécutable"

    return [{
        "EventsAnalyzed": len(analyses),
        "TradesExecuted": len(executed),
        "TargetBeforeStopCount": len(winners),
        "StopBeforeTargetCount": len(losers),
        "SameBarAmbiguityCount": sum(row["SameBarAmbiguity"] == "TRUE" for row in analyses),
        "WinRatePct": fmt(100 * len(winners) / len(executed) if executed else None, 2),
        "AverageReturnPct": fmt(sum(returns) / len(returns) if returns else None),
        "ValidatedShadowCount": len(strong),
        "SuspectEvents": len(suspects),
        "SuspectTargetBeforeStop": suspect_wins,
        "ControlEvents": len(controls),
        "ControlTargetBeforeStop": control_wins,
        "StructureAgentReadiness": readiness,
        "ReadinessReason": reason,
        "PromotionAllowed": "FALSE",
        "V1ExecutionChanged": "FALSE",
    }]


def build_report(analyses: Sequence[Dict[str, Any]], summary: Dict[str, Any]) -> str:
    lines = [
        "=" * 110,
        "ANALYSE INTÉGRÉE 5 MINUTES - SECONDE CHANCE SHADOW",
        "=" * 110,
        "Une seule analyse : structure, exécution, ordre TP/SL, gaps, MFE/MAE et signaux tardifs.",
        "",
    ]
    for row in analyses:
        lines.extend([
            f"{row['EventID']} | {row['EventRole']}",
            f"  Rejet V1      : {row['RejectionDate']} {row['DecisionTime_NewYork']} | {row['RejectionReason']}",
            f"  Structure     : breakout {row['BreakoutDetected']} | volume {row['VolumeConfirmed']} | pullback {row['PullbackHeld']}",
            f"  Signal/entrée : {row['StructureSignalTimeNY']} | {row['EntryPolicy']} | entrée {row['ShadowEntryExecutableTimeNY']} à {row['ShadowEntryPrice']}",
            f"  Risque        : stop {row['ShadowStopPrice']} | cible {row['ShadowTargetPrice']} | RR {row['ShadowInitialRiskReward']}",
            f"  Sortie        : {row['ShadowExitTimeNY']} | {row['ShadowExitReason']} | rendement {row['ShadowReturnPct']} %",
            f"  Après entrée  : MFE {row['ShadowMFEPctAfterEntry']} % | MAE {row['ShadowMAEPctAfterEntry']} % | nuits {row['OvernightCount']}",
            f"  Verdict       : {row['ShadowVerdict']} | {row['ShadowVerdictReason']}",
            "",
        ])

    lines.extend([
        "SYNTHÈSE",
        f"  Trades exécutés       : {summary['TradesExecuted']}",
        f"  TP avant SL           : {summary['TargetBeforeStopCount']}",
        f"  SL avant TP           : {summary['StopBeforeTargetCount']}",
        f"  Ambiguïtés même bougie: {summary['SameBarAmbiguityCount']}",
        f"  Taux de réussite      : {summary['WinRatePct']} %",
        f"  Rendement moyen       : {summary['AverageReturnPct']} %",
        f"  État Agent Structure  : {summary['StructureAgentReadiness']}",
        "",
        "GARDE-FOUS",
        "- Résultats descriptifs sur quatre événements seulement.",
        "- Aucune promotion ni modification de V1 autorisée.",
        "- L'Agent Structure devra ensuite être généralisé à tous les rejets en Shadow.",
        "- Une adoption future exigera walk-forward, OOS, coûts et slippage.",
        "",
        "ObservationOnly : True",
        "V1ExecutionChanged : False",
        "=" * 110,
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse intégrée 5 minutes des rejets V1.")
    parser.add_argument("--data-dir", default="historical_lab/data/rejection_replay")
    parser.add_argument("--decisions", default="intraday_decisions.csv")
    parser.add_argument("--output-dir", default="historical_lab/results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    decisions_path = Path(args.decisions)
    output_dir = Path(args.output_dir)

    if not data_dir.is_dir() or not decisions_path.exists():
        print("ERREUR : données replay ou intraday_decisions.csv introuvables", file=sys.stderr)
        return 2

    files = sorted(data_dir.glob("*_J0_J4_5m.csv"))
    if not files:
        print("ERREUR : aucun fichier replay J0_J4", file=sys.stderr)
        return 2

    decisions = read_decisions(decisions_path)
    analyses: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    executions: List[Dict[str, Any]] = []

    for path in files:
        analysis, technical_events, execution = analyze(path, decisions)
        analyses.append(analysis)
        events.extend(technical_events)
        executions.append(execution)

    summary_rows = build_summary(analyses)
    summary = summary_rows[0]

    analysis_path = output_dir / "rejection_replay_5m_analysis.csv"
    events_path = output_dir / "rejection_replay_5m_events.csv"
    execution_path = output_dir / "rejection_replay_5m_execution.csv"
    summary_path = output_dir / "rejection_replay_5m_summary.csv"
    report_path = output_dir / "rejection_replay_5m_report.txt"

    write_csv(analysis_path, analyses)
    write_csv(events_path, events)
    write_csv(execution_path, executions)
    write_csv(summary_path, summary_rows)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(analyses, summary), encoding="utf-8")

    print("=" * 110)
    print("ANALYSE INTÉGRÉE 5 MINUTES - STRUCTURE + EXÉCUTION SHADOW")
    print("=" * 110)
    print(f"Événements analysés : {len(analyses)}")
    for row in analyses:
        print(
            f"{row['EventID']} | {row['ShadowVerdict']} | "
            f"entrée {row['ShadowEntryPrice']} | sortie {row['ShadowExitReason']} | "
            f"rendement {row['ShadowReturnPct']} % | MFE {row['ShadowMFEPctAfterEntry']} % | "
            f"MAE {row['ShadowMAEPctAfterEntry']} %"
        )
    print("-" * 110)
    print(f"TP avant SL : {summary['TargetBeforeStopCount']}/{summary['TradesExecuted']}")
    print(f"Win rate    : {summary['WinRatePct']} %")
    print(f"État Agent Structure : {summary['StructureAgentReadiness']}")
    print(f"Détail     : {analysis_path}")
    print(f"Exécutions : {execution_path}")
    print(f"Résumé     : {summary_path}")
    print(f"Rapport    : {report_path}")
    print("ObservationOnly : True")
    print("V1ExecutionChanged : False")
    print("=" * 110)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
