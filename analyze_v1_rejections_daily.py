#!/usr/bin/env python3
"""
analyze_v1_rejections_daily.py

Analyse observationnelle des rejets V1 à partir de :
  - intraday_decisions.csv
  - historical_lab/data/raw/*.csv

AUCUN IMPACT SUR V1, V2 Shadow, le portefeuille ou l'exécution.

Principes méthodologiques :
  1. Seules les lignes dont la décision n'est ni ACHAT CONFIRME ni ACHAT REACTIVE
     sont analysées comme rejets.
  2. La référence de rendement est, par priorité :
       decision_close -> close_bougie_4 -> signal_price.
  3. La performance à J correspond à la clôture de la session du rejet.
  4. J+1, J+2 et J+4 désignent des séances boursières, pas des jours calendaires.
  5. Le MFE/MAE principal commence à J+1. Le High/Low de J peut avoir eu lieu
     avant l'heure du rejet et ne doit donc pas être utilisé comme preuve.
  6. Pour les rejets sans decision_time, les métriques J+1...J+4 restent valides,
     mais la référence intraday est marquée comme approximative.

Sorties :
  - historical_lab/results/v1_rejections_daily_analysis.csv
  - historical_lab/results/v1_rejections_daily_summary.csv
  - historical_lab/results/v1_rejections_daily_report.txt
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

BUY_DECISIONS = {"ACHAT CONFIRME", "ACHAT REACTIVE", "ACHAT RÉACTIF"}
REQUIRED_DECISION_COLUMNS = {"ticker", "session_date", "decision", "reason"}
REQUIRED_DAILY_COLUMNS = {"Date", "Close", "High", "Low", "Open", "Volume"}


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalized(value: Any) -> str:
    return clean(value).upper().replace("É", "E")


def to_float(value: Any) -> Optional[float]:
    text = clean(value).replace("&gt;", ">").replace(",", ".")
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


def truth(value: Optional[bool]) -> str:
    return "" if value is None else ("TRUE" if value else "FALSE")


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def validate_columns(path: Path, fields: Sequence[str], required: Iterable[str]) -> None:
    missing = sorted(set(required) - set(fields))
    if missing:
        raise ValueError(f"Colonnes manquantes dans {path}: {', '.join(missing)}")


def write_csv(path: Path, fields: Sequence[str], rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def choose_reference(row: Dict[str, str]) -> Tuple[Optional[float], str, str]:
    for column in ("decision_close", "close_bougie_4", "signal_price"):
        value = to_float(row.get(column))
        if value is not None and value > 0:
            quality = "EXACTE_DECISION" if column == "decision_close" and clean(row.get("decision_time")) else "APPROXIMATIVE"
            return value, column, quality
    return None, "", "INDISPONIBLE"


def load_daily(path: Path) -> List[Dict[str, Any]]:
    fields, rows = read_csv(path)
    validate_columns(path, fields, REQUIRED_DAILY_COLUMNS)
    parsed: List[Dict[str, Any]] = []
    for row in rows:
        date_text = clean(row.get("Date"))[:10]
        try:
            date_obj = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            continue
        parsed.append({
            "date": date_obj,
            "date_text": date_text,
            "open": to_float(row.get("Open")),
            "high": to_float(row.get("High")),
            "low": to_float(row.get("Low")),
            "close": to_float(row.get("Close")),
            "volume": to_float(row.get("Volume")),
        })
    parsed.sort(key=lambda item: item["date"])
    return parsed


def index_for_session(daily: Sequence[Dict[str, Any]], session_date: str) -> Optional[int]:
    target = clean(session_date)[:10]
    for index, bar in enumerate(daily):
        if bar["date_text"] == target:
            return index
    return None


def value_at(daily: Sequence[Dict[str, Any]], start: int, offset: int, column: str) -> Optional[float]:
    index = start + offset
    if 0 <= index < len(daily):
        return daily[index].get(column)
    return None


def date_at(daily: Sequence[Dict[str, Any]], start: int, offset: int) -> str:
    index = start + offset
    if 0 <= index < len(daily):
        return clean(daily[index].get("date_text"))
    return ""


def classify(mfe_4: Optional[float], mae_4: Optional[float]) -> str:
    if mfe_4 is None:
        return "DONNEES_INCOMPLETES"
    if mfe_4 >= 3.0:
        return "REJET_SUSPECT_3PCT"
    if mfe_4 >= 2.0:
        return "REJET_SUSPECT_2PCT"
    if mae_4 is not None and mae_4 <= -2.0:
        return "REJET_PROTECTEUR"
    return "REJET_NEUTRE"


def analyze(decision_row: Dict[str, str], daily: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    session_date = clean(decision_row.get("session_date"))[:10]
    start = index_for_session(daily, session_date)
    reference, reference_source, reference_quality = choose_reference(decision_row)

    output: Dict[str, Any] = {
        "Ticker": clean(decision_row.get("ticker")),
        "SignalDate": clean(decision_row.get("signal_date"))[:10],
        "SessionDate": session_date,
        "DecisionTime": clean(decision_row.get("decision_time")),
        "Decision": clean(decision_row.get("decision")),
        "Reason": clean(decision_row.get("reason")),
        "ReferencePrice": fmt(reference),
        "ReferenceSource": reference_source,
        "ReferenceQuality": reference_quality,
        "DailyFileFound": "TRUE",
        "SessionFound": truth(start is not None),
    }

    if start is None or reference is None:
        output.update({
            "AvailableFutureSessions": 0,
            "AnalysisStatus": "SESSION_OU_REFERENCE_INTROUVABLE",
            "Classification": "DONNEES_INCOMPLETES",
        })
        return output

    available = max(0, min(4, len(daily) - start - 1))
    output["AvailableFutureSessions"] = available

    for offset, label in ((0, "J"), (1, "J1"), (2, "J2"), (4, "J4")):
        close_value = value_at(daily, start, offset, "close")
        output[f"{label}Date"] = date_at(daily, start, offset)
        output[f"{label}Close"] = fmt(close_value)
        output[f"{label}CloseReturnPct"] = fmt(pct(close_value, reference))

    future_bars = list(daily[start + 1 : start + 5])
    highs = [bar["high"] for bar in future_bars if bar.get("high") is not None]
    lows = [bar["low"] for bar in future_bars if bar.get("low") is not None]
    max_high = max(highs) if highs else None
    min_low = min(lows) if lows else None
    mfe = pct(max_high, reference)
    mae = pct(min_low, reference)

    output.update({
        "MaxHighJ1J4": fmt(max_high),
        "MinLowJ1J4": fmt(min_low),
        "MFEPctJ1J4": fmt(mfe),
        "MAEPctJ1J4": fmt(mae),
        "ReachedPlus2PctJ1J4": truth(mfe is not None and mfe >= 2.0),
        "ReachedPlus3PctJ1J4": truth(mfe is not None and mfe >= 3.0),
        "TouchedMinus2PctJ1J4": truth(mae is not None and mae <= -2.0),
        "Classification": classify(mfe, mae),
        "AnalysisStatus": "OK" if available >= 4 else "FENETRE_PARTIELLE",
    })
    return output


def build_summary(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    groups["TOUS_LES_REJETS"] = list(rows)
    for row in rows:
        groups[clean(row.get("Reason"))].append(row)

    summary: List[Dict[str, Any]] = []
    for group, items in groups.items():
        valid = [item for item in items if item.get("AnalysisStatus") in {"OK", "FENETRE_PARTIELLE"}]
        full = [item for item in valid if item.get("AnalysisStatus") == "OK"]

        def numbers(column: str, source: Sequence[Dict[str, Any]] = valid) -> List[float]:
            values = [to_float(item.get(column)) for item in source]
            return [value for value in values if value is not None]

        mfe_values = numbers("MFEPctJ1J4")
        mae_values = numbers("MAEPctJ1J4")
        j4_values = numbers("J4CloseReturnPct", full)
        plus2 = sum(item.get("ReachedPlus2PctJ1J4") == "TRUE" for item in valid)
        plus3 = sum(item.get("ReachedPlus3PctJ1J4") == "TRUE" for item in valid)
        summary.append({
            "Group": group,
            "RejectCount": len(items),
            "AnalyzableCount": len(valid),
            "FullJ4Count": len(full),
            "ReachedPlus2Count": plus2,
            "ReachedPlus2RatePct": fmt(100.0 * plus2 / len(valid) if valid else None, 2),
            "ReachedPlus3Count": plus3,
            "ReachedPlus3RatePct": fmt(100.0 * plus3 / len(valid) if valid else None, 2),
            "AverageMFEPctJ1J4": fmt(sum(mfe_values) / len(mfe_values) if mfe_values else None),
            "AverageMAEPctJ1J4": fmt(sum(mae_values) / len(mae_values) if mae_values else None),
            "AverageJ4CloseReturnPct": fmt(sum(j4_values) / len(j4_values) if j4_values else None),
        })
    return summary


def build_report(rows: Sequence[Dict[str, Any]], summary: Sequence[Dict[str, Any]], source: Path) -> str:
    counts = Counter(clean(row.get("Classification")) for row in rows)
    analyzed = sum(row.get("AnalysisStatus") in {"OK", "FENETRE_PARTIELLE"} for row in rows)
    lines = [
        "=" * 92,
        "ANALYSE DAILY DES REJETS V1 - OBSERVATION UNIQUEMENT",
        "=" * 92,
        f"Source decisions : {source}",
        f"Rejets détectés  : {len(rows)}",
        f"Rejets analysés  : {analyzed}",
        "",
        "Classification :",
    ]
    for label in ("REJET_SUSPECT_3PCT", "REJET_SUSPECT_2PCT", "REJET_PROTECTEUR", "REJET_NEUTRE", "DONNEES_INCOMPLETES"):
        lines.append(f"  {label:<24} {counts.get(label, 0)}")

    lines.extend([
        "",
        "Attention méthodologique :",
        "- MFE/MAE utilisent uniquement J+1 à J+4 afin d'éviter le High/Low de J antérieur au rejet.",
        "- Une ligne FENETRE_PARTIELLE ne possède pas encore quatre séances futures complètes.",
        "- Un rejet suspect n'est pas automatiquement injustifié : il devra être rejoué en données 5 minutes.",
        "- Les observations répétées sur un même ticker ne sont pas statistiquement indépendantes.",
        "",
        "Rejets suspects classés par MFE décroissant :",
    ])
    suspects = [row for row in rows if clean(row.get("Classification")).startswith("REJET_SUSPECT")]
    suspects.sort(key=lambda row: to_float(row.get("MFEPctJ1J4")) or -999.0, reverse=True)
    if not suspects:
        lines.append("  Aucun rejet n'a atteint +2 % sur les séances futures disponibles.")
    else:
        for row in suspects:
            lines.append(
                f"  {row.get('Ticker')} {row.get('SessionDate')} | "
                f"MFE {row.get('MFEPctJ1J4')} % | MAE {row.get('MAEPctJ1J4')} % | "
                f"{row.get('Reason')} | {row.get('AnalysisStatus')}"
            )
    lines.extend(["", "V1ExecutionChanged : False", "=" * 92, ""])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse Daily des rejets V1, sans impact sur V1.")
    parser.add_argument("--decisions", default="intraday_decisions.csv")
    parser.add_argument("--raw-dir", default="historical_lab/data/raw")
    parser.add_argument("--output-dir", default="historical_lab/results")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    decisions_path = Path(args.decisions)
    raw_dir = Path(args.raw_dir)
    output_dir = Path(args.output_dir)

    if not decisions_path.exists():
        print(f"ERREUR : fichier introuvable : {decisions_path}", file=sys.stderr)
        return 2
    if not raw_dir.is_dir():
        print(f"ERREUR : dossier introuvable : {raw_dir}", file=sys.stderr)
        return 2

    fields, decisions = read_csv(decisions_path)
    validate_columns(decisions_path, fields, REQUIRED_DECISION_COLUMNS)
    rejections = [row for row in decisions if normalized(row.get("decision")) not in BUY_DECISIONS]

    results: List[Dict[str, Any]] = []
    daily_cache: Dict[str, List[Dict[str, Any]]] = {}
    for row in rejections:
        ticker = clean(row.get("ticker"))
        daily_path = raw_dir / f"{ticker}.csv"
        if not daily_path.exists():
            reference, source, quality = choose_reference(row)
            results.append({
                "Ticker": ticker,
                "SignalDate": clean(row.get("signal_date"))[:10],
                "SessionDate": clean(row.get("session_date"))[:10],
                "DecisionTime": clean(row.get("decision_time")),
                "Decision": clean(row.get("decision")),
                "Reason": clean(row.get("reason")),
                "ReferencePrice": fmt(reference),
                "ReferenceSource": source,
                "ReferenceQuality": quality,
                "DailyFileFound": "FALSE",
                "SessionFound": "FALSE",
                "AvailableFutureSessions": 0,
                "AnalysisStatus": "FICHIER_DAILY_INTROUVABLE",
                "Classification": "DONNEES_INCOMPLETES",
            })
            continue
        if ticker not in daily_cache:
            daily_cache[ticker] = load_daily(daily_path)
        results.append(analyze(row, daily_cache[ticker]))

    detail_fields = [
        "Ticker", "SignalDate", "SessionDate", "DecisionTime", "Decision", "Reason",
        "ReferencePrice", "ReferenceSource", "ReferenceQuality", "DailyFileFound", "SessionFound",
        "AvailableFutureSessions", "JDate", "JClose", "JCloseReturnPct",
        "J1Date", "J1Close", "J1CloseReturnPct", "J2Date", "J2Close", "J2CloseReturnPct",
        "J4Date", "J4Close", "J4CloseReturnPct", "MaxHighJ1J4", "MinLowJ1J4",
        "MFEPctJ1J4", "MAEPctJ1J4", "ReachedPlus2PctJ1J4", "ReachedPlus3PctJ1J4",
        "TouchedMinus2PctJ1J4", "Classification", "AnalysisStatus",
    ]
    summary_fields = [
        "Group", "RejectCount", "AnalyzableCount", "FullJ4Count", "ReachedPlus2Count",
        "ReachedPlus2RatePct", "ReachedPlus3Count", "ReachedPlus3RatePct",
        "AverageMFEPctJ1J4", "AverageMAEPctJ1J4", "AverageJ4CloseReturnPct",
    ]

    summary = build_summary(results)
    detail_path = output_dir / "v1_rejections_daily_analysis.csv"
    summary_path = output_dir / "v1_rejections_daily_summary.csv"
    report_path = output_dir / "v1_rejections_daily_report.txt"
    write_csv(detail_path, detail_fields, results)
    write_csv(summary_path, summary_fields, summary)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(build_report(results, summary, decisions_path), encoding="utf-8")

    print("=" * 92)
    print("ANALYSE DAILY DES REJETS V1 - MODE OBSERVATION UNIQUEMENT")
    print("=" * 92)
    print(f"Décisions totales : {len(decisions)}")
    print(f"Rejets détectés   : {len(rejections)}")
    print(f"Rejets analysés   : {sum(r.get('AnalysisStatus') in {'OK', 'FENETRE_PARTIELLE'} for r in results)}")
    print(f"Détail             : {detail_path}")
    print(f"Résumé             : {summary_path}")
    print(f"Rapport texte      : {report_path}")
    print("V1ExecutionChanged : False")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
