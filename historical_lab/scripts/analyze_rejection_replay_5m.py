#!/usr/bin/env python3
"""
analyze_rejection_replay_5m.py

Analyse 5 minutes, strictement observationnelle, de quatre rejets V1 ciblés.
Le script recherche une éventuelle seconde chance de type breakout / pullback
entre l'heure du rejet et J+4, sans modifier V1, V2 Shadow ou le portefeuille.

Entrées attendues :
  intraday_decisions.csv
  historical_lab/data/rejection_replay/*_J0_J4_5m.csv

Sorties :
  historical_lab/results/rejection_replay_5m_analysis.csv
  historical_lab/results/rejection_replay_5m_events.csv
  historical_lab/results/rejection_replay_5m_report.txt

Règles Shadow transparentes (hypothèses de recherche, pas règles de trading) :
  - résistance dynamique : plus haut des 12 bougies précédentes ;
  - breakout : clôture > résistance précédente de 0,10 % ;
  - confirmation volume : volume >= 1,50 x médiane des 12 bougies précédentes ;
  - pullback : après breakout, retour à +/- 0,35 % du niveau, avec clôture >= niveau ;
  - pullback invalidé si clôture < niveau de breakout de 0,50 % ;
  - fenêtre de pullback : 24 bougies (2 heures) ;
  - seconde chance forte : breakout + volume + pullback tenu + RR >= 2 ;
  - seconde chance possible : breakout + (volume ou pullback) + RR >= 1,5 ;
  - sinon : aucune seconde chance / cas ambigu.

IMPORTANT : ces règles servent à décrire les quatre cas et ne doivent pas être
promues dans V1 sans validation sur un échantillon indépendant beaucoup plus large.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd


ROLLING_BARS = 12
BREAKOUT_BUFFER_PCT = 0.10
VOLUME_MULTIPLIER = 1.50
PULLBACK_TOLERANCE_PCT = 0.35
PULLBACK_INVALIDATION_PCT = 0.50
PULLBACK_WINDOW_BARS = 24
TARGET_2_PCT = 2.0
TARGET_3_PCT = 3.0


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


def bool_text(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def first_true_index(mask: pd.Series) -> Optional[int]:
    positions = mask[mask].index.tolist()
    return int(positions[0]) if positions else None


def dt_text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return pd.Timestamp(value).isoformat()


def read_decisions(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def find_decision(
    decisions: Sequence[Dict[str, str]], ticker: str, rejection_date: str
) -> Optional[Dict[str, str]]:
    matches = [
        row
        for row in decisions
        if clean(row.get("ticker")) == ticker
        and clean(row.get("session_date"))[:10] == rejection_date
        and "ACHAT" not in clean(row.get("decision")).upper()
    ]
    if not matches:
        return None
    matches.sort(key=lambda row: clean(row.get("decision_time")))
    return matches[-1]


def load_event(path: Path) -> pd.DataFrame:
    data = pd.read_csv(path)
    required = {
        "EventID", "EventRole", "Ticker", "RejectionDate",
        "DecisionTime_NewYork", "SessionOffset", "SessionLabel",
        "Datetime_NewYork", "Date_NewYork", "Time_NewYork",
        "Open", "High", "Low", "Close", "Volume",
    }
    missing = sorted(required - set(data.columns))
    if missing:
        raise ValueError(f"Colonnes manquantes dans {path}: {', '.join(missing)}")

    data["Datetime_NewYork"] = pd.to_datetime(
        data["Datetime_NewYork"], errors="coerce", utc=True
    ).dt.tz_convert("America/New_York")
    data = data.dropna(subset=["Datetime_NewYork"]).copy()

    for column in ("Open", "High", "Low", "Close", "Volume", "SessionOffset"):
        data[column] = pd.to_numeric(data[column], errors="coerce")

    data = data.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    data = data.sort_values("Datetime_NewYork").reset_index(drop=True)

    # VWAP recalculé séparément pour chaque séance.
    typical = (data["High"] + data["Low"] + data["Close"]) / 3.0
    pv = typical * data["Volume"]
    grouped_volume = data.groupby("Date_NewYork")["Volume"].cumsum()
    grouped_pv = pv.groupby(data["Date_NewYork"]).cumsum()
    data["SessionVWAP"] = grouped_pv / grouped_volume.replace(0, pd.NA)

    # Résistance uniquement à partir des bougies antérieures, sans lookahead.
    data["PriorResistance12"] = data["High"].shift(1).rolling(
        ROLLING_BARS, min_periods=ROLLING_BARS
    ).max()
    data["PriorMedianVolume12"] = data["Volume"].shift(1).rolling(
        ROLLING_BARS, min_periods=ROLLING_BARS
    ).median()

    return data


def post_rejection_frame(data: pd.DataFrame) -> pd.DataFrame:
    rejection_date = clean(data.iloc[0]["RejectionDate"])
    decision_time = clean(data.iloc[0]["DecisionTime_NewYork"])
    decision_dt = pd.Timestamp(
        f"{rejection_date} {decision_time}", tz="America/New_York"
    )
    # La bougie timestampée à l'heure de décision est incluse comme première
    # information disponible au moment du rejet.
    return data[data["Datetime_NewYork"] >= decision_dt].copy().reset_index(drop=True)


def locate_threshold(
    post: pd.DataFrame, reference: float, threshold_pct: float
) -> Tuple[str, str, str]:
    target = reference * (1.0 + threshold_pct / 100.0)
    index = first_true_index(post["High"] >= target)
    if index is None:
        return "FALSE", "", ""
    row = post.loc[index]
    return "TRUE", dt_text(row["Datetime_NewYork"]), clean(row["SessionLabel"])


def adverse_before_target(
    post: pd.DataFrame, reference: float, target_pct: float
) -> Optional[float]:
    target = reference * (1.0 + target_pct / 100.0)
    target_positions = post.index[post["High"] >= target].tolist()
    if target_positions:
        window = post.loc[: target_positions[0]]
    else:
        window = post
    if window.empty:
        return None
    return pct(float(window["Low"].min()), reference)


def detect_vwap_reclaim(post: pd.DataFrame) -> Optional[int]:
    previous_below = post["Close"].shift(1) <= post["SessionVWAP"].shift(1)
    current_above = post["Close"] > post["SessionVWAP"]
    return first_true_index(previous_below & current_above)


def detect_or_reclaim(post: pd.DataFrame, or_high: Optional[float]) -> Optional[int]:
    if or_high is None:
        return None
    previous_below = post["Close"].shift(1) <= or_high
    current_above = post["Close"] > or_high
    return first_true_index(previous_below & current_above)


def detect_breakout_pullback(post: pd.DataFrame) -> Dict[str, Any]:
    breakout_mask = (
        post["PriorResistance12"].notna()
        & (post["Close"] > post["PriorResistance12"] * (1.0 + BREAKOUT_BUFFER_PCT / 100.0))
    )
    breakout_index = first_true_index(breakout_mask)

    result: Dict[str, Any] = {
        "breakout_index": None,
        "breakout_level": None,
        "breakout_volume_ratio": None,
        "volume_confirmed": False,
        "pullback_index": None,
        "pullback_held": False,
        "pullback_invalidated": False,
    }
    if breakout_index is None:
        return result

    breakout_row = post.loc[breakout_index]
    level = float(breakout_row["PriorResistance12"])
    median_volume = to_float(breakout_row["PriorMedianVolume12"])
    volume_ratio = (
        float(breakout_row["Volume"]) / median_volume
        if median_volume and median_volume > 0
        else None
    )

    result.update(
        {
            "breakout_index": breakout_index,
            "breakout_level": level,
            "breakout_volume_ratio": volume_ratio,
            "volume_confirmed": volume_ratio is not None and volume_ratio >= VOLUME_MULTIPLIER,
        }
    )

    end_index = min(len(post) - 1, breakout_index + PULLBACK_WINDOW_BARS)
    after = post.loc[breakout_index + 1 : end_index]
    if after.empty:
        return result

    invalidation_close = level * (1.0 - PULLBACK_INVALIDATION_PCT / 100.0)
    tolerance_high = level * (1.0 + PULLBACK_TOLERANCE_PCT / 100.0)
    tolerance_low = level * (1.0 - PULLBACK_TOLERANCE_PCT / 100.0)

    for index, row in after.iterrows():
        if float(row["Close"]) < invalidation_close:
            result["pullback_invalidated"] = True
            break
        touched_zone = float(row["Low"]) <= tolerance_high and float(row["High"]) >= tolerance_low
        held_level = float(row["Close"]) >= level
        if touched_zone and held_level:
            result["pullback_index"] = int(index)
            result["pullback_held"] = True
            break

    return result


def risk_reward_from_entry(
    post: pd.DataFrame, entry_index: Optional[int], entry_price: Optional[float],
    breakout_level: Optional[float]
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    if entry_index is None or entry_price is None or breakout_level is None:
        return None, None, None

    # Stop structurel sous le plus bas des trois dernières bougies, sans dépasser
    # 0,50 % sous le niveau de breakout. Le stop demeure purement théorique.
    start = max(0, entry_index - 2)
    recent_low = float(post.loc[start:entry_index, "Low"].min())
    structural_floor = breakout_level * (1.0 - PULLBACK_INVALIDATION_PCT / 100.0)
    stop = min(recent_low, structural_floor)
    risk = entry_price - stop
    if risk <= 0:
        return stop, None, None
    target = entry_price * 1.02
    rr = (target - entry_price) / risk
    return stop, target, rr


def classify_shadow(
    breakout_found: bool,
    volume_confirmed: bool,
    pullback_held: bool,
    rr: Optional[float],
    target2_reached: bool,
    mae_before_2: Optional[float],
) -> Tuple[str, str]:
    if not breakout_found:
        return "PAS_DE_SECONDE_CHANCE", "Aucun breakout sans lookahead détecté"

    if volume_confirmed and pullback_held and rr is not None and rr >= 2.0:
        return "SECONDE_CHANCE_SHADOW_FORTE", "Breakout, volume, pullback tenu et ratio >= 2"

    if (volume_confirmed or pullback_held) and rr is not None and rr >= 1.5:
        return "SECONDE_CHANCE_SHADOW_POSSIBLE", "Breakout partiellement confirmé et ratio >= 1,5"

    if target2_reached and mae_before_2 is not None and mae_before_2 > -2.0:
        return "CAS_AMBIGU", "Mouvement +2 % propre, mais structure Shadow incomplète"

    return "PAS_DE_SECONDE_CHANCE", "Structure ou ratio insuffisant"


def analyze_event(path: Path, decisions: Sequence[Dict[str, str]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    data = load_event(path)
    post = post_rejection_frame(data)
    if post.empty:
        raise ValueError(f"Aucune bougie après le rejet dans {path}")

    event_id = clean(data.iloc[0]["EventID"])
    ticker = clean(data.iloc[0]["Ticker"])
    rejection_date = clean(data.iloc[0]["RejectionDate"])
    role = clean(data.iloc[0]["EventRole"])
    decision = find_decision(decisions, ticker, rejection_date)

    reference = to_float(decision.get("decision_close")) if decision else None
    reference_source = "intraday_decisions.decision_close"
    if reference is None:
        reference = float(post.iloc[0]["Close"])
        reference_source = "premiere_bougie_disponible_apres_rejet"

    or_high = to_float(decision.get("opening_range_high")) if decision else None
    saved_vwap = to_float(decision.get("vwap")) if decision else None
    reason = clean(decision.get("reason")) if decision else clean(data.iloc[0].get("RejectionReason"))

    reached2, time2, session2 = locate_threshold(post, reference, TARGET_2_PCT)
    reached3, time3, session3 = locate_threshold(post, reference, TARGET_3_PCT)
    mfe = pct(float(post["High"].max()), reference)
    mae = pct(float(post["Low"].min()), reference)
    mae_before_2 = adverse_before_target(post, reference, TARGET_2_PCT)

    vwap_index = detect_vwap_reclaim(post)
    or_index = detect_or_reclaim(post, or_high)
    structure = detect_breakout_pullback(post)

    breakout_index = structure["breakout_index"]
    pullback_index = structure["pullback_index"]
    entry_index = pullback_index if pullback_index is not None else breakout_index
    entry_price = float(post.loc[entry_index, "Close"]) if entry_index is not None else None
    stop, target, rr = risk_reward_from_entry(
        post, entry_index, entry_price, structure["breakout_level"]
    )

    shadow_decision, shadow_reason = classify_shadow(
        breakout_found=breakout_index is not None,
        volume_confirmed=bool(structure["volume_confirmed"]),
        pullback_held=bool(structure["pullback_held"]),
        rr=rr,
        target2_reached=reached2 == "TRUE",
        mae_before_2=mae_before_2,
    )

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
        "MFEPostRejectionPct": fmt(mfe),
        "MAEPostRejectionPct": fmt(mae),
        "MAEBeforePlus2Pct": fmt(mae_before_2),
        "ReachedPlus2Pct": reached2,
        "Plus2FirstTimeNY": time2,
        "Plus2Session": session2,
        "ReachedPlus3Pct": reached3,
        "Plus3FirstTimeNY": time3,
        "Plus3Session": session3,
        "VWAPReclaim": bool_text(vwap_index is not None),
        "VWAPReclaimTimeNY": dt_text(post.loc[vwap_index, "Datetime_NewYork"]) if vwap_index is not None else "",
        "ORHighReclaim": bool_text(or_index is not None),
        "ORHighReclaimTimeNY": dt_text(post.loc[or_index, "Datetime_NewYork"]) if or_index is not None else "",
        "BreakoutDetected": bool_text(breakout_index is not None),
        "BreakoutTimeNY": dt_text(post.loc[breakout_index, "Datetime_NewYork"]) if breakout_index is not None else "",
        "BreakoutLevel": fmt(structure["breakout_level"]),
        "BreakoutVolumeRatio": fmt(structure["breakout_volume_ratio"]),
        "VolumeConfirmed": bool_text(bool(structure["volume_confirmed"])),
        "PullbackDetected": bool_text(pullback_index is not None),
        "PullbackTimeNY": dt_text(post.loc[pullback_index, "Datetime_NewYork"]) if pullback_index is not None else "",
        "PullbackHeld": bool_text(bool(structure["pullback_held"])),
        "PullbackInvalidated": bool_text(bool(structure["pullback_invalidated"])),
        "ShadowEntryTimeNY": dt_text(post.loc[entry_index, "Datetime_NewYork"]) if entry_index is not None else "",
        "ShadowEntryPrice": fmt(entry_price),
        "ShadowStopPrice": fmt(stop),
        "ShadowTarget2Price": fmt(target),
        "ShadowRiskRewardTo2Pct": fmt(rr),
        "ShadowDecision": shadow_decision,
        "ShadowReason": shadow_reason,
        "ObservationOnly": "TRUE",
        "V1ExecutionChanged": "FALSE",
    }

    # Journal des événements techniques détectés pour audit.
    event_rows: List[Dict[str, Any]] = []
    event_specs = [
        ("REJET_V1", 0),
        ("REPRISE_VWAP", vwap_index),
        ("REPRISE_OR_HIGH", or_index),
        ("BREAKOUT_12_BARS", breakout_index),
        ("PULLBACK_TENU", pullback_index),
    ]
    for event_type, index in event_specs:
        if index is None:
            continue
        row = post.loc[index]
        event_rows.append(
            {
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
                "PriorMedianVolume12": fmt(to_float(row["PriorMedianVolume12"]), 0),
            }
        )

    return analysis, event_rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_report(rows: Sequence[Dict[str, Any]]) -> str:
    lines = [
        "=" * 104,
        "ANALYSE 5 MINUTES DES REJETS V1 - SECONDE CHANCE SHADOW",
        "=" * 104,
        "Mode observation uniquement. V1, V2 Shadow et portefeuille non modifiés.",
        "",
    ]
    for row in rows:
        lines.extend(
            [
                f"{row['EventID']} | rôle {row['EventRole']}",
                f"  Rejet      : {row['RejectionDate']} {row['DecisionTime_NewYork']} | {row['RejectionReason']}",
                f"  Référence  : {row['ReferencePrice']} | OR High {row['OpeningRangeHigh']}",
                f"  Trajectoire: MFE {row['MFEPostRejectionPct']} % | MAE {row['MAEPostRejectionPct']} % | MAE avant +2 % {row['MAEBeforePlus2Pct']} %",
                f"  Objectifs  : +2 % {row['ReachedPlus2Pct']} {row['Plus2FirstTimeNY']} | +3 % {row['ReachedPlus3Pct']} {row['Plus3FirstTimeNY']}",
                f"  Structure  : breakout {row['BreakoutDetected']} | volume {row['VolumeConfirmed']} | pullback tenu {row['PullbackHeld']}",
                f"  Shadow     : {row['ShadowDecision']} | RR {row['ShadowRiskRewardTo2Pct']} | {row['ShadowReason']}",
                "",
            ]
        )
    lines.extend(
        [
            "RÈGLES DE PRUDENCE",
            "- Quatre événements ne suffisent pas à valider une stratégie.",
            "- Les seuils structurels sont des hypothèses Shadow et non des paramètres V1.",
            "- L'analyse utilise uniquement les bougies disponibles avant chaque détection.",
            "- Une éventuelle promotion exige backtest, walk-forward et validation OOS.",
            "",
            "V1ExecutionChanged : False",
            "=" * 104,
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse 5 minutes des rejets V1 ciblés.")
    parser.add_argument("--data-dir", default="historical_lab/data/rejection_replay")
    parser.add_argument("--decisions", default="intraday_decisions.csv")
    parser.add_argument("--output-dir", default="historical_lab/results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    data_dir = Path(args.data_dir)
    decisions_path = Path(args.decisions)
    output_dir = Path(args.output_dir)

    if not data_dir.is_dir():
        print(f"ERREUR : dossier introuvable : {data_dir}", file=sys.stderr)
        return 2
    if not decisions_path.exists():
        print(f"ERREUR : fichier introuvable : {decisions_path}", file=sys.stderr)
        return 2

    files = sorted(data_dir.glob("*_J0_J4_5m.csv"))
    if not files:
        print(f"ERREUR : aucun fichier *_J0_J4_5m.csv dans {data_dir}", file=sys.stderr)
        return 2

    decisions = read_decisions(decisions_path)
    analyses: List[Dict[str, Any]] = []
    technical_events: List[Dict[str, Any]] = []

    for path in files:
        analysis, events = analyze_event(path, decisions)
        analyses.append(analysis)
        technical_events.extend(events)

    analysis_path = output_dir / "rejection_replay_5m_analysis.csv"
    events_path = output_dir / "rejection_replay_5m_events.csv"
    report_path = output_dir / "rejection_replay_5m_report.txt"

    write_csv(analysis_path, analyses)
    write_csv(events_path, technical_events)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(analyses), encoding="utf-8")

    print("=" * 104)
    print("ANALYSE 5 MINUTES DES REJETS V1 - SECONDE CHANCE SHADOW")
    print("=" * 104)
    print(f"Événements analysés : {len(analyses)}")
    for row in analyses:
        print(
            f"{row['EventID']} | {row['ShadowDecision']} | "
            f"MFE {row['MFEPostRejectionPct']} % | MAE {row['MAEPostRejectionPct']} % | "
            f"+2 % {row['ReachedPlus2Pct']}"
        )
    print(f"Détail  : {analysis_path}")
    print(f"Événements techniques : {events_path}")
    print(f"Rapport : {report_path}")
    print("ObservationOnly : True")
    print("V1ExecutionChanged : False")
    print("=" * 104)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
